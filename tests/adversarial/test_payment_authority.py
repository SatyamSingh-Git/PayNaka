"""Whose payment is this?

Capture and refund name a ``payment_id`` and nothing else. Every check on those paths was
arithmetic -- is the amount inside the captured balance, is there a return on record -- and
all of it was correct about the *amount* while never asking whose payment it was. An
independent audit found the consequence and stated it exactly: a fresh refund-capable
mandate could operate on any payment that had been entered into state.

The committed Razorpay evidence showed the same gap from the other side. The order and the
capture carry ``mnd_24ac...`` in Razorpay's own notes; the refund carries ``mnd_36a0...``.
Two mandates in one lifecycle, anchored publicly, with nothing anywhere objecting.

So the gate walks ``payment -> order -> mandate, subject`` before it looks at a balance.
The tests here are about the walk failing: a payment from nowhere, a payment belonging to
somebody else, a link that was never made. The legitimate path is covered by every other
refund test in the suite, all of which now have to carry their own paperwork.
"""

from __future__ import annotations

import pytest

from paynaka.clock import FrozenClock
from paynaka.gate import MoneyRequest, Verdict, evaluate
from paynaka.mandate import IntentMandate
from paynaka.policy import Policy
from paynaka.state import SqliteState

pytestmark = pytest.mark.adversarial


def decide(request, mandate, state, policy, clock):  # type: ignore[no-untyped-def]
    """Keyword-only in the gate; positional here, the same way the other gate tests
    spell it, so the assertions stay readable."""
    return evaluate(request, mandate, state=state, policy=policy, clock=clock)


NOW = "2026-08-23 15:00"
CAPTURED = 199_900


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock.at_ist(NOW)


@pytest.fixture
def state(clock: FrozenClock) -> SqliteState:
    return SqliteState(":memory:", clock=clock)


@pytest.fixture
def policy() -> Policy:
    return Policy.from_yaml("policy.yaml")


def mandate_for(subject: str, clock: FrozenClock) -> IntentMandate:
    return IntentMandate.create(
        clock=clock,
        subject=subject,
        session_id=f"sess_{subject}",
        max_total=CAPTURED,
        allowed_actions=("capture_payment", "create_refund"),
    )


def settle(state: SqliteState, mandate: IntentMandate, payment_id: str) -> None:
    """A whole lifecycle: an order under this mandate, a payment from it, a capture."""
    order_id = f"order_{payment_id}"
    state.record_order(
        order_id,
        mandate_id=mandate.mandate_id,
        subject=mandate.subject,
        session_id=mandate.session_id,
    )
    state.link_payment(payment_id, order_id)
    state.record_capture(payment_id, CAPTURED)
    state.record_return(payment_id)


def refund(payment_id: str, amount: int = 50_000) -> MoneyRequest:
    return MoneyRequest(
        action="create_refund",
        request_id="r",
        idempotency_key=f"k_{payment_id}",
        amount=amount,
        payment_id=payment_id,
    )


def capture(payment_id: str, amount: int = CAPTURED) -> MoneyRequest:
    return MoneyRequest(
        action="capture_payment",
        request_id="r",
        idempotency_key=f"kc_{payment_id}",
        amount=amount,
        payment_id=payment_id,
    )


