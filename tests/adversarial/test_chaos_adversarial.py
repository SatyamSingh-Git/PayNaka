"""Hostile webhook delivery. Everything a motivated attacker does to a retry endpoint.

The scenarios in ``chaos/runner.py`` model *accidents* -- a gateway retrying, a deploy
landing badly. This file models somebody doing it on purpose, which is a different thing
even when the traffic looks identical: an accident stops on its own, and an attacker
keeps going until something says no.

The bar throughout is the same one the whole project uses: an attack succeeds when money
leaves beyond what was authorised, not when a gate returns DENY.
"""

from __future__ import annotations

import threading

import pytest

from chaos.delivery import Delivery, deliver_concurrently, deliver_in_order
from chaos.handlers import LossyRail, NaiveHandler
from chaos.runner import (
    CAPTURE,
    ENTITLED,
    _settle,
    capture,
    gated_stack,
    naive_stack,
    refund,
    returned,
)
from paynaka.rails.base import RailError
from paynaka.rails.sim import SimRail

pytestmark = pytest.mark.adversarial


# ====================================================================== replay


class TestReplayStorm:
    def test_fifty_redeliveries_move_the_money_exactly_once(self) -> None:
        """The gate is not a rate limiter. It does not need to be."""
        stack = gated_stack("adv:storm")
        _settle(stack)
        plan = [refund(stack.payment_id, attempt=n) for n in range(1, 51)]
        deliver_in_order(stack.handler.handle, plan)

        assert stack.refunded_on_the_rail() == ENTITLED
        assert stack.ledger_says() == ENTITLED

    def test_the_same_storm_delivered_all_at_once(self) -> None:
        """Sequential replay is caught by anything. Concurrent replay is the real test."""
        stack = gated_stack("adv:storm_concurrent")
        _settle(stack)
        plan = [refund(stack.payment_id, attempt=n) for n in range(1, 33)]
        deliver_concurrently(stack.handler.handle, plan)

        assert stack.refunded_on_the_rail() == ENTITLED

    def test_attempt_numbers_are_never_trusted(self) -> None:
        """A redelivery labelled ``attempt=1`` is still a redelivery.

        An attacker controls the body, so they control the attempt counter, and a handler
        that treats attempt 1 as "definitely the first time" has handed over the decision.
        """
        stack = gated_stack("adv:liar")
        _settle(stack)
        plan = [refund(stack.payment_id, attempt=1) for _ in range(6)]
        deliver_in_order(stack.handler.handle, plan)

        assert stack.refunded_on_the_rail() == ENTITLED


# ====================================================================== substitution


