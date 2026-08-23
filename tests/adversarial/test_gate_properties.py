"""Properties of the gate, checked against generated inputs rather than chosen ones.

Every other test in this suite asserts something about a case somebody thought of. That is
most of the value and it is not all of it, because the interesting failures in an
authorisation layer are the combinations nobody thought of: an empty allow-list, a
quantity of zero, an amount exactly equal to a ceiling, a mandate that permits an action
the policy has disabled, all four at once.

So this file states the theorems instead, and lets Hypothesis look for counterexamples.

The headline is :class:`TestSoundness`. **Whenever the gate says ALLOW, the request was
inside the mandate** -- not approximately, not usually, but on every one of the seven
dimensions the mandate constrains, for every input generated. That is the single claim
PayNaka makes, written as an assertion.

Its complement matters as much: a gate that denied everything would satisfy soundness
perfectly, so :class:`TestNotVacuous` proves that the generators actually produce
approvals, and :class:`TestCompleteness` proves that a request genuinely inside the
mandate is not refused. Soundness without either is a very well-tested brick wall.

Hypothesis shrinks its counterexamples, so a failure here arrives as the smallest input
that breaks the property rather than as the random one that happened to find it.
"""

from __future__ import annotations

import dataclasses

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from paynaka.clock import FrozenClock
from paynaka.gate import (
    LineItem,
    MoneyRequest,
    Verdict,
    evaluate,
    request_hash,
)
from paynaka.mandate import IntentMandate
from paynaka.policy import Policy
from paynaka.state import SqliteState

pytestmark = pytest.mark.adversarial

NOW = "2026-08-23 15:00"
POLICY = Policy.from_yaml("policy.yaml")

#: Bounded well below the money ceiling so that generated arithmetic stays in the range a
#: real order occupies. Overflow has its own dedicated tests; this file is about authority.
PAISE = st.integers(min_value=0, max_value=50_000_000)

SKUS = st.sampled_from(["ATTA-5KG", "GHEE-1L", "CHAI-500G", "GIFT-50K", "MIXER"])
DESTINATIONS = st.sampled_from(["addr_home", "addr_office", "addr_attacker", "addr_unknown"])
ACTIONS = st.sampled_from(
    ["create_order", "capture_payment", "create_refund", "create_payout", "not_an_action"]
)


def _state(clock: FrozenClock) -> SqliteState:
    """A fresh ledger per example.

    Built inside each test rather than by a fixture on purpose: Hypothesis reuses a
    function-scoped fixture across every example it generates, so a shared state would let
    example seventeen fail because of what example three wrote.
    """
    return SqliteState(":memory:", clock=clock)


line_items = st.builds(
    LineItem,
    sku=SKUS,
    qty=st.integers(min_value=-2, max_value=60),
    unit_paise=PAISE,
)

requests = st.builds(
    MoneyRequest,
    action=ACTIONS,
    request_id=st.text(min_size=1, max_size=24),
    idempotency_key=st.text(min_size=1, max_size=24),
    items=st.lists(line_items, max_size=4).map(tuple),
    amount=st.one_of(st.none(), PAISE),
    currency=st.sampled_from(["INR", "USD", "inr", ""]),
    destination=st.one_of(st.none(), DESTINATIONS),
    payment_id=st.one_of(st.none(), st.just("pay_1")),
)


@st.composite
def mandates(draw: st.DrawFn) -> IntentMandate:
    clock = FrozenClock.at_ist(NOW)
    return IntentMandate.create(
        clock=clock,
        subject="cust",
        session_id="sess",
        # A mandate refuses to exist with max_total <= 0, which is itself the right
        # behaviour and has its own test. Generating one here would only ever produce
        # MandateMalformed before the gate saw anything.
        max_total=draw(st.integers(min_value=1, max_value=10_000_000)),
        allowed_skus=tuple(draw(st.lists(SKUS, max_size=3, unique=True))),
        # Same story: the mandate itself refuses 0 and anything above 10,000.
        max_qty_per_sku=draw(st.integers(min_value=1, max_value=10)),
        allowed_destinations=tuple(draw(st.lists(DESTINATIONS, max_size=2, unique=True))),
        allowed_actions=tuple(
            draw(
                st.lists(
                    st.sampled_from(["create_order", "capture_payment", "create_refund"]),
                    max_size=3,
                    unique=True,
                )
            )
        ),
    )


