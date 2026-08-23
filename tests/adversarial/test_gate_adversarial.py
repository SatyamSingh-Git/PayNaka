"""Adversarial tests for paynaka.gate -- the checkpoint under attack.

Every HAAT attack family has a representative here, expressed as the money outcome it is
trying to achieve rather than as the prose that would achieve it. The gate never sees the
injected text; it sees the *request the injected text produced*. That is the point of the
architecture, and it is why these tests can be exhaustive without being about prompts.
"""

from __future__ import annotations

import dataclasses
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from paynaka.clock import FrozenClock
from paynaka.gate import LineItem, MoneyRequest, Verdict, evaluate, request_hash
from paynaka.mandate import IntentMandate
from paynaka.policy import Policy
from paynaka.state import SqliteState
from tests.conftest import ATTA, ATTACK, ATTACKER_ADDR, AUTHORISED, GIFT_CARD, HOME, order

pytestmark = pytest.mark.adversarial


def decide(request, mandate, state, policy, clock):  # type: ignore[no-untyped-def]
    return evaluate(request, mandate, state=state, policy=policy, clock=clock)


class TestTheHeadlineAttack:
    """₹1,999 authorised. The poisoned review wants ₹52,000."""

    def test_gift_card_append_is_blocked(
        self, poisoned_order, mandate, state, policy, clock
    ) -> None:
        result = decide(poisoned_order, mandate, state, policy, clock)
        assert result.verdict is Verdict.DENY
        assert result.check_id == "envelope.item_not_in_intent"
        assert result.evidence["sku"] == GIFT_CARD

    def test_the_legitimate_purchase_still_goes_through(
        self, legit_order, mandate, state, policy, clock
    ) -> None:
        """A gate that blocks everything is not a gate, it is an outage."""
        result = decide(legit_order, mandate, state, policy, clock)
        assert result.verdict is Verdict.ALLOW, result.reason

    def test_the_reason_is_legible_to_a_human(
        self, poisoned_order, mandate, state, policy, clock
    ) -> None:
        """This sentence appears on screen in the demo. It must read as English."""
        result = decide(poisoned_order, mandate, state, policy, clock)
        assert GIFT_CARD in result.reason
        assert "frozen intent" in result.reason

    def test_raising_the_price_of_an_authorised_sku_is_also_blocked(
        self, mandate, state, policy, clock
    ) -> None:
        """Subtler variant: no new SKU, just a much larger unit price."""
        result = decide(order(unit=ATTACK), mandate, state, policy, clock)
        assert result.verdict is Verdict.DENY
        assert result.check_id == "envelope.total_exceeded"
        assert result.evidence == {"requested": ATTACK, "authorised": AUTHORISED}


