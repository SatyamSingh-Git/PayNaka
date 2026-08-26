"""A merchant's decline must not destroy the shopper's authority.

``max_total`` is a budget, not a per-request ceiling, and the claim on it is taken *before*
the rail is called -- it has to be, or two concurrent requests both read the same remainder
and both fit inside it. The claim was never given back.

So a shopper who authorised ₹1,999, met a declining card, and tried again had a mandate
worth nothing. No money moved. Nothing anywhere explained it. The gate would say
``envelope.mandate_exhausted`` about a budget that had been spent on a payment that never
happened, and the honest reading of that message is a lie.

An audit reproduced it. The fix is the same rule the refundable-balance claim beside it
already followed, and the distinction it turns on is the one that matters on every money
path: a **definitive** refusal returns the claim, and a **timeout** does not, because there
the money may well have moved and a retry must not be able to spend it twice.
"""

from __future__ import annotations

import pytest

from paynaka.audit import AuditChain
from paynaka.clock import FrozenClock
from paynaka.engine import PayNaka
from paynaka.gate import LineItem, MoneyRequest
from paynaka.mandate import IntentMandate, MandateSigner, generate_keypair
from paynaka.policy import Policy
from paynaka.rails.base import RailDeclined, RailError
from paynaka.state import SqliteState

pytestmark = pytest.mark.adversarial

NOW = "2026-08-23 15:00"
BUDGET = 199_900
ATTA = "ATTA-5KG"
HOME = "addr_home"


class Rail:
    """A rail that fails the way it is told to, and counts how often it was asked."""

    def __init__(self, failure: Exception | None) -> None:
        self.failure = failure
        self.calls = 0

    def create_order(self, **kwargs: object) -> object:
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        raise AssertionError("this rail is only used for failures")


def stack(failure: Exception | None) -> tuple[PayNaka, object, IntentMandate, Rail]:
    clock = FrozenClock.at_ist(NOW)
    signer = MandateSigner(generate_keypair()[0])
    mandate = IntentMandate.create(
        clock=clock,
        subject="cust_kirana_001",
        session_id="sess_decline",
        max_total=BUDGET,
        allowed_skus=(ATTA,),
        allowed_destinations=(HOME,),
        max_qty_per_sku=3,
        allowed_actions=("create_order",),
    )
    state = SqliteState(":memory:", clock=clock)
    rail = Rail(failure)
    naka = PayNaka(
        rail=rail,  # type: ignore[arg-type]
        policy=Policy.from_yaml("policy.yaml"),
        state=state,
        audit=AuditChain(":memory:", clock=clock),
        verifier=signer.verifier(),
        clock=clock,
    )
    return naka, signer.sign(mandate), mandate, rail


def order(key: str, amount: int = BUDGET) -> MoneyRequest:
    return MoneyRequest(
        action="create_order",
        request_id=f"req_{key}",
        idempotency_key=key,
        items=(LineItem(sku=ATTA, qty=1, unit_paise=amount),),
        destination=HOME,
    )


