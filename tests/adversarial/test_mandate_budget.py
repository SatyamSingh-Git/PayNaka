"""Spending one authorisation more than once — the hole through the central claim.

An independent review found this, and it was the worst finding this project has had: the
thing the whole design promises did not hold. `max_total` was checked per request, so
`check_total` asked "does this request fit the budget?" and answered yes every time. One
signed mandate authorising ₹1,999 moved **₹5,997** across three `create_order` calls with
three fresh idempotency keys, every one of them ALLOW, and the nonce sat unspent.

The mistake underneath it is worth naming, because it is a common one: idempotency was
doing duty as a spending control. Idempotency stops *the same request* repeating. It has
never stopped a caller spending the same authority again under a new key — those are
different questions, and only one of them was being asked.

So `max_total` is now cumulative and claimed atomically, and this file is arranged around
the ways that could still be wrong:

* **Exhaustion** — the budget must actually run out, at the boundary, in one call or many.
* **Replay** — a retry must not be charged twice, or the fix becomes the mirror of the bug.
* **Concurrency** — two requests arriving together must not both fit into the same
  remainder, which is exactly how the refund ledger broke before it.
* **Isolation** — one mandate exhausting itself must not spend another's authority.
* **Ordering** — authority claimed by a request that is then refused is authority quietly
  burned, and a shopper whose budget drains on denials has been robbed by the defence.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from paynaka.audit import AuditChain
from paynaka.clock import FrozenClock
from paynaka.engine import PayNaka
from paynaka.gate import LineItem, MoneyRequest, Verdict
from paynaka.mandate import IntentMandate, MandateSigner, SignedMandate, generate_keypair
from paynaka.policy import Policy
from paynaka.rails.sim import SimRail
from paynaka.state import SqliteState

pytestmark = pytest.mark.adversarial

ATTA = "ATTA-5KG"
HOME = "addr_home"
UNIT = 199_900


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock.at_ist("2026-08-23 15:00")


@pytest.fixture
def signer() -> MandateSigner:
    return MandateSigner(generate_keypair()[0])


@pytest.fixture
def state(clock: FrozenClock) -> SqliteState:
    return SqliteState(":memory:", clock=clock)


@pytest.fixture
def naka(clock: FrozenClock, signer: MandateSigner, state: SqliteState) -> PayNaka:
    return PayNaka(
        rail=SimRail(seed="budget"),
        policy=Policy.from_yaml("policy.yaml"),
        state=state,
        audit=AuditChain(":memory:", clock=clock),
        verifier=signer.verifier(),
        clock=clock,
    )


def a_mandate(
    clock: FrozenClock, signer: MandateSigner, *, budget: int = UNIT, session: str = "sess_1"
) -> SignedMandate:
    return signer.sign(
        IntentMandate.create(
            clock=clock,
            subject="cust_1",
            session_id=session,
            max_total=budget,
            allowed_skus=(ATTA,),
            allowed_destinations=(HOME,),
            max_qty_per_sku=9,
            allowed_actions=("create_order",),
        )
    )


def order(key: str, *, qty: int = 1, unit: int = UNIT) -> MoneyRequest:
    return MoneyRequest(
        action="create_order",
        request_id=f"req_{key}",
        idempotency_key=key,
        items=(LineItem(sku=ATTA, qty=qty, unit_paise=unit),),
        currency="INR",
        destination=HOME,
    )


class TestOneAuthorisationIsSpentOnce:
    def test_the_reported_bug_verbatim(
        self, naka: PayNaka, clock: FrozenClock, signer: MandateSigner
    ) -> None:
        """Three orders, three fresh keys, one ₹1,999 mandate. Was ₹5,997."""
        signed = a_mandate(clock, signer)
        moved = sum(
            naka.execute(order(key), signed).money_moved for key in ("first", "second", "third")
        )
        assert moved == UNIT

    def test_the_second_purchase_says_why(
        self, naka: PayNaka, clock: FrozenClock, signer: MandateSigner
    ) -> None:
        signed = a_mandate(clock, signer)
        naka.execute(order("first"), signed)
        refused = naka.execute(order("second"), signed).decision
        assert refused.verdict is Verdict.DENY
        assert refused.check_id == "envelope.mandate_exhausted"
        assert refused.evidence["remaining"] == 0
        assert refused.evidence["already_spent"] == UNIT

    @pytest.mark.parametrize(
        ("budget", "orders", "expected"),
        [
            (UNIT, 1, UNIT),
            (UNIT, 5, UNIT),
            (2 * UNIT, 5, 2 * UNIT),
            (3 * UNIT, 2, 2 * UNIT),
            (UNIT - 1, 1, 0),
            (UNIT + 1, 2, UNIT),
        ],
        ids=[
            "exact",
            "one-budget-many-tries",
            "two-budgets",
            "under-budget",
            "a-paisa-short",
            "a-paisa-over",
        ],
    )
    def test_total_spend_never_exceeds_the_budget(
        self,
        naka: PayNaka,
        clock: FrozenClock,
        signer: MandateSigner,
        budget: int,
        orders: int,
        expected: int,
    ) -> None:
        """The boundary, probed from both sides. A paisa short buys nothing; a paisa over
        buys exactly one."""
        signed = a_mandate(clock, signer, budget=budget)
        moved = sum(naka.execute(order(f"k{i}"), signed).money_moved for i in range(orders))
        assert moved == expected
        assert moved <= budget

    def test_a_partial_spend_leaves_the_remainder_usable(
        self, naka: PayNaka, clock: FrozenClock, signer: MandateSigner
    ) -> None:
        """A budget is not a single-use token. Spending half must leave half."""
        signed = a_mandate(clock, signer, budget=3 * UNIT)
        assert naka.execute(order("a"), signed).money_moved == UNIT
        assert naka.execute(order("b"), signed).money_moved == UNIT
        assert naka.execute(order("c"), signed).money_moved == UNIT
        assert naka.execute(order("d"), signed).decision.check_id == "envelope.mandate_exhausted"

    def test_splitting_a_purchase_does_not_buy_more_than_was_authorised(
        self, naka: PayNaka, clock: FrozenClock, signer: MandateSigner
    ) -> None:
        """One basket of two, or two baskets of one, must cost the same authority.

        Priced under the merchant's ₹2,000 step-up band on purpose: this test is about the
        mandate budget, and a basket that trips step-up would be measuring the escalation
        path instead.
        """
        half = 99_900
        bulk = a_mandate(clock, signer, budget=2 * half, session="s_bulk")
        assert naka.execute(order("bulk", qty=2, unit=half), bulk).money_moved == 2 * half
        assert naka.execute(order("bulk_more", unit=half), bulk).decision.verdict is Verdict.DENY

        split = a_mandate(clock, signer, budget=2 * half, session="s_split")
        moved = sum(
            naka.execute(order(f"split{i}", unit=half), split).money_moved for i in range(4)
        )
        assert moved == 2 * half

    def test_the_merchant_cap_refuses_before_the_mandate_is_charged(
        self, naka: PayNaka, clock: FrozenClock, signer: MandateSigner
    ) -> None:
        """A basket over the merchant's per-action ceiling must not spend shopper authority
        on its way to being refused. The shopper's budget and the merchant's ceiling are
        independent bounds, and the tighter one wins without costing the other anything."""
        signed = a_mandate(clock, signer, budget=9 * UNIT, session="s_cap")
        refused = naka.execute(order("huge", qty=3), signed).decision
        assert refused.verdict is Verdict.DENY
        assert refused.check_id == "policy.max_amount"
        assert naka.state.mandate_spent(signed.mandate.mandate_id) == 0

    def test_a_request_waiting_on_a_human_holds_no_authority(
        self, naka: PayNaka, clock: FrozenClock, signer: MandateSigner
    ) -> None:
        """A step-up is not a spend. Reserving authority while an approver thinks would let
        an unanswered escalation quietly exhaust a shopper's budget -- the same reasoning
        that already stops a step-up claiming a refund balance."""
        signed = a_mandate(clock, signer, budget=9 * UNIT, session="s_stepup")
        stepped = naka.execute(order("big", qty=2), signed).decision
        assert stepped.verdict is Verdict.STEP_UP
        assert naka.state.mandate_spent(signed.mandate.mandate_id) == 0


class TestARetryIsNotASecondPurchase:
    def test_replaying_one_request_does_not_charge_twice(
        self, naka: PayNaka, clock: FrozenClock, signer: MandateSigner
    ) -> None:
        """The mirror of the bug. Fixing double-spend by charging every retry would be a
        worse defect than the one it replaced."""
        signed = a_mandate(clock, signer, budget=2 * UNIT)
        first = naka.execute(order("same"), signed)
        replay = naka.execute(order("same"), signed)
        assert first.decision.verdict is Verdict.ALLOW
        assert replay.decision.replayed is True
        assert naka.state.mandate_spent(signed.mandate.mandate_id) == UNIT

    def test_many_replays_still_cost_one_purchase(
        self, naka: PayNaka, clock: FrozenClock, signer: MandateSigner
    ) -> None:
        signed = a_mandate(clock, signer, budget=2 * UNIT)
        for _ in range(12):
            naka.execute(order("same"), signed)
        assert naka.state.mandate_spent(signed.mandate.mandate_id) == UNIT

    def test_a_refused_request_does_not_burn_authority(
        self, naka: PayNaka, clock: FrozenClock, signer: MandateSigner
    ) -> None:
        """Ordering, and it matters more than it looks. A shopper whose budget drains on
        denials has been robbed by the thing protecting them."""
        signed = a_mandate(clock, signer)
        naka.execute(order("bad", unit=5_000_000), signed)  # over budget, refused
        assert naka.state.mandate_spent(signed.mandate.mandate_id) == 0
        assert naka.execute(order("good"), signed).money_moved == UNIT

    @pytest.mark.parametrize(
        ("kwargs", "why"),
        [
            ({"unit": 5_000_000}, "over the budget"),
            ({"qty": 99}, "over the quantity ceiling"),
        ],
        ids=["too-expensive", "too-many"],
    )
    def test_no_refusal_reserves_anything(
        self,
        naka: PayNaka,
        clock: FrozenClock,
        signer: MandateSigner,
        kwargs: dict[str, int],
        why: str,
    ) -> None:
        signed = a_mandate(clock, signer)
        assert naka.execute(order("bad", **kwargs), signed).decision.verdict is Verdict.DENY
        assert naka.state.mandate_spent(signed.mandate.mandate_id) == 0, why


class TestConcurrentPurchasesCannotBothFit:
    def test_two_orders_arriving_together_produce_one_purchase(
        self, naka: PayNaka, clock: FrozenClock, signer: MandateSigner
    ) -> None:
        """The read-then-write window, which is how the refund ledger broke before it. Both
        callers see a full budget; only one may spend it."""
        signed = a_mandate(clock, signer)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda i: naka.execute(order(f"race{i}"), signed), range(8)))
        allowed = [r for r in results if r.decision.verdict is Verdict.ALLOW]
        assert len(allowed) == 1
        assert sum(r.money_moved for r in results) == UNIT

    def test_a_budget_for_three_admits_exactly_three(
        self, naka: PayNaka, clock: FrozenClock, signer: MandateSigner
    ) -> None:
        signed = a_mandate(clock, signer, budget=3 * UNIT)
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(lambda i: naka.execute(order(f"race{i}"), signed), range(12)))
        assert sum(r.money_moved for r in results) == 3 * UNIT


class TestMandatesDoNotShareAuthority:
    def test_exhausting_one_leaves_another_untouched(
        self, naka: PayNaka, clock: FrozenClock, signer: MandateSigner
    ) -> None:
        first = a_mandate(clock, signer, session="s_a")
        second = a_mandate(clock, signer, session="s_b")
        assert naka.execute(order("a"), first).money_moved == UNIT
        assert naka.execute(order("b"), first).decision.verdict is Verdict.DENY
        assert naka.execute(order("c"), second).money_moved == UNIT

    def test_the_same_idempotency_key_under_two_mandates_is_two_purchases(
        self, naka: PayNaka, clock: FrozenClock, signer: MandateSigner
    ) -> None:
        """The claim is keyed on (mandate, key). Keying on the key alone would make one
        mandate's retry silently satisfy another's purchase."""
        first = a_mandate(clock, signer, session="s_a")
        second = a_mandate(clock, signer, session="s_b")
        naka.execute(order("shared"), first)
        assert naka.state.mandate_spent(first.mandate.mandate_id) == UNIT
        assert naka.state.mandate_spent(second.mandate.mandate_id) == 0


class TestTheStatePrimitiveDirectly:
    @pytest.mark.parametrize("amount", [0, -1, -199_900])
    def test_a_nonpositive_claim_is_refused(self, state: SqliteState, amount: int) -> None:
        with pytest.raises(Exception, match="positive"):
            state.reserve_mandate_spend("mnd_1", "k", amount, 199_900)

    @pytest.mark.parametrize("amount", [True, False, 1999.0, "199900", None])
    def test_a_claim_that_is_not_int_paise_is_refused(
        self, state: SqliteState, amount: object
    ) -> None:
        with pytest.raises(Exception, match="int paise"):
            state.reserve_mandate_spend("mnd_1", "k", amount, 199_900)  # type: ignore[arg-type]

    @pytest.mark.parametrize(("mandate_id", "key"), [("", "k"), ("m", ""), ("", "")])
    def test_an_unattributable_claim_is_refused(
        self, state: SqliteState, mandate_id: str, key: str
    ) -> None:
        with pytest.raises(Exception, match="needs a mandate and a key"):
            state.reserve_mandate_spend(mandate_id, key, 100, 199_900)

    def test_remaining_never_reads_negative(self, state: SqliteState) -> None:
        """A breach must not present itself as headroom."""
        state.reserve_mandate_spend("mnd_1", "k", 199_900, 199_900)
        assert state.mandate_remaining("mnd_1", 100) == 0

    def test_an_unknown_mandate_has_spent_nothing(self, state: SqliteState) -> None:
        assert state.mandate_spent("mnd_never_seen") == 0
        assert state.mandate_remaining("mnd_never_seen", 199_900) == 199_900

    def test_a_zero_ceiling_admits_nothing(self, state: SqliteState) -> None:
        assert state.reserve_mandate_spend("mnd_1", "k", 1, 0) is False


class TestARetryCanRecoverWhatTheOriginalDid:
    """A timeout is not a decline. The client that retries must be able to find out what
    already happened, and must not be told it happened again.

    The first attempt at this fix set ``executed=True`` on a replay, which made twenty
    redeliveries sum to twenty payments -- and HAAT scores on that number, so a duplicate
    webhook would have inflated attack success. The existing tests caught it. The original
    outcome lives in its own field now, where it cannot be mistaken for money moving twice.
    """

    def test_the_replay_carries_the_original_order_id(
        self, naka: PayNaka, clock: FrozenClock, signer: MandateSigner
    ) -> None:
        signed = a_mandate(clock, signer)
        first = naka.execute(order("k"), signed)
        replay = naka.execute(order("k"), signed)
        assert replay.decision.replayed is True
        assert replay.original_result is not None
        assert replay.original_result.order_id == first.rail_result.order_id

    def test_the_replay_did_not_reach_the_rail(
        self, naka: PayNaka, clock: FrozenClock, signer: MandateSigner
    ) -> None:
        """`executed` means *this call* reached the rail. It did not."""
        signed = a_mandate(clock, signer)
        naka.execute(order("k"), signed)
        replay = naka.execute(order("k"), signed)
        assert replay.executed is False
        assert replay.rail_result is None

    def test_a_replay_moves_no_money_a_second_time(
        self, naka: PayNaka, clock: FrozenClock, signer: MandateSigner
    ) -> None:
        """The regression the first attempt introduced, asserted directly."""
        signed = a_mandate(clock, signer)
        naka.execute(order("k"), signed)
        total = sum(naka.execute(order("k"), signed).value_at_risk for _ in range(20))
        assert total == 0

    def test_a_replay_is_distinguishable_from_a_refusal(
        self, naka: PayNaka, clock: FrozenClock, signer: MandateSigner
    ) -> None:
        """The defect in one sentence: a caller checking `executed` could not tell "already
        done" from "refused", so it would reasonably try again with a new key."""
        signed = a_mandate(clock, signer, budget=2 * UNIT)
        naka.execute(order("k"), signed)
        replay = naka.execute(order("k"), signed)
        refused = naka.execute(order("bad", unit=5_000_000), signed)

        assert replay.original_result is not None
        assert refused.original_result is None
        assert replay.outcome != refused.outcome or replay.decision.replayed

    def test_a_request_with_no_idempotency_key_has_nothing_to_replay(
        self, naka: PayNaka, clock: FrozenClock, signer: MandateSigner
    ) -> None:
        signed = a_mandate(clock, signer)
        request = MoneyRequest(
            action="create_order",
            request_id="req_nokey",
            idempotency_key="",
            items=(LineItem(sku=ATTA, qty=1, unit_paise=UNIT),),
            currency="INR",
            destination=HOME,
        )
        assert naka.execute(request, signed).original_result is None

    def test_the_stored_result_survives_into_the_dict(
        self, naka: PayNaka, clock: FrozenClock, signer: MandateSigner
    ) -> None:
        """The console and the audit reader both go through `to_dict`."""
        signed = a_mandate(clock, signer)
        naka.execute(order("k"), signed)
        rendered = naka.execute(order("k"), signed).to_dict()
        assert rendered["original_result"]["order_id"]