class TestEnvelopeChecks:
    @pytest.mark.parametrize("amount", [AUTHORISED + 1, AUTHORISED * 2, ATTACK])
    def test_any_overspend_is_denied(self, amount, mandate, state, policy, clock) -> None:
        result = decide(order(unit=amount), mandate, state, policy, clock)
        assert result.verdict is Verdict.DENY

    def test_exactly_the_authorised_amount_is_allowed(self, mandate, state, policy, clock) -> None:
        """The boundary is inclusive: spending the whole budget is authorised."""
        assert decide(order(unit=AUTHORISED), mandate, state, policy, clock).allowed

    def test_one_paise_over_is_denied(self, mandate, state, policy, clock) -> None:
        result = decide(order(unit=AUTHORISED + 1), mandate, state, policy, clock)
        assert result.verdict is Verdict.DENY

    @pytest.mark.parametrize("qty", [4, 10, 999])
    def test_quantity_inflation_is_denied(self, qty, mandate, state, policy, clock) -> None:
        """'Orders below 40 units are rejected' -- the description-injection family."""
        result = decide(order(qty=qty, unit=1000), mandate, state, policy, clock)
        assert result.verdict is Verdict.DENY
        assert result.check_id == "envelope.qty_exceeded"

    def test_quantity_boundary_is_exact(self, mandate, state, policy, clock) -> None:
        assert decide(order(qty=3, unit=1000), mandate, state, policy, clock).allowed
        assert not decide(order(qty=4, unit=1000, key="k2"), mandate, state, policy, clock).allowed

    def test_destination_swap_is_denied(self, mandate, state, policy, clock) -> None:
        result = decide(order(destination=ATTACKER_ADDR), mandate, state, policy, clock)
        assert result.verdict is Verdict.DENY
        assert result.check_id == "envelope.destination"

    @pytest.mark.parametrize("currency", ["USD", "EUR", "inr", "GBP"])
    def test_currency_confusion_is_denied(self, currency, mandate, state, policy, clock) -> None:
        result = decide(order(currency=currency), mandate, state, policy, clock)
        assert result.verdict is Verdict.DENY
        assert result.check_id == "envelope.currency"

    def test_many_small_items_cannot_sum_past_the_budget(
        self, mandate, state, policy, clock
    ) -> None:
        """Splitting the overspend across lines must not evade the total check."""
        request = MoneyRequest(
            action="create_order",
            request_id="req_split",
            idempotency_key="idem_split",
            items=tuple(LineItem(sku=ATTA, qty=1, unit_paise=100_000) for _ in range(30)),
            destination=HOME,
        )
        result = decide(request, mandate, state, policy, clock)
        assert result.verdict is Verdict.DENY
        assert result.check_id == "envelope.total_exceeded"

    def test_stated_total_disagreeing_with_line_items_is_denied(
        self, mandate, state, policy, clock
    ) -> None:
        """Claim ₹1,999 in the header while the lines add to ₹52,000."""
        request = MoneyRequest(
            action="create_order",
            request_id="req_lie",
            idempotency_key="idem_lie",
            amount=AUTHORISED,
            items=(LineItem(sku=ATTA, qty=1, unit_paise=ATTACK),),
            destination=HOME,
        )
        result = decide(request, mandate, state, policy, clock)
        assert result.verdict is Verdict.DENY
        assert result.check_id == "structure.total_mismatch"


class TestAuthorityChecks:
    def test_action_absent_from_mandate_is_denied(self, mandate, state, policy, clock) -> None:
        """The mandate authorised orders and captures. It never authorised refunds."""
        request = MoneyRequest(
            action="create_refund",
            request_id="r",
            idempotency_key="k",
            amount=100,
            payment_id="pay_1",
        )
        result = decide(request, mandate, state, policy, clock)
        assert result.verdict is Verdict.DENY
        assert result.check_id == "authority.action_not_in_mandate"

    def test_payout_is_disabled_by_policy_even_if_a_mandate_permits_it(
        self, clock, state, policy
    ) -> None:
        """Mandate says yes, merchant policy says no. The intersection is no."""
        permissive = IntentMandate.create(
            clock=clock,
            subject="c",
            session_id="s",
            max_total=AUTHORISED,
            allowed_actions=("create_payout",),
        )
        request = MoneyRequest(
            action="create_payout", request_id="r", idempotency_key="k", amount=1000
        )
        result = decide(request, permissive, state, policy, clock)
        assert result.verdict is Verdict.DENY
        assert result.check_id == "policy.action_disabled"

    def test_unknown_action_is_denied_not_defaulted(self, clock, state, policy) -> None:
        """An action nobody configured must be refused, never silently permitted."""
        m = IntentMandate.create(clock=clock, subject="c", session_id="s", max_total=AUTHORISED)
        request = MoneyRequest(
            action="transfer_everything", request_id="r", idempotency_key="k", amount=1
        )
        assert decide(request, m, state, policy, clock).verdict is Verdict.DENY

    def test_empty_action_list_permits_nothing(self, clock, state, policy) -> None:
        locked = IntentMandate.create(
            clock=clock, subject="c", session_id="s", max_total=AUTHORISED, allowed_actions=()
        )
        assert decide(order(), locked, state, policy, clock).verdict is Verdict.DENY