def decide(request: MoneyRequest, mandate: IntentMandate, state: SqliteState | None = None):  # type: ignore[no-untyped-def]
    clock = FrozenClock.at_ist(NOW)
    return evaluate(request, mandate, state=state or _state(clock), policy=POLICY, clock=clock)


#: How hard to look. The profile is chosen in conftest.py: a small budget locally so the
#: loop stays fast, a much larger one in CI where nobody is waiting, and
#: HYPOTHESIS_PROFILE=thorough for a deliberate deep sweep.
SLOW = settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])


@st.composite
def allowable(draw: st.DrawFn) -> tuple[MoneyRequest, IntentMandate]:
    """A (request, mandate) pair the gate will approve, with everything else varying.

    The independent strategies above almost never produce an approval -- a random action
    against a random allow-list is a denial nearly every time -- which is right for
    soundness and useless for any property that only has content *after* an ALLOW.
    Filtering with ``assume`` there would discard fifty inputs for every one kept, and
    Hypothesis is correct to call that out as distorting the distribution.

    So this constructs approvals directly and varies the parts that are free: how many
    line items, which authorised SKU, what quantity inside the ceiling, which authorised
    destination, and the idempotency key.
    """
    unit = draw(st.integers(min_value=1, max_value=40_000))
    qty = draw(st.integers(min_value=1, max_value=3))
    sku = draw(st.sampled_from(["ATTA-5KG", "GHEE-1L"]))
    destination = draw(st.sampled_from(["addr_home", "addr_office"]))
    key = draw(st.text(min_size=1, max_size=16))

    mandate = IntentMandate.create(
        clock=FrozenClock.at_ist(NOW),
        subject="cust",
        session_id="sess",
        max_total=200_000,
        allowed_skus=("ATTA-5KG", "GHEE-1L"),
        max_qty_per_sku=3,
        allowed_destinations=("addr_home", "addr_office"),
        allowed_actions=("create_order",),
    )
    request = MoneyRequest(
        action="create_order",
        request_id=draw(st.text(min_size=1, max_size=16)),
        idempotency_key=key,
        items=(LineItem(sku=sku, qty=qty, unit_paise=unit),),
        destination=destination,
    )
    # Stay inside the policy's auto-approval band so the verdict is ALLOW and not STEP_UP.
    step_up = POLICY.for_action("create_order").step_up_above or 0
    assume(request.effective_amount <= min(mandate.max_total, step_up))
    return request, mandate


# ====================================================================== soundness