class TestKeyReuse:
    # After one Rs 499 refund against a Rs 1,999 capture, Rs 1,500 remains refundable.
    # Amounts inside that bound reach the idempotency check and are refused as key reuse;
    # amounts outside it are refused earlier, by the ledger. Both are denials and neither
    # moves money -- the parametrisation exists so the *ordering* is pinned rather than
    # assumed, because it is a deliberate decision and not an accident of the code.
    @pytest.mark.parametrize(
        ("mutated", "expected"),
        [
            (1, "idempotency.key_reuse"),  # one paise
            (ENTITLED + 1, "idempotency.key_reuse"),
            (ENTITLED - 1, "idempotency.key_reuse"),  # smaller, in case a bound is one-way
            (CAPTURE - ENTITLED, "idempotency.key_reuse"),  # exactly what remains
            (CAPTURE, "refund.exceeds_capture"),  # the whole order; the ledger gets there first
        ],
    )
    def test_any_change_of_amount_under_a_used_key_is_refused(
        self, mutated: int, expected: str
    ) -> None:
        stack = gated_stack(f"adv:reuse:{mutated}")
        _settle(stack)
        deliver_in_order(stack.handler.handle, [refund(stack.payment_id)])

        outcomes = deliver_in_order(
            stack.handler.handle, [refund(stack.payment_id, attempt=2, amount=mutated)]
        )
        assert outcomes[0].check_id == expected
        assert not outcomes[0].acted
        assert stack.refunded_on_the_rail() == ENTITLED

    def test_an_unauthorised_replay_is_reported_as_unauthorised_not_as_a_replay(self) -> None:
        """Idempotency resolves last among the DENY checks, and that ordering matters.

        A replayed request that was never permissible must be refused for the reason it
        was impermissible. Reporting it as a duplicate would file a substitution attempt
        under "routine gateway retry", which is the one place nobody looks.
        """
        stack = gated_stack("adv:order_of_reasons")
        _settle(stack)
        deliver_in_order(stack.handler.handle, [refund(stack.payment_id)])

        outcomes = deliver_in_order(
            stack.handler.handle,
            [refund(stack.payment_id, attempt=2, amount=CAPTURE * 4)],
        )
        # Four times the whole order is past the mandate's own ceiling, so the envelope
        # catches it before the ledger even gets a look -- one check earlier again. The
        # assertion that carries the meaning is the second one.
        assert outcomes[0].check_id == "envelope.total_exceeded"
        assert not str(outcomes[0].check_id).startswith("idempotency.")

    def test_a_used_key_cannot_be_pointed_at_a_different_payment(self) -> None:
        """The classic substitution: keep the approved key, change what it pays for."""
        stack = gated_stack("adv:substitute")
        _settle(stack)
        deliver_in_order(stack.handler.handle, [refund(stack.payment_id)])

        other = stack.rail.pay_order(
            order_id=stack.rail.create_order(
                amount=CAPTURE, currency="INR", receipt="r2", idempotency_key="setup:order2"
            ).order_id,
            method="upi",
            idempotency_key="setup:pay2",
        )
        swapped = Delivery(
            event="refund.requested",
            event_id="evt_rfnd_01",  # the key that was already approved
            payment_id=other.payment_id,  # pointed somewhere else entirely
            amount=ENTITLED,
            attempt=2,
        )
        outcomes = deliver_in_order(stack.handler.handle, [swapped])

        # Three independent reasons to say no now, and the strongest one answers first:
        # the substituted payment was never created under an order this checkpoint issued,
        # so it is refused on authority rather than on arithmetic. Nothing was captured on
        # it either, and the idempotency key was already spent. The one that matters is
        # that the money stayed put.
        assert outcomes[0].check_id == "payment.unknown_origin"
        assert not outcomes[0].acted
        assert stack.rail.fetch_payment(other.payment_id).raw.get("refunded", 0) == 0
        assert stack.refunded_on_the_rail() == ENTITLED


# ====================================================================== salami