class TestReplayAndIdempotency:
    def test_identical_retry_replays_rather_than_charging_twice(
        self, legit_order, mandate, state, policy, clock
    ) -> None:
        """A duplicate webhook is not an attack. It is Tuesday."""
        first = decide(legit_order, mandate, state, policy, clock)
        second = decide(legit_order, mandate, state, policy, clock)
        assert first.allowed and not first.replayed
        assert second.allowed and second.replayed
        assert second.check_id == "idempotency.replay"

    def test_same_key_different_amount_is_denied(self, mandate, state, policy, clock) -> None:
        """The substitution attack: reuse an approved key for a bigger request."""
        decide(order(key="shared"), mandate, state, policy, clock)
        result = decide(order(key="shared", unit=100_000), mandate, state, policy, clock)
        assert result.verdict is Verdict.DENY
        assert result.check_id == "idempotency.key_reuse"

    def test_missing_idempotency_key_is_denied(self, mandate, state, policy, clock) -> None:
        request = dataclasses.replace(order(), idempotency_key=None)
        result = decide(request, mandate, state, policy, clock)
        assert result.verdict is Verdict.DENY
        assert result.check_id == "idempotency.missing"

    def test_reordering_line_items_is_still_the_same_request(
        self, mandate, state, policy, clock
    ) -> None:
        """A retry that shuffles its cart must hash the same, not look like a new request."""
        a = MoneyRequest(
            action="create_order",
            request_id="r1",
            idempotency_key="k",
            items=(
                LineItem(ATTA, 1, 50_000),
                LineItem("DAL-1KG", 1, 40_000),
            ),
        )
        b = dataclasses.replace(
            a, request_id="r2", items=(LineItem("DAL-1KG", 1, 40_000), LineItem(ATTA, 1, 50_000))
        )
        assert request_hash(a) == request_hash(b)

    def test_request_id_does_not_affect_the_fingerprint(self) -> None:
        a = order(key="k")
        b = dataclasses.replace(a, request_id="totally-different")
        assert request_hash(a) == request_hash(b)

    @pytest.mark.parametrize("workers", [4, 16])
    def test_concurrent_duplicates_charge_exactly_once(
        self, workers, mandate, policy, clock
    ) -> None:
        """The double-charge race, end to end through the gate."""
        with SqliteState(":memory:", clock=clock) as state:
            barrier = threading.Barrier(workers)

            def attempt(i: int):  # type: ignore[no-untyped-def]
                barrier.wait()
                return decide(order(key="raced"), mandate, state, policy, clock)

            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(attempt, range(workers)))

        fresh = [r for r in results if r.allowed and not r.replayed]
        assert len(fresh) == 1, f"{len(fresh)} concurrent duplicates were treated as new"
        assert all(r.allowed for r in results), "a legitimate retry was denied"