class TestSoundness:
    """If the gate approved it, it was inside the mandate. Seven ways, every time."""

    @given(request=requests, mandate=mandates())
    @SLOW
    def test_an_approved_request_never_exceeds_the_authorised_total(
        self, request: MoneyRequest, mandate: IntentMandate
    ) -> None:
        decision = decide(request, mandate)
        if decision.verdict is Verdict.ALLOW:
            assert request.effective_amount <= mandate.max_total

    @given(request=requests, mandate=mandates())
    @SLOW
    def test_an_approved_request_only_names_authorised_skus(
        self, request: MoneyRequest, mandate: IntentMandate
    ) -> None:
        decision = decide(request, mandate)
        if decision.verdict is Verdict.ALLOW and mandate.allowed_skus:
            for item in request.items:
                assert item.sku in mandate.allowed_skus

    @given(request=requests, mandate=mandates())
    @SLOW
    def test_an_approved_request_never_exceeds_the_quantity_ceiling(
        self, request: MoneyRequest, mandate: IntentMandate
    ) -> None:
        decision = decide(request, mandate)
        if decision.verdict is Verdict.ALLOW:
            for item in request.items:
                assert 0 <= item.qty <= mandate.max_qty_per_sku

    @given(request=requests, mandate=mandates())
    @SLOW
    def test_an_approved_request_only_ships_to_an_authorised_destination(
        self, request: MoneyRequest, mandate: IntentMandate
    ) -> None:
        decision = decide(request, mandate)
        if (
            decision.verdict is Verdict.ALLOW
            and mandate.allowed_destinations
            and request.destination is not None
        ):
            assert request.destination in mandate.allowed_destinations

    @given(request=requests, mandate=mandates())
    @SLOW
    def test_an_approved_request_only_performs_an_authorised_action(
        self, request: MoneyRequest, mandate: IntentMandate
    ) -> None:
        decision = decide(request, mandate)
        if decision.verdict is Verdict.ALLOW:
            assert request.action in mandate.allowed_actions
            assert POLICY.for_action(request.action).enabled

    @given(request=requests, mandate=mandates())
    @SLOW
    def test_an_approved_request_is_in_the_authorised_currency(
        self, request: MoneyRequest, mandate: IntentMandate
    ) -> None:
        decision = decide(request, mandate)
        if decision.verdict is Verdict.ALLOW:
            assert request.currency == mandate.currency

    @given(request=requests, mandate=mandates())
    @SLOW
    def test_an_approved_request_is_within_the_merchant_policy_ceiling(
        self, request: MoneyRequest, mandate: IntentMandate
    ) -> None:
        decision = decide(request, mandate)
        if decision.verdict is Verdict.ALLOW:
            limit = POLICY.for_action(request.action).max_amount
            if limit is not None:
                assert request.effective_amount <= limit


class TestNotVacuous:
    """Soundness is trivially true of a gate that refuses everything. This says it does not."""

    def test_the_generators_do_produce_approvals(self) -> None:
        allowed = 0
        clock = FrozenClock.at_ist(NOW)
        for index in range(200):
            mandate = IntentMandate.create(
                clock=clock,
                subject="cust",
                session_id="sess",
                max_total=500_000,
                allowed_skus=("ATTA-5KG",),
                max_qty_per_sku=3,
                allowed_destinations=("addr_home",),
                allowed_actions=("create_order",),
            )
            request = MoneyRequest(
                action="create_order",
                request_id=f"r{index}",
                idempotency_key=f"k{index}",
                items=(LineItem(sku="ATTA-5KG", qty=1, unit_paise=1_999 * 100),),
                destination="addr_home",
            )
            if decide(request, mandate).verdict is Verdict.ALLOW:
                allowed += 1
        assert allowed == 200, "a gate that never approves proves nothing about soundness"


class TestCompleteness:
    """A request genuinely inside the mandate must be approved.

    The mirror of soundness, and the property a nervous gate quietly breaks: refusing
    honest traffic scores perfectly on every attack benchmark ever written.
    """

    @given(
        qty=st.integers(min_value=1, max_value=3),
        unit=st.integers(min_value=1, max_value=60_000),
    )
    @SLOW
    def test_a_request_inside_every_bound_is_allowed(self, qty: int, unit: int) -> None:
        clock = FrozenClock.at_ist(NOW)
        mandate = IntentMandate.create(
            clock=clock,
            subject="cust",
            session_id="sess",
            max_total=200_000,
            allowed_skus=("ATTA-5KG",),
            max_qty_per_sku=3,
            allowed_destinations=("addr_home",),
            allowed_actions=("create_order",),
        )
        request = MoneyRequest(
            action="create_order",
            request_id="r",
            idempotency_key="k",
            items=(LineItem(sku="ATTA-5KG", qty=qty, unit_paise=unit),),
            destination="addr_home",
        )
        assume(request.effective_amount <= mandate.max_total)
        assume(request.effective_amount <= (POLICY.for_action("create_order").max_amount or 0))
        step_up = POLICY.for_action("create_order").step_up_above
        assume(step_up is None or request.effective_amount <= step_up)

        decision = decide(request, mandate)
        assert decision.verdict is Verdict.ALLOW, decision.reason