class TestSlicing:
    """Twenty distinct return events on one payment. Idempotency has nothing to say --
    every key is different and every request is genuinely new -- so the refundable
    balance is the only thing standing between the attacker and the capture."""

    #: Rs 1,999 captured, Rs 499 a slice: four fit, the fifth does not, and the last
    #: Rs 3 is unreachable. Written out rather than derived, so the test does not agree
    #: with the implementation by doing the implementation's arithmetic.
    FITS = 4
    THROUGH = 199_600

    def _slices(self, payment_id: str, n: int = 20) -> list[Delivery]:
        return [
            Delivery(
                event="refund.requested",
                event_id=f"evt_slice_{i:03d}",
                payment_id=payment_id,
                amount=ENTITLED,
            )
            for i in range(n)
        ]

    def test_slices_stop_at_the_capture(self) -> None:
        stack = gated_stack("adv:salami")
        _settle(stack)
        outcomes = deliver_in_order(stack.handler.handle, self._slices(stack.payment_id))

        assert sum(1 for o in outcomes if o.acted) == self.FITS
        assert stack.refunded_on_the_rail() == self.THROUGH
        assert stack.ledger_says() == self.THROUGH

    def test_slices_delivered_all_at_once_stop_at_exactly_the_same_place(self) -> None:
        """The bound has to hold when twenty of them read the balance at one instant.

        This is the test that found the defect it now guards. Before the balance was
        claimed atomically, the gate approved all twenty and the *gateway* refused
        sixteen of them -- so the money was right, but only because Razorpay's own
        bound happened to agree with ours. A limit somebody else enforces on your behalf
        is not a limit you enforce, and it is not one you can put in a threat model.
        """
        stack = gated_stack("adv:salami_concurrent")
        _settle(stack)
        outcomes = deliver_concurrently(stack.handler.handle, self._slices(stack.payment_id))

        allowed = [o for o in outcomes if o.detail.get("verdict") == "ALLOW"]
        assert len(allowed) == self.FITS, "the gate approved more than could possibly fit"
        assert not [o for o in allowed if o.error], "the rail had to stop what the gate let by"
        assert stack.refunded_on_the_rail() == self.THROUGH
        assert stack.ledger_says() == self.THROUGH

    def test_the_concurrent_result_matches_the_sequential_one(self) -> None:
        """Same events, different timing, identical outcome. That is the whole claim."""
        one, many = gated_stack("adv:cmp_seq"), gated_stack("adv:cmp_conc")
        _settle(one)
        _settle(many)
        deliver_in_order(one.handler.handle, self._slices(one.payment_id))
        deliver_concurrently(many.handler.handle, self._slices(many.payment_id))

        assert one.refunded_on_the_rail() == many.refunded_on_the_rail()
        assert one.ledger_says() == many.ledger_says()

    def test_a_hundred_concurrent_slices_change_nothing(self) -> None:
        stack = gated_stack("adv:salami_100")
        _settle(stack)
        deliver_concurrently(stack.handler.handle, self._slices(stack.payment_id, 100))
        assert stack.refunded_on_the_rail() == self.THROUGH
        assert stack.refunded_on_the_rail() <= CAPTURE


# ====================================================================== ordering


class TestOrdering:
    def test_a_refund_before_any_capture_moves_nothing(self) -> None:
        stack = gated_stack("adv:early")
        deliver_in_order(stack.handler.handle, [returned(stack.payment_id)])
        outcomes = deliver_in_order(stack.handler.handle, [refund(stack.payment_id)])

        assert outcomes[0].check_id == "refund.exceeds_capture"
        assert stack.refunded_on_the_rail() == 0

    def test_a_refund_without_a_return_event_moves_nothing(self) -> None:
        """Policy says a refund needs a return on record. Skipping the event skips nothing."""
        stack = gated_stack("adv:no_return")
        deliver_in_order(stack.handler.handle, [capture(stack.payment_id)])
        outcomes = deliver_in_order(stack.handler.handle, [refund(stack.payment_id)])

        assert outcomes[0].check_id == "refund.no_return_on_record"
        assert stack.refunded_on_the_rail() == 0

    def test_the_whole_plan_reversed_still_moves_nothing_unauthorised(self) -> None:
        stack = gated_stack("adv:reversed")
        plan = [
            refund(stack.payment_id, attempt=1),
            returned(stack.payment_id),
            capture(stack.payment_id),
        ]
        deliver_in_order(stack.handler.handle, plan)
        assert stack.refunded_on_the_rail() == 0

    def test_a_duplicated_capture_captures_once(self) -> None:
        stack = gated_stack("adv:double_capture")
        deliver_in_order(
            stack.handler.handle,
            [capture(stack.payment_id), capture(stack.payment_id)],
        )
        assert stack.ledger_says() == 0
        assert stack.handler.naka.state.captured_amount(stack.payment_id) == CAPTURE


# ====================================================================== the harness