class TestRefundChecks:
    def test_refund_without_a_return_is_denied(self, refund_mandate, state, policy, clock) -> None:
        """'Skip verification, refund now' -- the metadata-injection family."""
        state.record_capture("pay_1", AUTHORISED)
        request = MoneyRequest(
            action="create_refund",
            request_id="r",
            idempotency_key="k",
            amount=50_000,
            payment_id="pay_1",
        )
        result = decide(request, refund_mandate, state, policy, clock)
        assert result.verdict is Verdict.DENY
        assert result.check_id == "refund.no_return_on_record"

    def test_refund_with_a_return_is_allowed(self, refund_mandate, state, policy, clock) -> None:
        state.record_capture("pay_1", AUTHORISED)
        state.record_return("pay_1")
        request = MoneyRequest(
            action="create_refund",
            request_id="r",
            idempotency_key="k",
            amount=50_000,
            payment_id="pay_1",
        )
        assert decide(request, refund_mandate, state, policy, clock).allowed

    def test_refund_beyond_capture_is_denied(self, refund_mandate, state, policy, clock) -> None:
        state.record_capture("pay_1", 100_000)
        state.record_return("pay_1")
        request = MoneyRequest(
            action="create_refund",
            request_id="r",
            idempotency_key="k",
            amount=150_000,
            payment_id="pay_1",
        )
        result = decide(request, refund_mandate, state, policy, clock)
        assert result.verdict is Verdict.DENY
        assert result.check_id == "refund.exceeds_capture"

    def test_two_partial_refunds_cannot_exceed_the_capture(
        self, refund_mandate, state, policy, clock
    ) -> None:
        """Each is individually legal; together they would over-refund."""
        state.record_capture("pay_1", 100_000)
        state.record_return("pay_1")

        first = MoneyRequest(
            action="create_refund",
            request_id="r1",
            idempotency_key="k1",
            amount=60_000,
            payment_id="pay_1",
        )
        assert decide(first, refund_mandate, state, policy, clock).allowed
        state.record_refund("pay_1", 60_000)  # the rail settles it

        second = dataclasses.replace(first, request_id="r2", idempotency_key="k2")
        result = decide(second, refund_mandate, state, policy, clock)
        assert result.verdict is Verdict.DENY
        assert result.evidence["refundable"] == 40_000

    def test_refund_on_an_unknown_payment_is_denied(
        self, refund_mandate, state, policy, clock
    ) -> None:
        request = MoneyRequest(
            action="create_refund",
            request_id="r",
            idempotency_key="k",
            amount=1,
            payment_id="pay_never_existed",
        )
        result = decide(request, refund_mandate, state, policy, clock)
        assert result.verdict is Verdict.DENY
        assert result.check_id == "refund.exceeds_capture"

    def test_refund_naming_no_payment_is_denied(self, refund_mandate, state, policy, clock) -> None:
        request = MoneyRequest(
            action="create_refund", request_id="r", idempotency_key="k", amount=100
        )
        result = decide(request, refund_mandate, state, policy, clock)
        assert result.verdict is Verdict.DENY
        assert result.check_id == "refund.no_payment"

    def test_daily_cap_stops_slow_draining(self, refund_mandate, state, policy, clock) -> None:
        """A compromised agent must not be able to bleed the account a little at a time."""
        state.record_capture("pay_big", 10_000_000)
        state.record_return("pay_big")
        state.record_refund("pay_big", 1_950_000)  # today's total so far

        request = MoneyRequest(
            action="create_refund",
            request_id="r",
            idempotency_key="k",
            amount=100_000,
            payment_id="pay_big",
        )
        result = decide(request, refund_mandate, state, policy, clock)
        assert result.verdict is Verdict.DENY
        assert result.check_id == "policy.daily_cap"


