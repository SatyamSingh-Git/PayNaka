"""Adversarial tests for the payment rails.

Two concerns. The simulator must be a faithful adversary rather than a permissive mock --
if it accepts a double capture or an over-refund that a real gateway would reject, the
benchmark measures a world that does not exist. And the Razorpay adapter must be
impossible to point at live money, which is checked by construction rather than by
convention.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from paynaka.rails import RailError, SimRail, build_rail
from paynaka.rails.base import RailDeclined
from paynaka.rails.razorpay_rail import RazorpayRail, _scrub
from paynaka.rails.sim import FaultSchedule

pytestmark = pytest.mark.adversarial


@pytest.fixture
def rail() -> SimRail:
    return SimRail(seed="test")


def captured(rail: SimRail, amount: int = 199_900, key: str = "k") -> str:
    order = rail.create_order(
        amount=amount, currency="INR", receipt="r", idempotency_key=f"{key}-o"
    )
    payment = rail.pay_order(order_id=order.order_id, method="upi", idempotency_key=f"{key}-p")
    rail.capture_payment(payment_id=payment.payment_id, amount=amount, idempotency_key=f"{key}-c")
    return payment.payment_id


class TestNoLiveMode:
    """The refusal that makes this repo safe to hand to a stranger."""

    @pytest.mark.parametrize(
        "key",
        [
            "rzp_live_abcd1234",
            "rzp_LIVE_abcd1234",
            "rzp_test",  # prefix without a body
            "rzp_test_",  # empty body
            "RZP_TEST_abcd1234",  # wrong case
            " rzp_test_abcd1234",  # leading space
            "rzp_test_abcd1234 ",  # trailing space
            "rzp_test_abcd1234\n",  # trailing newline -- \Z, not $
            "xrzp_test_abcd1234",  # prefix smuggling
            "rzp_test_abc$1234",  # non-alphanumeric body
        ],
    )
    def test_non_test_keys_are_refused(self, key: str, monkeypatch) -> None:
        monkeypatch.setenv("RAZORPAY_KEY_ID", key)
        monkeypatch.setenv("RAZORPAY_KEY_SECRET", "secret")
        with pytest.raises(RailError, match=r"test key|not installed"):
            RazorpayRail()

    def test_there_is_no_live_rail_to_build(self, monkeypatch) -> None:
        monkeypatch.setenv("PAYNAKA_RAIL", "live")
        with pytest.raises(RailError, match="no live rail"):
            build_rail()

    @pytest.mark.parametrize("name", ["prod", "production", "real", "SIM ULATOR", "sim2"])
    def test_unknown_rail_names_are_refused(self, name: str) -> None:
        with pytest.raises(RailError, match=r"unknown rail|no live rail"):
            build_rail(name)

    def test_an_empty_selector_falls_back_to_the_safe_default(self, monkeypatch) -> None:
        """Falling back is only acceptable because the fallback is the offline simulator.

        An empty PAYNAKA_RAIL resolving to "sim" costs a confusing config at worst. The
        same fallback resolving to a network rail would be a footgun, which is why sim is
        the default rather than merely one option among several.
        """
        monkeypatch.delenv("PAYNAKA_RAIL", raising=False)
        assert build_rail("").name == "sim"

    def test_sim_is_the_default(self, monkeypatch) -> None:
        """Reaching the network should be a choice, not what an unset variable does."""
        monkeypatch.delenv("PAYNAKA_RAIL", raising=False)
        assert build_rail().name == "sim"

    def test_missing_credentials_are_refused(self, monkeypatch) -> None:
        monkeypatch.delenv("RAZORPAY_KEY_ID", raising=False)
        monkeypatch.delenv("RAZORPAY_KEY_SECRET", raising=False)
        with pytest.raises(RailError, match=r"must be set|not installed"):
            RazorpayRail()


class TestErrorScrubbing:
    """Rail errors reach the audit log and the demo screen. Nothing sensitive may ride along."""

    @pytest.mark.parametrize(
        ("raw", "leaked"),
        [
            ("auth failed for rzp_test_ABC123xyz", "rzp_test_ABC123xyz"),
            ("key rzp_live_SECRET99 rejected", "rzp_live_SECRET99"),
            ("Authorization: Basic YWJjOmRlZg==", "YWJjOmRlZg=="),
            ("authorization: Bearer eyJhbGciOi.J9.sig", "eyJhbGciOi.J9.sig"),
            ("x-api-key: sk_abc123", "sk_abc123"),
            ("token = tok_live_9f8e7d", "tok_live_9f8e7d"),
            ("secret=hunter2 was wrong", "hunter2"),
            ("password: correct-horse", "correct-horse"),
        ],
    )
    def test_credentials_are_removed(self, raw: str, leaked: str) -> None:
        assert leaked not in _scrub(raw)

    def test_messages_are_truncated(self) -> None:
        """An unbounded provider message should not be able to flood the audit log."""
        assert len(_scrub("x" * 10_000)) <= 500


class TestSimIsAFaithfulAdversary:
    def test_double_capture_is_refused(self, rail: SimRail) -> None:
        payment = captured(rail)
        with pytest.raises(RailError, match="already captured"):
            rail.capture_payment(payment_id=payment, amount=199_900, idempotency_key="new")

    def test_capture_above_the_authorised_amount_is_refused(self, rail: SimRail) -> None:
        order = rail.create_order(amount=199_900, currency="INR", receipt="r", idempotency_key="o")
        payment = rail.pay_order(order_id=order.order_id, method="upi", idempotency_key="p")
        with pytest.raises(RailError, match="exceeds the authorised"):
            rail.capture_payment(
                payment_id=payment.payment_id, amount=5_200_000, idempotency_key="c"
            )

    def test_refund_beyond_capture_is_refused_by_the_rail_too(self, rail: SimRail) -> None:
        """A second, independent refusal, so a gate bug cannot become a real over-refund."""
        payment = captured(rail, 100_000)
        with pytest.raises(RailError, match="exceeds the"):
            rail.create_refund(payment_id=payment, amount=150_000, idempotency_key="r")

    def test_partial_refunds_accumulate_against_the_capture(self, rail: SimRail) -> None:
        payment = captured(rail, 100_000)
        rail.create_refund(payment_id=payment, amount=60_000, idempotency_key="r1")
        with pytest.raises(RailError, match="exceeds the 40000"):
            rail.create_refund(payment_id=payment, amount=60_000, idempotency_key="r2")

    def test_refunding_an_uncaptured_payment_is_refused(self, rail: SimRail) -> None:
        order = rail.create_order(amount=199_900, currency="INR", receipt="r", idempotency_key="o")
        payment = rail.pay_order(order_id=order.order_id, method="upi", idempotency_key="p")
        with pytest.raises(RailError, match="cannot refund"):
            rail.create_refund(payment_id=payment.payment_id, amount=100, idempotency_key="r")

    @pytest.mark.parametrize("amount", [0, -1, 1.5, "199900", True, 10**20])
    def test_bad_amounts_are_refused(self, rail: SimRail, amount: object) -> None:
        with pytest.raises(RailError):
            rail.create_order(
                amount=amount,  # type: ignore[arg-type]
                currency="INR",
                receipt="r",
                idempotency_key="o",
            )

    def test_unknown_order_and_payment_ids_are_refused(self, rail: SimRail) -> None:
        with pytest.raises(RailError, match="no such order"):
            rail.pay_order(order_id="order_nope", method="upi", idempotency_key="p")
        with pytest.raises(RailError, match="no such payment"):
            rail.fetch_payment("pay_nope")


class TestIdempotency:
    def test_repeating_a_key_returns_the_original_order(self, rail: SimRail) -> None:
        first = rail.create_order(
            amount=199_900, currency="INR", receipt="r", idempotency_key="same"
        )
        second = rail.create_order(
            amount=5_200_000, currency="INR", receipt="r", idempotency_key="same"
        )
        assert second.order_id == first.order_id
        assert second.amount == 199_900, "the replayed key returned the second amount"

    def test_repeating_a_refund_key_does_not_refund_twice(self, rail: SimRail) -> None:
        payment = captured(rail, 100_000)
        a = rail.create_refund(payment_id=payment, amount=50_000, idempotency_key="r")
        b = rail.create_refund(payment_id=payment, amount=50_000, idempotency_key="r")
        assert a.refund_id == b.refund_id
        # ...and the remaining balance moved only once
        rail.create_refund(payment_id=payment, amount=50_000, idempotency_key="r2")
        with pytest.raises(RailError):
            rail.create_refund(payment_id=payment, amount=1, idempotency_key="r3")

    @pytest.mark.parametrize("workers", [8, 24])
    def test_concurrent_same_key_orders_collapse_to_one(self, workers: int) -> None:
        rail = SimRail(seed="race")
        barrier = threading.Barrier(workers)

        def attempt(_: int) -> str:
            barrier.wait()
            return rail.create_order(
                amount=199_900, currency="INR", receipt="r", idempotency_key="shared"
            ).order_id

        with ThreadPoolExecutor(max_workers=workers) as pool:
            ids = set(pool.map(attempt, range(workers)))

        assert len(ids) == 1, f"{len(ids)} distinct orders created under one idempotency key"


class TestDeterminism:
    def test_two_rails_with_the_same_seed_mint_the_same_ids(self) -> None:
        """A benchmark whose transcripts differ run to run cannot show a regression."""
        a, b = SimRail(seed="fixed"), SimRail(seed="fixed")
        ids_a = [
            a.create_order(
                amount=1000, currency="INR", receipt="r", idempotency_key=str(i)
            ).order_id
            for i in range(5)
        ]
        ids_b = [
            b.create_order(
                amount=1000, currency="INR", receipt="r", idempotency_key=str(i)
            ).order_id
            for i in range(5)
        ]
        assert ids_a == ids_b

    def test_different_seeds_diverge(self) -> None:
        a = SimRail(seed="one").create_order(
            amount=1000, currency="INR", receipt="r", idempotency_key="k"
        )
        b = SimRail(seed="two").create_order(
            amount=1000, currency="INR", receipt="r", idempotency_key="k"
        )
        assert a.order_id != b.order_id

    def test_ids_look_like_razorpay_ids(self, rail: SimRail) -> None:
        """So nothing downstream quietly learns to parse a shape that is not real."""
        order = rail.create_order(amount=1000, currency="INR", receipt="r", idempotency_key="k")
        assert order.order_id.startswith("order_")
        assert len(order.order_id) == len("order_") + 14


class TestFaultInjection:
    def test_declines_are_scheduled_not_random(self) -> None:
        rail = SimRail(seed="s", faults=FaultSchedule(decline_every=3))
        outcomes = []
        for i in range(6):
            order = rail.create_order(
                amount=1000, currency="INR", receipt="r", idempotency_key=f"o{i}"
            )
            try:
                rail.pay_order(order_id=order.order_id, method="upi", idempotency_key=f"p{i}")
                outcomes.append("ok")
            except RailDeclined:
                outcomes.append("declined")
        assert outcomes == ["ok", "ok", "declined", "ok", "ok", "declined"]

    def test_timeouts_are_distinguishable_from_declines(self) -> None:
        """A decline is final; a timeout may have succeeded. They must not be one type."""
        rail = SimRail(seed="s", faults=FaultSchedule(timeout_every=1))
        order = rail.create_order(amount=1000, currency="INR", receipt="r", idempotency_key="o")
        with pytest.raises(RailError) as exc:
            rail.pay_order(order_id=order.order_id, method="upi", idempotency_key="p")
        assert not isinstance(exc.value, RailDeclined)
        assert "timed out" in str(exc.value)

    def test_duplicate_webhooks_are_emitted_on_schedule(self) -> None:
        """At-least-once delivery is normal gateway behaviour, not a simulated bug."""
        rail = SimRail(seed="s", faults=FaultSchedule(duplicate_webhook_every=1))
        order = rail.create_order(amount=1000, currency="INR", receipt="r", idempotency_key="o")
        rail.pay_order(order_id=order.order_id, method="upi", idempotency_key="p")
        events = rail.drain_webhooks()
        assert len(events) > 1
        assert events[0]["event"] == events[1]["event"]

    @pytest.mark.parametrize("value", [-1, 1.5, "3", True])
    def test_malformed_fault_schedules_are_refused(self, value: object) -> None:
        with pytest.raises(ValueError, match="non-negative int"):
            FaultSchedule(decline_every=value)  # type: ignore[arg-type]
