"""The delivery simulator itself. If the hazards are wrong, every scenario is theatre."""

from __future__ import annotations

import threading

import pytest

from chaos.delivery import (
    Delivery,
    Outcome,
    deliver_concurrently,
    deliver_in_order,
    duplicate_every,
    reorder_pairs,
    tamper_amount,
)


def d(event_id: str, *, amount: int = 100, attempt: int = 1) -> Delivery:
    return Delivery(
        event="refund.requested",
        event_id=event_id,
        payment_id="pay_1",
        amount=amount,
        attempt=attempt,
    )


def echo(delivery: Delivery) -> Outcome:
    return Outcome(delivery=delivery, acted=True, moved=delivery.amount)


# ====================================================================== the envelope


class TestDelivery:
    def test_redelivery_is_attempt_above_one(self) -> None:
        assert not d("e1").redelivery
        assert d("e1", attempt=2).redelivery

    @pytest.mark.parametrize(
        ("amount", "exc"),
        [
            (100.0, TypeError),  # a float is how paise become rounding errors
            ("100", TypeError),
            (True, TypeError),  # bool is an int subclass, and that has bitten people
            (-1, ValueError),
        ],
    )
    def test_amount_must_be_non_negative_int_paise(self, amount: object, exc: type) -> None:
        with pytest.raises(exc):
            Delivery(
                event="refund.requested",
                event_id="e1",
                payment_id="pay_1",
                amount=amount,  # type: ignore[arg-type]
            )

    @pytest.mark.parametrize("field", ["event", "event_id", "payment_id"])
    def test_identifying_fields_must_be_present(self, field: str) -> None:
        kwargs = {
            "event": "refund.requested",
            "event_id": "e1",
            "payment_id": "pay_1",
            "amount": 100,
        }
        kwargs[field] = ""
        with pytest.raises(ValueError, match=field):
            Delivery(**kwargs)  # type: ignore[arg-type]

    def test_zero_is_allowed_because_a_return_event_carries_no_amount(self) -> None:
        assert Delivery(event="return.received", event_id="e", payment_id="p", amount=0).amount == 0


# ====================================================================== hazards


class TestDuplicateEvery:
    def test_every_one_redelivers_everything(self) -> None:
        plan = duplicate_every([d("a"), d("b")], 1)
        assert [(x.event_id, x.attempt) for x in plan] == [("a", 1), ("a", 2), ("b", 1), ("b", 2)]

    def test_every_two_redelivers_the_second_only(self) -> None:
        plan = duplicate_every([d("a"), d("b"), d("c")], 2)
        assert [(x.event_id, x.attempt) for x in plan] == [
            ("a", 1),
            ("b", 1),
            ("b", 2),
            ("c", 1),
        ]

    @pytest.mark.parametrize("n", [0, -1, -100])
    def test_non_positive_n_is_a_no_op_rather_than_a_crash(self, n: int) -> None:
        assert duplicate_every([d("a")], n) == [d("a")]

    def test_the_original_plan_is_not_mutated(self) -> None:
        plan = [d("a")]
        duplicate_every(plan, 1)
        assert len(plan) == 1

    def test_attempt_increments_from_whatever_it_already_was(self) -> None:
        (first, second) = duplicate_every([d("a", attempt=3)], 1)
        assert (first.attempt, second.attempt) == (3, 4)


class TestReorderPairs:
    def test_adjacent_pairs_swap(self) -> None:
        assert [x.event_id for x in reorder_pairs([d("a"), d("b"), d("c"), d("e")])] == [
            "b",
            "a",
            "e",
            "c",
        ]

    def test_an_odd_tail_is_left_alone(self) -> None:
        assert [x.event_id for x in reorder_pairs([d("a"), d("b"), d("c")])] == ["b", "a", "c"]

    @pytest.mark.parametrize("size", [0, 1])
    def test_nothing_to_swap_is_not_an_error(self, size: int) -> None:
        plan = [d("a")][:size]
        assert reorder_pairs(plan) == plan


class TestTamperAmount:
    def test_the_last_matching_delivery_is_the_one_altered(self) -> None:
        plan = tamper_amount([d("a"), d("a", attempt=2)], event_id="a", to=999)
        assert [x.amount for x in plan] == [100, 999]

    def test_the_event_id_is_left_intact(self) -> None:
        (altered,) = tamper_amount([d("a")], event_id="a", to=999)
        assert altered.event_id == "a"
        assert altered.note

    def test_an_unknown_event_id_raises_rather_than_silently_doing_nothing(self) -> None:
        with pytest.raises(KeyError, match="nope"):
            tamper_amount([d("a")], event_id="nope", to=999)


# ====================================================================== transports


class TestDeliverInOrder:
    def test_order_is_preserved(self) -> None:
        out = deliver_in_order(echo, [d("a"), d("b"), d("c")])
        assert [o.delivery.event_id for o in out] == ["a", "b", "c"]


class TestDeliverConcurrently:
    def test_an_empty_plan_returns_nothing_and_does_not_hang(self) -> None:
        assert deliver_concurrently(echo, []) == []

    def test_results_come_back_in_plan_order_not_completion_order(self) -> None:
        # Position in the result list must track the plan, or a report attributes the
        # wrong outcome to the wrong delivery -- which is worse than no report.
        out = deliver_concurrently(echo, [d(f"e{i}") for i in range(8)])
        assert [o.delivery.event_id for o in out] == [f"e{i}" for i in range(8)]

    def test_a_single_delivery_needs_no_partner_to_arrive(self) -> None:
        assert len(deliver_concurrently(echo, [d("a")])) == 1

    def test_every_delivery_really_is_released_together(self) -> None:
        """The barrier is the whole value of this transport; assert it, do not assume it."""
        inside = threading.Barrier(4, timeout=5)
        seen: list[str] = []

        def handler(delivery: Delivery) -> Outcome:
            inside.wait()  # raises BrokenBarrierError if they are not concurrent
            seen.append(delivery.event_id)
            return echo(delivery)

        out = deliver_concurrently(handler, [d(f"e{i}") for i in range(4)])
        assert len(out) == 4
        assert sorted(seen) == ["e0", "e1", "e2", "e3"]
