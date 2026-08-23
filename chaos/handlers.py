"""Two webhook handlers. The difference between them is the whole point of the harness.

``NaiveHandler`` is **not a strawman.** It checks the payment exists, checks the amount is
still refundable, and keeps a ``seen`` set so a redelivery does not refund twice. That is
more care than a great many production webhook handlers get, and under the case everyone
tests -- one worker, deliveries in order -- it is correct.

It has three holes, and the striking thing is that none of them is visible in the code
that contains them:

- ``seen`` is read and then written. Two threads fit in the gap.
- ``seen`` lives in memory. A deploy forgets it, and the gateway is still retrying.
- the idempotency key carries the attempt number, so the *rail* cannot deduplicate
  either. This one is the most common of the three in real code: it comes from reading
  the key as "an id for this API call" instead of "an id for this business event", which
  is exactly backwards and reads perfectly sensibly until the day it does not.

``GatedHandler`` routes the same events through ``PayNaka.execute``. Its deduplication is
an ``INSERT`` against a ``PRIMARY KEY``, in the same SQLite file as the ledger, committed
in the same breath as the decision. There is no window, and there is no RAM to lose.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar

from chaos.delivery import Delivery, Outcome
from paynaka.clock import Clock
from paynaka.engine import PayNaka
from paynaka.gate import MoneyRequest
from paynaka.mandate import SignedMandate
from paynaka.rails.base import Rail, RailError, RefundResult
from paynaka.rails.sim import SimRail

__all__ = ["GatedHandler", "LossyRail", "NaiveHandler"]


# ====================================================================== the timeout


class LossyRail:
    """A rail that does the work and then loses the response.

    This is what a gateway timeout actually is, and the distinction matters enormously:
    the operation may well have *succeeded*. A caller who reads a timeout as a failure
    and retries has just asked for the money to move a second time.

    ``lose_first`` responses are swallowed after the underlying call has completed.
    Everything afterwards behaves normally.
    """

    def __init__(self, inner: SimRail, *, lose_first: int = 1) -> None:
        self._inner = inner
        self._remaining = lose_first
        self.name = f"{inner.name}+lossy"
        self.swallowed: list[str] = []

    def __getattr__(self, item: str) -> Any:
        # Everything except create_refund passes straight through to the real simulator.
        return getattr(self._inner, item)

    def create_refund(self, *, payment_id: str, amount: int, idempotency_key: str) -> RefundResult:
        result = self._inner.create_refund(
            payment_id=payment_id, amount=amount, idempotency_key=idempotency_key
        )
        if self._remaining > 0:
            self._remaining -= 1
            self.swallowed.append(idempotency_key)
            # The refund happened. The caller will never hear about it.
            raise RailError("gateway timed out; outcome unknown")
        return result


# ====================================================================== naive


def _nothing() -> None:
    """The default for the seam below. Present so the seam costs nothing in a real run."""


@dataclass
class NaiveHandler:
    """Read, decide, write. What a careful engineer writes on a Tuesday."""

    rail: Rail
    name: str = "naive"
    seen: set[str] = field(default_factory=set)
    moved: int = 0
    #: A seam the tests use to make the read-then-write window deterministic. The window
    #: itself is real and is open in every run; whether two threads happen to land inside
    #: it is the scheduler's business, and a test that depends on the scheduler being in
    #: a bad mood is not a test. Defaults to doing nothing.
    gap: Callable[[], None] = _nothing

    def restart(self) -> None:
        """Simulate a deploy. Everything the process remembered is gone."""
        self.seen = set()

    def handle(self, delivery: Delivery) -> Outcome:
        if delivery.event == "payment.captured":
            return self._capture(delivery)
        if delivery.event == "refund.requested":
            return self._refund(delivery)
        # A naive handler has no concept of a return entitlement, so this event is noise
        # to it. That absence is not laziness; it is what "the gateway is my source of
        # truth" costs, because the gateway does not know why a refund was owed.
        return Outcome(delivery=delivery, acted=False, reason="event ignored")

    def _capture(self, delivery: Delivery) -> Outcome:
        try:
            result = self.rail.capture_payment(
                payment_id=delivery.payment_id,
                amount=delivery.amount,
                idempotency_key=f"cap:{delivery.event_id}:{delivery.attempt}",
            )
        except RailError as exc:
            return Outcome(delivery=delivery, acted=False, error=str(exc))
        return Outcome(delivery=delivery, acted=True, reason=f"captured {result.amount}")

    def _refund(self, delivery: Delivery) -> Outcome:
        # The deduplication that works right up until it does not.
        if delivery.event_id in self.seen:
            return Outcome(delivery=delivery, acted=False, reason="already seen this event")
        self.gap()

        try:
            payment = self.rail.fetch_payment(delivery.payment_id)
        except RailError as exc:
            return Outcome(delivery=delivery, acted=False, error=str(exc))

        if payment.status != "captured":
            # The refund is dropped on the floor. Nobody is told, the customer is not
            # refunded, and no record survives saying anything was ever owed.
            return Outcome(
                delivery=delivery,
                acted=False,
                error=f"payment is {payment.status}, not captured",
            )

        remaining = int(payment.raw.get("captured", 0)) - int(payment.raw.get("refunded", 0))
        if delivery.amount > remaining:
            return Outcome(delivery=delivery, acted=False, error=f"only {remaining} refundable")

        try:
            result = self.rail.create_refund(
                payment_id=delivery.payment_id,
                amount=delivery.amount,
                # The bug that reads perfectly sensibly: the key names the attempt rather
                # than the business event, so the rail cannot help either.
                idempotency_key=f"rfnd:{delivery.event_id}:{delivery.attempt}",
            )
        except RailError as exc:
            # Not marked seen, deliberately. Returning a 5xx and letting the gateway
            # redeliver is the standard pattern and the correct one for a *decline* --
            # but a timeout is not a decline, and this is the line where that difference
            # turns into a second refund.
            return Outcome(delivery=delivery, acted=False, error=str(exc))

        self.seen.add(delivery.event_id)
        self.moved += result.amount
        return Outcome(
            delivery=delivery,
            acted=True,
            moved=result.amount,
            reason=f"refunded {result.amount}",
            detail={"refund_id": result.refund_id},
        )


# ====================================================================== gated


@dataclass
class GatedHandler:
    """The same events, through ``PayNaka.execute``."""

    naka: PayNaka
    signed: SignedMandate
    clock: Clock
    name: str = "paynaka"
    moved: int = 0

    _ACTIONS: ClassVar[dict[str, str]] = {
        "payment.captured": "capture_payment",
        "refund.requested": "create_refund",
    }

    def restart(self) -> None:
        """A deploy changes nothing here. State lives in SQLite, not in this object."""

    def handle(self, delivery: Delivery) -> Outcome:
        if delivery.event == "return.received":
            # Not a money movement, so not a gate question. It records the entitlement a
            # later refund is checked against.
            self.naka.state.record_return(delivery.payment_id, clock=self.clock)
            return Outcome(delivery=delivery, acted=False, reason="return recorded")

        action = self._ACTIONS.get(delivery.event)
        if action is None:
            return Outcome(delivery=delivery, acted=False, reason="event ignored")

        request = MoneyRequest(
            action=action,
            request_id=f"{delivery.event_id}#{delivery.attempt}",
            # Keyed on the business event, never on the attempt. This is the entire
            # difference: a redelivery of the same event carries the same key, so the
            # gate resolves it as a replay rather than as a second payment.
            idempotency_key=delivery.event_id,
            amount=delivery.amount,
            payment_id=delivery.payment_id,
        )
        result = self.naka.execute(
            request, self.signed, provenance={"source": "webhook", "attempt": delivery.attempt}
        )
        self.moved += result.money_moved

        return Outcome(
            delivery=delivery,
            acted=result.executed,
            moved=result.money_moved,
            reason=result.decision.reason,
            check_id=result.decision.check_id,
            error=result.error,
            detail={
                "verdict": str(result.decision.verdict),
                "replayed": result.decision.replayed,
                "audit_seq": result.audit_seq,
            },
        )