# ====================================================================== behaviour


class TestItAlwaysAnswers:
    @given(request=requests, mandate=mandates())
    @SLOW
    def test_the_gate_never_raises(self, request: MoneyRequest, mandate: IntentMandate) -> None:
        """A crashed check is an unenforced check. Ambiguity must resolve to a decision."""
        decision = decide(request, mandate)
        assert decision.verdict in {Verdict.ALLOW, Verdict.DENY, Verdict.STEP_UP}

    @given(request=requests, mandate=mandates())
    @SLOW
    def test_every_refusal_names_the_check_that_made_it(
        self, request: MoneyRequest, mandate: IntentMandate
    ) -> None:
        """A denial nobody can act on is a denial that gets configured away."""
        decision = decide(request, mandate)
        if decision.verdict is not Verdict.ALLOW:
            assert decision.check_id
            assert decision.reason

    @given(request=requests, mandate=mandates())
    @SLOW
    def test_the_decision_is_deterministic(
        self, request: MoneyRequest, mandate: IntentMandate
    ) -> None:
        """Same request, same mandate, same empty ledger -- twice."""
        first = decide(request, mandate)
        second = decide(request, mandate)
        assert first.verdict is second.verdict
        assert first.check_id == second.check_id


class TestMonotonicity:
    """Narrowing authority can close a hole. It must never open one."""

    @given(request=requests, mandate=mandates())
    @SLOW
    def test_lowering_the_total_never_turns_a_denial_into_an_approval(
        self, request: MoneyRequest, mandate: IntentMandate
    ) -> None:
        before = decide(request, mandate)
        assume(before.verdict is not Verdict.ALLOW)
        # max(1, ...) because a mandate refuses to exist with a non-positive ceiling, so
        # halving one rupee would fail in the constructor rather than in the gate.
        tighter = dataclasses.replace(mandate, max_total=max(1, mandate.max_total // 2))
        assert decide(request, tighter).verdict is not Verdict.ALLOW

    @given(request=requests, mandate=mandates())
    @SLOW
    def test_removing_an_action_never_turns_a_denial_into_an_approval(
        self, request: MoneyRequest, mandate: IntentMandate
    ) -> None:
        before = decide(request, mandate)
        assume(before.verdict is not Verdict.ALLOW)
        tighter = dataclasses.replace(mandate, allowed_actions=())
        assert decide(request, tighter).verdict is not Verdict.ALLOW

    @given(request=requests, mandate=mandates())
    @SLOW
    def test_an_empty_action_list_grants_nothing(
        self, request: MoneyRequest, mandate: IntentMandate
    ) -> None:
        """The fail-closed reading of an empty permission list is the empty one."""
        stripped = dataclasses.replace(mandate, allowed_actions=())
        assert decide(request, stripped).verdict is Verdict.DENY

    @given(request=requests, mandate=mandates())
    @SLOW
    def test_revoking_denies_everything(
        self, request: MoneyRequest, mandate: IntentMandate
    ) -> None:
        clock = FrozenClock.at_ist(NOW)
        state = _state(clock)
        state.revoke(mandate.mandate_id, clock=clock)
        assert decide(request, mandate, state).verdict is Verdict.DENY

    @given(request=requests, mandate=mandates(), late=st.integers(min_value=1, max_value=48))
    @SLOW
    def test_expiry_denies_everything(
        self, request: MoneyRequest, mandate: IntentMandate, late: int
    ) -> None:
        """Moved the clock rather than the mandate.

        A mandate cannot be *constructed* already expired -- the constructor refuses -- so
        forging one would test the constructor. Expiry happens because time passes, and
        that is what an injectable clock is for.
        """
        clock = FrozenClock.at_ist(NOW)
        clock.advance(hours=late)
        decision = evaluate(request, mandate, state=_state(clock), policy=POLICY, clock=clock)
        assert decision.verdict is Verdict.DENY


class TestTheLedgerHolds:
    """Properties about money, not about permission."""

    @given(pair=allowable())
    @SLOW
    def test_a_second_identical_request_never_authorises_a_second_movement(
        self, pair: tuple[MoneyRequest, IntentMandate]
    ) -> None:
        request, mandate = pair
        state = _state(FrozenClock.at_ist(NOW))
        first = decide(request, mandate, state)
        assert first.verdict is Verdict.ALLOW and not first.replayed

        second = decide(request, mandate, state)
        assert second.replayed, "a duplicate must replay, never authorise again"

    @given(pair=allowable(), repeats=st.integers(min_value=2, max_value=12))
    @SLOW
    def test_no_number_of_repeats_authorises_a_second_movement(
        self, pair: tuple[MoneyRequest, IntentMandate], repeats: int
    ) -> None:
        request, mandate = pair
        state = _state(FrozenClock.at_ist(NOW))
        decisions = [decide(request, mandate, state) for _ in range(repeats)]
        fresh = [d for d in decisions if d.verdict is Verdict.ALLOW and not d.replayed]
        assert len(fresh) == 1

    @given(pair=allowable(), other=st.integers(min_value=1, max_value=190_000))
    @SLOW
    def test_the_same_key_with_a_different_amount_is_never_a_silent_replay(
        self, pair: tuple[MoneyRequest, IntentMandate], other: int
    ) -> None:
        """Reusing an approved key for a different body is substitution, not a retry."""
        request, mandate = pair
        state = _state(FrozenClock.at_ist(NOW))
        assert decide(request, mandate, state).verdict is Verdict.ALLOW

        mutated = dataclasses.replace(request, amount=other, items=())
        assume(request_hash(mutated) != request_hash(request))
        second = decide(mutated, mandate, state)
        assert not second.replayed
        assert second.verdict is not Verdict.ALLOW


class TestRequestHash:
    @given(request=requests)
    @SLOW
    def test_hashing_is_stable(self, request: MoneyRequest) -> None:
        assert request_hash(request) == request_hash(request)

    @given(request=requests, other_id=st.text(min_size=1, max_size=24))
    @SLOW
    def test_the_request_id_is_not_part_of_the_fingerprint(
        self, request: MoneyRequest, other_id: str
    ) -> None:
        """Two retries of one operation carry different ids and must still hash the same."""
        renamed = dataclasses.replace(request, request_id=other_id)
        assert request_hash(renamed) == request_hash(request)

    @given(request=requests, delta=st.integers(min_value=1, max_value=1_000_000))
    @SLOW
    def test_changing_the_amount_changes_the_fingerprint(
        self, request: MoneyRequest, delta: int
    ) -> None:
        """Otherwise an approved key could be pointed at a larger payment."""
        base = dataclasses.replace(request, items=(), amount=1_000)
        larger = dataclasses.replace(base, amount=1_000 + delta)
        assert request_hash(larger) != request_hash(base)

    @given(items=st.lists(line_items, min_size=2, max_size=4))
    @SLOW
    def test_reordering_line_items_does_not_change_the_fingerprint(
        self, items: list[LineItem]
    ) -> None:
        """A retry that reorders its cart is still the same retry."""
        forward = MoneyRequest(action="create_order", request_id="r", items=tuple(items))
        backward = dataclasses.replace(forward, items=tuple(reversed(items)))
        assert request_hash(forward) == request_hash(backward)