class TestADeclineGivesTheBudgetBack:
    def test_the_mandate_is_not_exhausted_by_a_refusal(self) -> None:
        """The finding. One declining card used to end the shopping trip."""
        naka, signed, mandate, _ = stack(RailDeclined("card declined"))
        first = naka.execute(order("k1"), signed)
        assert not first.executed

        assert naka.state.mandate_spent(mandate.mandate_id) == 0, (
            "a decline consumed the shopper's authority; no money moved and the budget is gone"
        )
        assert naka.state.mandate_remaining(mandate.mandate_id, BUDGET) == BUDGET

    def test_the_next_attempt_reaches_the_rail(self) -> None:
        """The consequence a shopper would actually notice: retrying with another card.
        Checked at the rail, because a gate that refuses before dispatch looks identical
        from the outside to a rail that declined again."""
        naka, signed, _, rail = stack(RailDeclined("card declined"))
        naka.execute(order("k1"), signed)
        second = naka.execute(order("k2"), signed)

        assert rail.calls == 2, "the retry never got past the gate"
        assert second.decision.check_id != "envelope.mandate_exhausted"

    def test_the_budget_still_binds_after_a_decline(self) -> None:
        """The other direction, and the one that would make this fix dangerous if wrong.
        Releasing on a decline must not release anything else: a *successful* spend still
        has to hold its claim, or the release becomes a way to spend forever."""
        naka, signed, mandate, _ = stack(RailDeclined("card declined"))
        naka.execute(order("k1"), signed)

        naka.state.reserve_mandate_spend(mandate.mandate_id, "settled", BUDGET, BUDGET)
        assert naka.state.mandate_remaining(mandate.mandate_id, BUDGET) == 0

    def test_only_this_requests_claim_goes_back(self) -> None:
        """Keyed on the request. A decline must not hand back a sibling's authority."""
        naka, signed, mandate, _ = stack(RailDeclined("card declined"))
        naka.state.reserve_mandate_spend(mandate.mandate_id, "someone_else", 50_000, BUDGET)
        naka.execute(order("k1", amount=100_000), signed)

        assert naka.state.mandate_spent(mandate.mandate_id) == 50_000


class TestATimeoutKeepsIt:
    """The distinction the whole fix turns on."""

    def test_an_unknown_outcome_keeps_the_claim_held(self) -> None:
        """A timeout is not a decline. The money may have moved, and releasing would let a
        retry spend authority that is already gone."""
        naka, signed, mandate, _ = stack(RailError("gateway timed out"))
        result = naka.execute(order("k1"), signed)

        assert not result.executed
        assert naka.state.mandate_spent(mandate.mandate_id) == BUDGET, (
            "an unknown outcome released the budget; a retry can now spend it twice"
        )

    def test_a_retry_after_a_timeout_cannot_double_spend(self) -> None:
        naka, signed, _mandate, _ = stack(RailError("gateway timed out"))
        naka.execute(order("k1"), signed)
        second = naka.execute(order("k2"), signed)

        assert not second.executed
        assert second.decision.check_id == "envelope.mandate_exhausted"


class TestTheReleaseItself:
    def test_releasing_a_claim_that_is_not_there_is_not_an_error(self) -> None:
        """Idempotent. A double release, or a release after a replay already cleaned up,
        must not raise on a money path."""
        state = SqliteState(":memory:", clock=FrozenClock.at_ist(NOW))
        assert state.release_mandate_spend("mnd_x", "never_claimed") is False

    def test_releasing_returns_true_only_when_something_was_held(self) -> None:
        state = SqliteState(":memory:", clock=FrozenClock.at_ist(NOW))
        state.reserve_mandate_spend("mnd_x", "k", 1_000, 10_000)
        assert state.release_mandate_spend("mnd_x", "k") is True
        assert state.release_mandate_spend("mnd_x", "k") is False

    @pytest.mark.parametrize(("mandate_id", "key"), [("", "k"), ("m", ""), ("", "")])
    def test_an_incomplete_release_is_refused(self, mandate_id: str, key: str) -> None:
        from paynaka.state import StateError

        state = SqliteState(":memory:", clock=FrozenClock.at_ist(NOW))
        with pytest.raises(StateError):
            state.release_mandate_spend(mandate_id, key)

    def test_it_releases_one_mandate_and_not_another(self) -> None:
        state = SqliteState(":memory:", clock=FrozenClock.at_ist(NOW))
        state.reserve_mandate_spend("mnd_a", "k", 1_000, 10_000)
        state.reserve_mandate_spend("mnd_b", "k", 1_000, 10_000)
        state.release_mandate_spend("mnd_a", "k")

        assert state.mandate_spent("mnd_a") == 0
        assert state.mandate_spent("mnd_b") == 1_000
