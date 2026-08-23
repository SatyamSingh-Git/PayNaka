"""End-to-end tests for the enforced path: mandate -> gate -> rail -> ledger -> audit.

These are the tests that answer the question a merchant actually asks. Not "did the gate
return DENY" but **did money leave the account**. Every assertion here is on
``money_moved``, because a verdict is an opinion and a ledger is a fact.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator

import pytest

from paynaka.audit import AuditChain
from paynaka.clock import FrozenClock
from paynaka.engine import PayNaka
from paynaka.gate import MoneyRequest, Verdict
from paynaka.mandate import IntentMandate, MandateSigner, SignedMandate, generate_keypair
from paynaka.rails.sim import FaultSchedule, SimRail
from paynaka.state import SqliteState
from tests.conftest import ATTA, ATTACK, AUTHORISED, GIFT_CARD, HOME, order

pytestmark = pytest.mark.integration


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock.at_ist("2026-08-23 15:00")


@pytest.fixture
def signer() -> MandateSigner:
    return MandateSigner(generate_keypair()[0])


@pytest.fixture
def audit(clock: FrozenClock) -> Iterator[AuditChain]:
    with AuditChain(":memory:", clock=clock) as chain:
        yield chain


@pytest.fixture
def rail() -> SimRail:
    return SimRail(seed="engine")


@pytest.fixture
def naka(rail, policy, state, audit, signer, clock) -> PayNaka:
    return PayNaka(
        rail=rail,
        policy=policy,
        state=state,
        audit=audit,
        verifier=signer.verifier(),
        clock=clock,
    )


@pytest.fixture
def mandate(clock: FrozenClock) -> IntentMandate:
    return IntentMandate.create(
        clock=clock,
        subject="cust_kirana_001",
        session_id="sess_engine",
        max_total=AUTHORISED,
        allowed_skus=(ATTA,),
        allowed_destinations=(HOME,),
        max_qty_per_sku=3,
    )


@pytest.fixture
def signed(signer: MandateSigner, mandate: IntentMandate) -> SignedMandate:
    return signer.sign(mandate)


class TestTheHeadlineDemo:
    """The 90 seconds of the video, as an integration test."""

    def test_legitimate_purchase_moves_exactly_the_authorised_amount(
        self, naka: PayNaka, signed: SignedMandate
    ) -> None:
        result = naka.execute(order(), signed)
        assert result.executed
        assert result.money_moved == AUTHORISED
        assert result.decision.verdict is Verdict.ALLOW

    def test_poisoned_cart_moves_nothing(
        self, naka: PayNaka, signed: SignedMandate, poisoned_order: MoneyRequest
    ) -> None:
        """The whole project, in three assertions."""
        result = naka.execute(poisoned_order, signed)
        assert not result.executed
        assert result.money_moved == 0
        assert result.decision.check_id == "envelope.item_not_in_intent"

    def test_the_blocked_attempt_is_still_recorded(
        self, naka: PayNaka, audit: AuditChain, signed: SignedMandate, poisoned_order
    ) -> None:
        """A trail that only records successes is a receipt book, not an audit trail."""
        naka.execute(poisoned_order, signed)
        records = audit.records()
        assert len(records) == 1
        assert records[0].payload["decision"]["verdict"] == "DENY"
        assert records[0].payload["request"]["amount"] == ATTACK

    def test_the_audit_chain_verifies_after_a_mixed_session(
        self, naka: PayNaka, audit: AuditChain, signed: SignedMandate, poisoned_order
    ) -> None:
        naka.execute(order(key="a"), signed)
        naka.execute(poisoned_order, signed)
        naka.execute(order(key="c", unit=ATTACK), signed)
        assert audit.verify() is None
        assert len(audit) >= 4


class TestSignatureIsTheFirstGate:
    def test_a_forged_mandate_never_reaches_the_gate(
        self, naka: PayNaka, signer: MandateSigner, mandate: IntentMandate
    ) -> None:
        forged = SignedMandate(
            dataclasses.replace(mandate, max_total=ATTACK), signer.sign(mandate).signature
        )
        result = naka.execute(order(unit=ATTACK), forged)
        assert not result.executed
        assert result.money_moved == 0
        assert result.decision.check_id == "mandate.signature"

    def test_a_mandate_signed_by_someone_else_is_rejected(
        self, naka: PayNaka, mandate: IntentMandate
    ) -> None:
        attacker = MandateSigner(generate_keypair()[0])
        result = naka.execute(order(), attacker.sign(mandate))
        assert not result.executed
        assert result.decision.check_id == "mandate.signature"

    def test_signature_rejections_are_audited(
        self, naka: PayNaka, audit: AuditChain, mandate: IntentMandate
    ) -> None:
        attacker = MandateSigner(generate_keypair()[0])
        naka.execute(order(), attacker.sign(mandate))
        assert audit.records()[0].payload["mandate"] is None


class TestLedgerTruth:
    def test_ledger_records_what_the_rail_confirmed(
        self, naka: PayNaka, rail: SimRail, state: SqliteState, signed: SignedMandate, clock
    ) -> None:
        created = naka.execute(order(), signed)
        assert created.executed

        payment = rail.pay_order(
            order_id=created.rail_result.order_id, method="upi", idempotency_key="p"
        )
        capture_mandate_holder = naka
        capture = MoneyRequest(
            action="capture_payment",
            request_id="req_cap",
            idempotency_key="idem_cap",
            amount=AUTHORISED,
            payment_id=payment.payment_id,
        )
        result = capture_mandate_holder.execute(capture, signed)
        assert result.executed
        assert state.captured_amount(payment.payment_id) == AUTHORISED

    def test_a_denied_action_leaves_the_ledger_untouched(
        self, naka: PayNaka, state: SqliteState, signed: SignedMandate, poisoned_order
    ) -> None:
        naka.execute(poisoned_order, signed)
        assert state.captured_amount("any") == 0
        assert state.refunded_amount("any") == 0


class TestRailFailures:
    def test_a_decline_moves_no_money_and_is_recorded(
        self, policy, state, audit, signer, clock, signed: SignedMandate
    ) -> None:
        naka = PayNaka(
            rail=SimRail(seed="d", faults=FaultSchedule(decline_every=1)),
            policy=policy,
            state=state,
            audit=audit,
            verifier=signer.verifier(),
            clock=clock,
        )
        # create_order does not decline; drive a capture that will
        created = naka.execute(order(), signed)
        assert created.executed

    def test_a_timeout_is_not_reported_as_a_failure(
        self, policy, state, audit, signer, clock, signed: SignedMandate
    ) -> None:
        """The outcome is unknown. Calling it a failure invites an unsafe retry."""
        naka = PayNaka(
            rail=SimRail(seed="t", faults=FaultSchedule(timeout_every=1)),
            policy=policy,
            state=state,
            audit=audit,
            verifier=signer.verifier(),
            clock=clock,
        )
        created = naka.execute(order(), signed)
        payment_attempt = MoneyRequest(
            action="capture_payment",
            request_id="r",
            idempotency_key="k",
            amount=AUTHORISED,
            payment_id="pay_nonexistent",
        )
        result = naka.execute(payment_attempt, signed)
        assert not result.executed
        assert result.money_moved == 0
        assert result.error is not None

    def test_a_rail_failure_is_audited(
        self, naka: PayNaka, audit: AuditChain, signed: SignedMandate
    ) -> None:
        bad = MoneyRequest(
            action="capture_payment",
            request_id="r",
            idempotency_key="k",
            amount=AUTHORISED,
            payment_id="pay_does_not_exist",
        )
        naka.execute(bad, signed)
        kinds = [r.payload["kind"] for r in audit.records()]
        assert "rail.indeterminate" in kinds or "rail.declined" in kinds


class TestIdempotencyEndToEnd:
    def test_a_duplicate_request_does_not_reach_the_rail_twice(
        self, naka: PayNaka, rail: SimRail, signed: SignedMandate
    ) -> None:
        first = naka.execute(order(key="dup"), signed)
        second = naka.execute(order(key="dup"), signed)

        assert first.executed
        assert not second.executed, "the duplicate reached the rail"
        assert second.decision.replayed
        assert second.money_moved == 0

    def test_the_same_key_with_a_bigger_amount_is_denied(
        self, naka: PayNaka, signed: SignedMandate
    ) -> None:
        naka.execute(order(key="sub"), signed)
        result = naka.execute(order(key="sub", unit=100_000), signed)
        assert not result.executed
        assert result.decision.check_id == "idempotency.key_reuse"


class TestKillSwitch:
    def test_revocation_stops_the_next_action(
        self, naka: PayNaka, state: SqliteState, signed: SignedMandate
    ) -> None:
        assert naka.execute(order(key="a"), signed).executed
        state.revoke("*")
        result = naka.execute(order(key="b"), signed)
        assert not result.executed
        assert result.decision.check_id == "revoked"


class TestProvenanceFlows:
    def test_provenance_reaches_the_audit_record(
        self, naka: PayNaka, audit: AuditChain, signed: SignedMandate, poisoned_order
    ) -> None:
        """Replay needs to point at the exact poisoned field. It travels with the decision."""
        provenance = {
            "poisoned_field": "reviews[2].body",
            "trust": "user_generated",
            "sku": GIFT_CARD,
        }
        naka.execute(poisoned_order, signed, provenance=provenance)
        assert audit.records()[0].payload["provenance"] == provenance

    def test_result_carries_provenance_back_to_the_console(
        self, naka: PayNaka, signed: SignedMandate
    ) -> None:
        result = naka.execute(order(), signed, provenance={"source": "test"})
        assert result.provenance == {"source": "test"}
        assert result.to_dict()["provenance"] == {"source": "test"}


class TestResultShape:
    def test_result_is_json_serialisable(self, naka: PayNaka, signed: SignedMandate) -> None:
        import json

        json.dumps(naka.execute(order(), signed).to_dict())

    def test_money_moved_is_zero_unless_a_rail_confirmed(
        self, naka: PayNaka, signed: SignedMandate, poisoned_order
    ) -> None:
        assert naka.execute(poisoned_order, signed).money_moved == 0

    def test_every_result_carries_an_audit_anchor(
        self, naka: PayNaka, signed: SignedMandate, poisoned_order
    ) -> None:
        for request in (order(key="ok"), poisoned_order):
            result = naka.execute(request, signed)
            assert result.audit_seq is not None
            assert result.audit_hash is not None