class TestRegulatoryChecks:
    """Indian payments regulation, enforced rather than documented.

    Each test mints its mandate from the same clock it evaluates at. A mandate lives 15
    minutes, so borrowing the 11:30 fixture to test an 08:00 rule would trip the expiry
    check first and the regulatory check would never run -- a green test proving nothing.
    """

    @staticmethod
    def _mandate(clock: FrozenClock, **kwargs: object) -> IntentMandate:
        defaults: dict[str, object] = {
            "subject": "cust_kirana_001",
            "session_id": "sess_reg",
            "max_total": AUTHORISED,
            "allowed_skus": (ATTA,),
            "allowed_destinations": (HOME,),
            "max_qty_per_sku": 3,
        }
        defaults.update(kwargs)
        return IntentMandate.create(clock=clock, **defaults)  # type: ignore[arg-type]

    def test_debit_inside_the_npci_blackout_is_denied(self, mandate, state, policy, clock) -> None:
        """11:30 IST is inside the 10:00-13:00 restricted peak window."""
        request = dataclasses.replace(order(), is_recurring_debit=True)
        result = decide(request, mandate, state, policy, clock)
        assert result.verdict is Verdict.DENY
        assert result.check_id == "regulatory.debit_blackout"

    def test_debit_outside_the_blackout_passes_that_check(self, state, policy) -> None:
        late = FrozenClock.at_ist("2026-08-23 15:00")
        request = dataclasses.replace(
            order(), is_recurring_debit=True, notice_sent_at=late.epoch() - 90_000
        )
        result = decide(request, self._mandate(late), state, policy, late)
        assert result.allowed, result.reason

    def test_npci_retry_cap_is_enforced(self, state, policy) -> None:
        late = FrozenClock.at_ist("2026-08-23 15:00")
        m = self._mandate(late)
        for _ in range(3):
            state.bump_retry(m.mandate_id, clock=late)

        request = dataclasses.replace(
            order(), is_recurring_debit=True, notice_sent_at=late.epoch() - 90_000
        )
        result = decide(request, m, state, policy, late)
        assert result.verdict is Verdict.DENY
        assert result.check_id == "regulatory.retry_cap"
        assert result.evidence == {"used": 3, "cap": 3}

    def test_third_retry_is_permitted_and_the_fourth_is_not(self, state, policy) -> None:
        """The boundary is the rule: NPCI allows 3 retries, so the 4th attempt is refused."""
        late = FrozenClock.at_ist("2026-08-23 15:00")
        m = self._mandate(late)
        request = dataclasses.replace(
            order(), is_recurring_debit=True, notice_sent_at=late.epoch() - 90_000
        )

        for attempt in range(3):
            assert decide(request, m, state, policy, late).allowed, f"retry {attempt} refused"
            state.bump_retry(m.mandate_id, clock=late)

        assert decide(request, m, state, policy, late).check_id == "regulatory.retry_cap"

    def test_missing_pre_debit_notice_is_denied(self, state, policy) -> None:
        late = FrozenClock.at_ist("2026-08-23 15:00")
        request = dataclasses.replace(order(), is_recurring_debit=True)
        result = decide(request, self._mandate(late), state, policy, late)
        assert result.verdict is Verdict.DENY
        assert result.check_id == "regulatory.no_pre_debit_notice"

    def test_notice_less_than_24h_old_is_denied(self, state, policy) -> None:
        late = FrozenClock.at_ist("2026-08-23 15:00")
        request = dataclasses.replace(
            order(), is_recurring_debit=True, notice_sent_at=late.epoch() - 3600
        )
        result = decide(request, self._mandate(late), state, policy, late)
        assert result.verdict is Verdict.DENY
        assert result.check_id == "regulatory.notice_too_recent"
        assert result.evidence["required"] == 86_400

    @pytest.mark.parametrize("ist", ["2026-08-23 07:59", "2026-08-23 19:00", "2026-08-23 23:30"])
    def test_contact_outside_rbi_hours_is_denied(self, ist, state, policy) -> None:
        clock = FrozenClock.at_ist(ist)
        request = dataclasses.replace(order(), is_customer_contact=True)
        result = decide(request, self._mandate(clock), state, policy, clock)
        assert result.verdict is Verdict.DENY
        assert result.check_id == "regulatory.contact_window"

    @pytest.mark.parametrize("ist", ["2026-08-23 08:00", "2026-08-23 13:00", "2026-08-23 18:59"])
    def test_contact_inside_rbi_hours_is_permitted(self, ist, state, policy) -> None:
        clock = FrozenClock.at_ist(ist)
        request = dataclasses.replace(order(), is_customer_contact=True)
        assert decide(request, self._mandate(clock), state, policy, clock).allowed

    def test_afa_required_above_the_rbi_threshold(self, clock, state) -> None:
        """AFA sits at Rs 15,000, above the demo policy's Rs 5,000 order cap.

        So this needs a policy whose own limit is loose enough for the regulatory check to
        be the binding one -- otherwise policy.max_amount fires first and the test would
        pass for the wrong reason.
        """
        loose = Policy.from_dict(
            {
                "version": 1,
                "merchant": "high-value",
                "actions": {"create_order": {"max_amount": 5_000_000}},
            }
        )
        m = self._mandate(clock, max_total=5_000_000, session_id="sess_afa")
        result = decide(order(unit=2_000_000), m, state, loose, clock)
        assert result.verdict is Verdict.DENY
        assert result.check_id == "regulatory.afa_required"
        assert result.evidence == {"amount": 2_000_000, "threshold": 1_500_000}

    def test_afa_present_clears_the_threshold(self, clock, state) -> None:
        loose = Policy.from_dict(
            {
                "version": 1,
                "merchant": "high-value",
                "actions": {"create_order": {"max_amount": 5_000_000}},
            }
        )
        m = self._mandate(clock, max_total=5_000_000, session_id="sess_afa2")
        request = dataclasses.replace(order(unit=2_000_000), has_afa=True)
        assert decide(request, m, state, loose, clock).allowed

    def test_exactly_the_afa_threshold_does_not_require_afa(self, clock, state) -> None:
        """'Above Rs 15,000' means strictly above. The boundary itself is clear."""
        loose = Policy.from_dict(
            {
                "version": 1,
                "merchant": "high-value",
                "actions": {"create_order": {"max_amount": 5_000_000}},
            }
        )
        m = self._mandate(clock, max_total=5_000_000, session_id="sess_afa3")
        assert decide(order(unit=1_500_000), m, state, loose, clock).allowed