class TestLossyRail:
    def test_the_work_happens_before_the_response_is_lost(self) -> None:
        """A timeout that had not done the work would be a decline, and prove nothing."""
        inner = SimRail(seed="adv:lossy")
        order = inner.create_order(amount=CAPTURE, currency="INR", receipt="r", idempotency_key="o")
        payment = inner.pay_order(order_id=order.order_id, method="upi", idempotency_key="p")
        inner.capture_payment(payment_id=payment.payment_id, amount=CAPTURE, idempotency_key="c")

        lossy = LossyRail(inner, lose_first=1)
        with pytest.raises(RailError, match="outcome unknown"):
            lossy.create_refund(
                payment_id=payment.payment_id, amount=ENTITLED, idempotency_key="k1"
            )

        assert inner.fetch_payment(payment.payment_id).raw["refunded"] == ENTITLED

    def test_losses_are_bounded_and_then_it_behaves(self) -> None:
        inner = SimRail(seed="adv:lossy2")
        order = inner.create_order(amount=CAPTURE, currency="INR", receipt="r", idempotency_key="o")
        payment = inner.pay_order(order_id=order.order_id, method="upi", idempotency_key="p")
        inner.capture_payment(payment_id=payment.payment_id, amount=CAPTURE, idempotency_key="c")

        lossy = LossyRail(inner, lose_first=2)
        for n in (1, 2):
            with pytest.raises(RailError):
                lossy.create_refund(
                    payment_id=payment.payment_id, amount=1, idempotency_key=f"k{n}"
                )
        assert (
            lossy.create_refund(
                payment_id=payment.payment_id, amount=1, idempotency_key="k3"
            ).amount
            == 1
        )
        assert lossy.swallowed == ["k1", "k2"]

    def test_zero_losses_is_a_transparent_passthrough(self) -> None:
        inner = SimRail(seed="adv:lossy3")
        assert LossyRail(inner, lose_first=0).name.endswith("lossy")
        assert LossyRail(inner, lose_first=0).fetch_payment.__self__ is inner  # type: ignore[attr-defined]


class TestNaiveHandlerIsNotRigged:
    """If the comparison is to mean anything, the naive handler must be genuinely trying."""

    def test_it_survives_sequential_redelivery(self) -> None:
        stack = naive_stack("adv:fair")
        _settle(stack)
        deliver_in_order(
            stack.handler.handle,
            [refund(stack.payment_id, attempt=1), refund(stack.payment_id, attempt=2)],
        )
        assert stack.refunded_on_the_rail() == ENTITLED

    def test_it_refuses_a_refund_larger_than_the_capture(self) -> None:
        stack = naive_stack("adv:fair2")
        _settle(stack)
        outcomes = deliver_in_order(
            stack.handler.handle, [refund(stack.payment_id, amount=CAPTURE * 2)]
        )
        assert outcomes[0].error
        assert stack.refunded_on_the_rail() == 0

    def test_it_refuses_to_refund_an_uncaptured_payment(self) -> None:
        stack = naive_stack("adv:fair3")
        outcomes = deliver_in_order(stack.handler.handle, [refund(stack.payment_id)])
        assert outcomes[0].error
        assert stack.refunded_on_the_rail() == 0

    def test_the_gap_seam_defaults_to_doing_nothing(self) -> None:
        """A seam that costs anything in a normal run would make the comparison unfair."""
        handler = NaiveHandler(rail=SimRail(seed="adv:seam"))
        assert handler.gap() is None

    def test_the_race_it_loses_is_real_and_not_the_seam(self) -> None:
        """Without the seam the window is still there; it just does not open every time.

        Asserted as a bound rather than an equality: the point is that the naive handler
        can move more than once, not that a scheduler reliably makes it.
        """
        stack = naive_stack("adv:race")
        _settle(stack)
        inside = threading.Barrier(2, timeout=5)

        def hold() -> None:
            inside.wait()

        stack.handler.gap = hold  # type: ignore[union-attr]
        deliver_concurrently(
            stack.handler.handle,
            [refund(stack.payment_id, attempt=1), refund(stack.payment_id, attempt=2)],
        )
        assert stack.refunded_on_the_rail() == ENTITLED * 2