class TestAPaymentFromNowhere:
    """The fail-closed reading of "we have never seen this"."""

    @pytest.mark.parametrize("build", [refund, capture], ids=["refund", "capture"])
    def test_an_unrecorded_payment_is_refused(self, build, state, policy, clock) -> None:  # type: ignore[no-untyped-def]
        mandate = mandate_for("cust_a", clock)
        result = decide(build("pay_from_thin_air"), mandate, state, policy, clock)
        assert result.verdict is Verdict.DENY
        assert result.check_id == "payment.unknown_origin"

    def test_a_payment_with_a_ledger_but_no_order_is_refused(self, state, policy, clock) -> None:  # type: ignore[no-untyped-def]
        """The exact shape the audit described: state seeded by hand. A capture on the
        ledger and a return on record make the arithmetic pass perfectly, and the payment
        still has no origin."""
        mandate = mandate_for("cust_a", clock)
        state.record_capture("pay_seeded", CAPTURED)
        state.record_return("pay_seeded")
        result = decide(refund("pay_seeded"), mandate, state, policy, clock)
        assert result.verdict is Verdict.DENY
        assert result.check_id == "payment.unknown_origin"

    def test_an_order_with_no_payment_linked_is_refused(self, state, policy, clock) -> None:  # type: ignore[no-untyped-def]
        """Half a chain is not a chain. The order exists; nothing says this payment came
        from it, and that link only ever arrives from the provider."""
        mandate = mandate_for("cust_a", clock)
        state.record_order(
            "order_1",
            mandate_id=mandate.mandate_id,
            subject=mandate.subject,
            session_id=mandate.session_id,
        )
        state.record_capture("pay_unlinked", CAPTURED)
        state.record_return("pay_unlinked")
        result = decide(refund("pay_unlinked"), mandate, state, policy, clock)
        assert result.check_id == "payment.unknown_origin"

    def test_a_payment_linked_to_an_order_nobody_recorded_is_refused(
        self, state, policy, clock
    ) -> None:  # type: ignore[no-untyped-def]
        """A dangling link. The join finds nothing, and nothing must read as no."""
        mandate = mandate_for("cust_a", clock)
        state.link_payment("pay_dangling", "order_that_was_never_recorded")
        state.record_capture("pay_dangling", CAPTURED)
        state.record_return("pay_dangling")
        assert (
            decide(refund("pay_dangling"), mandate, state, policy, clock).check_id
            == "payment.unknown_origin"
        )


class TestOneShoppersMandateCannotReachAnothersMoney:
    """The containment property, and the reason the check exists at all."""

    def test_a_refund_across_subjects_is_refused(self, state, policy, clock) -> None:  # type: ignore[no-untyped-def]
        owner = mandate_for("cust_a", clock)
        settle(state, owner, "pay_owned_by_a")

        stranger = mandate_for("cust_b", clock)
        result = decide(refund("pay_owned_by_a"), stranger, state, policy, clock)
        assert result.verdict is Verdict.DENY
        assert result.check_id == "payment.not_this_shopper"
        assert result.evidence["order_id"] == "order_pay_owned_by_a"

    def test_a_capture_across_subjects_is_refused(self, state, policy, clock) -> None:  # type: ignore[no-untyped-def]
        owner = mandate_for("cust_a", clock)
        settle(state, owner, "pay_owned_by_a")
        stranger = mandate_for("cust_b", clock)
        assert (
            decide(capture("pay_owned_by_a"), stranger, state, policy, clock).check_id
            == "payment.not_this_shopper"
        )

    def test_the_owner_is_still_allowed(self, state, policy, clock) -> None:  # type: ignore[no-untyped-def]
        """The other direction, and the one that matters for whether this ships. A check
        that refuses everybody is not a check."""
        owner = mandate_for("cust_a", clock)
        settle(state, owner, "pay_owned_by_a")
        assert decide(refund("pay_owned_by_a"), owner, state, policy, clock).allowed

    def test_a_later_mandate_for_the_same_shopper_may_refund(self, state, policy, clock) -> None:  # type: ignore[no-untyped-def]
        """Named because it is a deliberate design decision, not an oversight.

        Binding to `mandate_id` reads stronger and is wrong: a refund is a legitimate thing
        to do a week later, under a fresh mandate signed for exactly that purpose, long
        after the buying mandate expired. Refusing it means either refunds that cannot
        happen or purchase mandates kept alive for months. The subject is the containment
        property and it holds under both.
        """
        buying = mandate_for("cust_a", clock)
        settle(state, buying, "pay_owned_by_a")

        refunding = mandate_for("cust_a", clock)
        assert refunding.mandate_id != buying.mandate_id
        assert decide(refund("pay_owned_by_a"), refunding, state, policy, clock).allowed