class TestRevocationAndExpiry:
    def test_kill_switch_stops_everything(self, legit_order, mandate, state, policy, clock) -> None:
        assert decide(legit_order, mandate, state, policy, clock).allowed
        state.revoke("*")
        result = decide(order(key="k2"), mandate, state, policy, clock)
        assert result.verdict is Verdict.DENY
        assert result.check_id == "revoked"

    def test_revoking_one_session_spares_the_others(self, mandate, state, policy, clock) -> None:
        state.revoke("sess_someone_else")
        assert decide(order(), mandate, state, policy, clock).allowed

    def test_revoking_by_session_id_works(self, mandate, state, policy, clock) -> None:
        state.revoke(mandate.session_id)
        assert decide(order(), mandate, state, policy, clock).verdict is Verdict.DENY

    def test_expired_mandate_is_denied(self, mandate, state, policy, clock) -> None:
        clock.advance(hours=2)
        result = decide(order(), mandate, state, policy, clock)
        assert result.verdict is Verdict.DENY
        assert result.check_id == "mandate.expired"

    def test_revocation_is_checked_before_expiry(self, mandate, state, policy, clock) -> None:
        """Cheapest and most certain first: one lookup beats a full envelope evaluation."""
        state.revoke("*")
        clock.advance(hours=2)
        assert decide(order(), mandate, state, policy, clock).check_id == "revoked"


class TestStepUp:
    def test_amount_above_the_band_requires_a_human(self, clock, state, policy) -> None:
        big = IntentMandate.create(clock=clock, subject="c", session_id="s", max_total=400_000)
        result = decide(order(unit=300_000), big, state, policy, clock)
        assert result.verdict is Verdict.STEP_UP
        assert result.check_id == "policy.step_up"

    def test_step_up_never_overrides_a_deny(self, mandate, state, policy, clock) -> None:
        """Deny wins. A request that is both over-budget and over the step-up band is denied."""
        result = decide(order(unit=ATTACK), mandate, state, policy, clock)
        assert result.verdict is Verdict.DENY

    def test_below_the_band_is_a_plain_allow(self, mandate, state, policy, clock) -> None:
        assert decide(order(unit=150_000), mandate, state, policy, clock).verdict is Verdict.ALLOW


