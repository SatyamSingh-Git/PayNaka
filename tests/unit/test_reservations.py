"""The refundable-balance claim: the thing that makes the refund bound actually a bound.

Before this existed, ``check_refund_bounds`` read the balance and the ledger was written
later, with everything the rail does in between. Two refunds for the same payment that
arrive together both read the full balance and both proceed. It was found by pointing the
chaos harness at twenty concurrent refunds: the gate approved all twenty, and the payment
gateway refused sixteen. The money came out right and the enforcement was fiction.
"""

from __future__ import annotations

import pytest

from paynaka.clock import FrozenClock
from paynaka.state import SqliteState, StateError

NOW = "2026-08-23 15:00"


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock.at_ist(NOW)


@pytest.fixture
def state(clock: FrozenClock) -> SqliteState:
    s = SqliteState(":memory:", clock=clock)
    s.record_capture("pay_1", 100_000)
    return s


class TestReserving:
    def test_a_claim_inside_the_balance_succeeds(self, state: SqliteState) -> None:
        assert state.reserve_refund("k1", "pay_1", 60_000) is True
        assert state.held_amount("pay_1") == 60_000

    def test_a_held_claim_is_subtracted_from_what_is_refundable(self, state: SqliteState) -> None:
        state.reserve_refund("k1", "pay_1", 60_000)
        assert state.refundable_amount("pay_1") == 40_000

    def test_a_second_claim_may_only_take_what_is_left(self, state: SqliteState) -> None:
        assert state.reserve_refund("k1", "pay_1", 60_000) is True
        assert state.reserve_refund("k2", "pay_1", 40_001) is False
        assert state.reserve_refund("k2", "pay_1", 40_000) is True
        assert state.refundable_amount("pay_1") == 0

    def test_exactly_the_whole_balance_is_allowed(self, state: SqliteState) -> None:
        assert state.reserve_refund("k1", "pay_1", 100_000) is True

    def test_one_paise_over_is_not(self, state: SqliteState) -> None:
        assert state.reserve_refund("k1", "pay_1", 100_001) is False
        assert state.held_amount("pay_1") == 0

    def test_a_payment_with_no_capture_has_nothing_to_claim(self, state: SqliteState) -> None:
        assert state.reserve_refund("k1", "pay_unknown", 1) is False

    def test_claims_on_different_payments_do_not_interfere(self, state: SqliteState) -> None:
        state.record_capture("pay_2", 500)
        state.reserve_refund("k1", "pay_1", 100_000)
        assert state.reserve_refund("k2", "pay_2", 500) is True
        assert state.held_amount("pay_1") == 100_000
        assert state.held_amount("pay_2") == 500

    def test_a_repeated_key_is_refused_rather_than_raising(self, state: SqliteState) -> None:
        """The caller's question is "may I proceed". Both answers of no look the same."""
        assert state.reserve_refund("k1", "pay_1", 10) is True
        assert state.reserve_refund("k1", "pay_1", 10) is False
        assert state.held_amount("pay_1") == 10

    @pytest.mark.parametrize("amount", [0, -1, -100_000])
    def test_a_non_positive_claim_is_rejected(self, state: SqliteState, amount: int) -> None:
        with pytest.raises(StateError, match="positive"):
            state.reserve_refund("k1", "pay_1", amount)

    @pytest.mark.parametrize("amount", [1.5, "100", True, None])
    def test_only_int_paise_may_be_claimed(self, state: SqliteState, amount: object) -> None:
        with pytest.raises(StateError, match="int paise"):
            state.reserve_refund("k1", "pay_1", amount)  # type: ignore[arg-type]

    @pytest.mark.parametrize(("key", "payment"), [("", "pay_1"), ("k", "")])
    def test_a_claim_needs_a_key_and_a_payment(
        self, state: SqliteState, key: str, payment: str
    ) -> None:
        with pytest.raises(StateError, match="key and a payment"):
            state.reserve_refund(key, payment, 10)