class TestTheGraphCannotBeRewritten:
    def test_a_second_order_record_does_not_change_the_first(self, state, clock) -> None:  # type: ignore[no-untyped-def]
        """An order id is provider-assigned and unique, so a second write is a replay of
        the same fact. If it overwrote, an attacker who could reach state would move a
        payment under their own subject and refund it."""
        state.record_order("order_1", mandate_id="m1", subject="cust_a", session_id="s1")
        state.link_payment("pay_1", "order_1")
        state.record_order("order_1", mandate_id="m2", subject="cust_b", session_id="s2")

        authority = state.authority_for("pay_1")
        assert authority is not None
        assert authority.subject == "cust_a"
        assert authority.mandate_id == "m1"

    def test_a_payment_cannot_be_relinked_to_another_order(self, state, clock) -> None:  # type: ignore[no-untyped-def]
        state.record_order("order_a", mandate_id="m1", subject="cust_a", session_id="s")
        state.record_order("order_b", mandate_id="m2", subject="cust_b", session_id="s")
        state.link_payment("pay_1", "order_a")
        state.link_payment("pay_1", "order_b")

        authority = state.authority_for("pay_1")
        assert authority is not None
        assert authority.order_id == "order_a"

    @pytest.mark.parametrize(
        ("order_id", "mandate_id", "subject"),
        [
            ("", "m", "cust"),
            ("o", "", "cust"),
            ("o", "m", ""),
        ],
        ids=["no-order", "no-mandate", "no-subject"],
    )
    def test_an_incomplete_record_is_refused_at_the_door(
        self, state, order_id: str, mandate_id: str, subject: str
    ) -> None:  # type: ignore[no-untyped-def]
        """A half-written row would resolve to a payment owned by nobody, which the gate
        would then compare against a subject and refuse for the wrong reason."""
        from paynaka.state import StateError

        with pytest.raises(StateError):
            state.record_order(order_id, mandate_id=mandate_id, subject=subject, session_id="s")

    @pytest.mark.parametrize(("payment_id", "order_id"), [("", "o"), ("p", "")])
    def test_an_incomplete_link_is_refused(self, state, payment_id: str, order_id: str) -> None:  # type: ignore[no-untyped-def]
        from paynaka.state import StateError

        with pytest.raises(StateError):
            state.link_payment(payment_id, order_id)

    def test_an_empty_payment_id_resolves_to_nobody(self, state) -> None:  # type: ignore[no-untyped-def]
        assert state.authority_for("") is None


class TestTheCheckDefersWhereAClearerMessageExists:
    def test_a_refund_naming_no_payment_keeps_its_own_message(self, state, policy, clock) -> None:  # type: ignore[no-untyped-def]
        """Two check ids for one condition is a denial a reader has to look up twice."""
        mandate = mandate_for("cust_a", clock)
        request = MoneyRequest(
            action="create_refund", request_id="r", idempotency_key="k", amount=100
        )
        assert decide(request, mandate, state, policy, clock).check_id == "refund.no_payment"

    def test_a_capture_naming_no_payment_is_refused_here(self, state, policy, clock) -> None:  # type: ignore[no-untyped-def]
        """Capture had no such check of its own, so this one covers it."""
        mandate = mandate_for("cust_a", clock)
        request = MoneyRequest(
            action="capture_payment", request_id="r", idempotency_key="k", amount=100
        )
        assert decide(request, mandate, state, policy, clock).check_id == "capture.no_payment"

    def test_an_order_is_not_subject_to_this_check(self, state, policy, clock) -> None:  # type: ignore[no-untyped-def]
        """`create_order` names no payment because there is not one yet. Applying the walk
        to it would refuse every purchase ever made."""
        from tests.conftest import order

        mandate = IntentMandate.create(
            clock=clock,
            subject="cust_a",
            session_id="s",
            max_total=CAPTURED,
            allowed_skus=("ATTA-5KG",),
            allowed_destinations=("addr_home",),
            max_qty_per_sku=3,
            allowed_actions=("create_order",),
        )
        assert decide(order(), mandate, state, policy, clock).allowed