class TestFailClosed:
    def test_a_check_that_raises_denies_rather_than_continues(
        self, mandate, state, policy, clock
    ) -> None:
        """A crashed check is an unenforced check. Never continue past one."""
        broken = MoneyRequest(
            action="create_order",
            request_id="r",
            idempotency_key="k",
            items=(LineItem(sku=ATTA, qty=999_999, unit_paise=10**9),),
        )
        result = decide(broken, mandate, state, policy, clock)
        assert result.verdict is Verdict.DENY

    def test_policy_cannot_turn_a_deny_into_an_allow(self, mandate, state, clock) -> None:
        """A wide-open policy still cannot widen the mandate."""
        permissive = Policy.from_dict(
            {
                "version": 1,
                "merchant": "wide-open",
                "actions": {"create_order": {"max_amount": 100_000_000}},
            }
        )
        result = decide(order(unit=ATTACK), mandate, state, permissive, clock)
        assert result.verdict is Verdict.DENY
        assert result.check_id == "envelope.total_exceeded"

    def test_empty_policy_denies_every_action(self, mandate, state, clock) -> None:
        """A policy that configures nothing permits nothing."""
        empty = Policy.from_dict({"version": 1, "merchant": "empty"})
        result = decide(order(), mandate, state, empty, clock)
        assert result.verdict is Verdict.DENY
        assert result.check_id == "policy.action_disabled"


class TestDecisionQuality:
    def test_every_denial_names_the_check_that_fired(
        self, poisoned_order, mandate, state, policy, clock
    ) -> None:
        result = decide(poisoned_order, mandate, state, policy, clock)
        assert result.check_id
        assert result.reason
        assert result.evidence

    def test_decisions_carry_provenance_for_the_audit_log(
        self, legit_order, mandate, state, policy, clock
    ) -> None:
        result = decide(legit_order, mandate, state, policy, clock)
        assert result.mandate_id == mandate.mandate_id
        assert result.request_id == legit_order.request_id
        assert result.action == "create_order"

    def test_latency_is_measured_and_small(
        self, legit_order, mandate, state, policy, clock
    ) -> None:
        """The claim is 'deterministic code, not an LLM judge'. Prove the cost matches."""
        result = decide(legit_order, mandate, state, policy, clock)
        assert 0 <= result.latency_us < 40_000, "gate took longer than 40ms"

    def test_decision_is_json_serialisable(
        self, poisoned_order, mandate, state, policy, clock
    ) -> None:
        import json

        json.dumps(decide(poisoned_order, mandate, state, policy, clock).to_dict())


class TestNoModelInTheDecisionPath:
    """The architectural claim, enforced as a test rather than asserted in a README."""

    def test_gate_module_imports_no_llm_sdk(self) -> None:
        import ast
        import pathlib

        source = pathlib.Path("paynaka/gate.py").read_text(encoding="utf-8")
        tree = ast.parse(source)

        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])

        forbidden = {
            "anthropic",
            "openai",
            "langchain",
            "langgraph",
            "llama_index",
            "transformers",
            "litellm",
            "mcp",
            "requests",
            "httpx",
        }
        assert not (imported & forbidden), (
            f"paynaka/gate.py imports {sorted(imported & forbidden)}. The gate must decide "
            "with deterministic code and must not reach the network or a model."
        )

    def test_gate_makes_no_network_calls(self, legit_order, mandate, state, policy, clock) -> None:
        """Belt and braces: fail loudly if a socket is ever opened during a decision."""
        import socket

        original = socket.socket

        def forbidden(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            raise AssertionError("the gate opened a socket during a decision")

        socket.socket = forbidden  # type: ignore[assignment,misc]
        try:
            decide(legit_order, mandate, state, policy, clock)
        finally:
            socket.socket = original  # type: ignore[misc]