class TestSettling:
    def test_settling_writes_the_ledger_and_frees_the_claim(self, state: SqliteState) -> None:
        state.reserve_refund("k1", "pay_1", 60_000)
        state.settle_reservation("k1", 60_000)

        assert state.refunded_amount("pay_1") == 60_000
        assert state.held_amount("pay_1") == 0
        assert state.refundable_amount("pay_1") == 40_000
        assert state.reservation_state("k1") == "settled"

    def test_a_partial_settlement_gives_the_remainder_back(self, state: SqliteState) -> None:
        """Rails do partial refunds, and the difference must not stay claimed forever."""
        state.reserve_refund("k1", "pay_1", 60_000)
        state.settle_reservation("k1", 25_000)

        assert state.refunded_amount("pay_1") == 25_000
        assert state.refundable_amount("pay_1") == 75_000

    def test_settling_at_zero_writes_no_ledger_entry(self, state: SqliteState) -> None:
        state.reserve_refund("k1", "pay_1", 60_000)
        state.settle_reservation("k1", 0)

        assert state.refunded_amount("pay_1") == 0
        assert state.refundable_amount("pay_1") == 100_000

    def test_settling_above_the_claim_is_refused(self, state: SqliteState) -> None:
        """Otherwise the claim is decoration and the hole it closed reopens."""
        state.reserve_refund("k1", "pay_1", 10_000)
        with pytest.raises(StateError, match="cannot settle"):
            state.settle_reservation("k1", 10_001)
        assert state.refunded_amount("pay_1") == 0

    def test_settling_an_unknown_claim_raises(self, state: SqliteState) -> None:
        with pytest.raises(StateError, match="no reservation"):
            state.settle_reservation("nope", 1)

    def test_a_claim_cannot_be_settled_twice(self, state: SqliteState) -> None:
        state.reserve_refund("k1", "pay_1", 10_000)
        state.settle_reservation("k1", 10_000)
        with pytest.raises(StateError, match="already settled"):
            state.settle_reservation("k1", 10_000)
        assert state.refunded_amount("pay_1") == 10_000

    def test_a_released_claim_cannot_then_be_settled(self, state: SqliteState) -> None:
        state.reserve_refund("k1", "pay_1", 10_000)
        state.release_reservation("k1")
        with pytest.raises(StateError, match="already released"):
            state.settle_reservation("k1", 10_000)

    @pytest.mark.parametrize("confirmed", [1.0, "100", True])
    def test_only_int_paise_may_be_settled(self, state: SqliteState, confirmed: object) -> None:
        state.reserve_refund("k1", "pay_1", 10_000)
        with pytest.raises(StateError, match="int paise"):
            state.settle_reservation("k1", confirmed)  # type: ignore[arg-type]


class TestReleasing:
    def test_releasing_returns_the_balance(self, state: SqliteState) -> None:
        state.reserve_refund("k1", "pay_1", 60_000)
        assert state.release_reservation("k1") is True
        assert state.refundable_amount("pay_1") == 100_000
        assert state.reservation_state("k1") == "released"

    def test_releasing_an_unknown_claim_is_false_not_an_error(self, state: SqliteState) -> None:
        assert state.release_reservation("nope") is False

    def test_releasing_twice_only_works_once(self, state: SqliteState) -> None:
        state.reserve_refund("k1", "pay_1", 10)
        assert state.release_reservation("k1") is True
        assert state.release_reservation("k1") is False

    def test_a_settled_claim_cannot_be_released_back_into_the_balance(
        self, state: SqliteState
    ) -> None:
        """Releasing a settled claim would hand back money that has already left."""
        state.reserve_refund("k1", "pay_1", 10_000)
        state.settle_reservation("k1", 10_000)
        assert state.release_reservation("k1") is False
        assert state.refundable_amount("pay_1") == 90_000


class TestReconciliation:
    def test_unresolved_claims_are_listed_in_the_order_they_were_taken(
        self, state: SqliteState
    ) -> None:
        state.reserve_refund("k1", "pay_1", 10)
        state.reserve_refund("k2", "pay_1", 20)
        state.reserve_refund("k3", "pay_1", 30)
        state.settle_reservation("k2", 20)
        state.release_reservation("k3")

        assert state.unresolved_reservations() == [("k1", "pay_1", 10)]

    def test_nothing_unresolved_is_an_empty_list(self, state: SqliteState) -> None:
        assert state.unresolved_reservations() == []

    def test_an_unknown_key_has_no_state(self, state: SqliteState) -> None:
        assert state.reservation_state("never") is None
