"""A deterministic payment simulator.

Not a mock. A mock returns whatever the test told it to; this reproduces the parts of a
real gateway's behaviour that PayNaka has to survive, and it does so identically on every
run given the same seed:

- ids that look like Razorpay's, so nothing downstream learns to parse a fake format
- idempotency, so the same key returns the same result rather than a second charge
- captures bounded by the authorised amount
- refunds bounded by what was actually captured
- deterministic, seeded failures, so "one in twenty payments fails" is reproducible
- an injectable fault schedule, which is what the chaos harness drives

One distinction is made carefully throughout, because getting it wrong poisons the audit
chain: **every definitive refusal raises ``RailDeclined``, and only a lost response raises
``RailError``.** Refusing to refund more than was captured is a decision the gateway made
and stands by; a timeout is the gateway declining to say. The engine records the first as
settled and the second as *outcome unknown*, and a rail that files a firm "no" under the
second teaches reconciliation to chase money that never moved.

The point is that a benchmark of several thousand agent runs must be reproducible and
must not depend on a sandbox being up, while the demo still runs against the real thing.
"""

from __future__ import annotations

import hashlib
import itertools
import threading
from dataclasses import dataclass
from typing import Any, Final

from paynaka.money import MAX_PAISE
from paynaka.rails.base import (
    OrderResult,
    PaymentResult,
    RailDeclined,
    RailError,
    RefundResult,
)

__all__ = ["FaultSchedule", "SimRail"]

_ALPHABET: Final[str] = "abcdefghijklmnopqrstuvwxyz0123456789"


@dataclass
class FaultSchedule:
    """Deterministic faults the chaos harness injects.

    ``decline_every`` of 5 means every fifth payment is declined -- not a random one in
    five, which would make a failing benchmark run impossible to reproduce.
    """

    decline_every: int = 0
    timeout_every: int = 0
    duplicate_webhook_every: int = 0

    def __post_init__(self) -> None:
        for name in ("decline_every", "timeout_every", "duplicate_webhook_every"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative int")


@dataclass
class _Order:
    order_id: str
    amount: int
    currency: str
    receipt: str
    status: str = "created"


@dataclass
class _Payment:
    payment_id: str
    order_id: str
    amount: int
    currency: str
    status: str
    method: str
    captured_amount: int = 0
    refunded_amount: int = 0


class SimRail:
    """An in-process gateway. Thread-safe, deterministic, and offline."""

    name = "sim"

    def __init__(self, *, seed: str = "paynaka", faults: FaultSchedule | None = None) -> None:
        self._seed = seed
        self._faults = faults or FaultSchedule()
        self._lock = threading.RLock()
        self._counter = itertools.count(1)
        self._orders: dict[str, _Order] = {}
        self._payments: dict[str, _Payment] = {}
        self._refunds: dict[str, RefundResult] = {}
        self._idempotency: dict[str, Any] = {}
        self._attempts = 0
        self.webhooks: list[dict[str, Any]] = []

    # ---------------------------------------------------------------- ids
    def _mint(self, prefix: str) -> str:
        """A stable pseudo-random id derived from the seed and a counter.

        Derived rather than random so two runs of the same benchmark produce byte-identical
        transcripts, which is what makes a regression in HAAT results legible.
        """
        n = next(self._counter)
        digest = hashlib.sha256(f"{self._seed}:{prefix}:{n}".encode()).digest()
        tail = "".join(_ALPHABET[b % len(_ALPHABET)] for b in digest[:14])
        return f"{prefix}_{tail}"

    # ---------------------------------------------------------------- idempotency
    def _replay(self, key: str) -> Any | None:
        return self._idempotency.get(key)

    def _remember(self, key: str, value: Any) -> Any:
        self._idempotency[key] = value
        return value

    # ---------------------------------------------------------------- operations
    def create_order(
        self, *, amount: int, currency: str, receipt: str, idempotency_key: str
    ) -> OrderResult:
        _require_amount(amount)
        with self._lock:
            cached = self._replay(idempotency_key)
            if isinstance(cached, OrderResult):
                return cached

            order = _Order(
                order_id=self._mint("order"), amount=amount, currency=currency, receipt=receipt
            )
            self._orders[order.order_id] = order
            result = OrderResult(
                order_id=order.order_id,
                amount=amount,
                currency=currency,
                status="created",
                receipt=receipt,
                raw={"entity": "order", "attempts": 0},
            )
            return self._remember(idempotency_key, result)  # type: ignore[no-any-return]

    def pay_order(
        self, *, order_id: str, method: str, idempotency_key: str, succeed: bool = True
    ) -> PaymentResult:
        with self._lock:
            cached = self._replay(idempotency_key)
            if isinstance(cached, PaymentResult):
                return cached

            order = self._orders.get(order_id)
            if order is None:
                raise RailDeclined(f"no such order: {order_id}")

            self._attempts += 1
            if self._faults.timeout_every and self._attempts % self._faults.timeout_every == 0:
                raise RailError("gateway timed out; outcome unknown")

            declined = not succeed or (
                self._faults.decline_every and self._attempts % self._faults.decline_every == 0
            )

            payment = _Payment(
                payment_id=self._mint("pay"),
                order_id=order_id,
                amount=order.amount,
                currency=order.currency,
                status="failed" if declined else "authorized",
                method=method,
            )
            self._payments[payment.payment_id] = payment

            result = PaymentResult(
                payment_id=payment.payment_id,
                order_id=order_id,
                amount=payment.amount,
                currency=payment.currency,
                status=payment.status,
                method=method,
                raw={"entity": "payment"},
            )
            self._remember(idempotency_key, result)
            self._emit("payment.failed" if declined else "payment.authorized", result)

            if declined:
                raise RailDeclined(f"payment {payment.payment_id} was declined")
            return result

    def capture_payment(
        self, *, payment_id: str, amount: int, idempotency_key: str
    ) -> PaymentResult:
        _require_amount(amount)
        with self._lock:
            cached = self._replay(idempotency_key)
            if isinstance(cached, PaymentResult):
                return cached

            payment = self._require_payment(payment_id)
            if payment.status == "captured":
                raise RailDeclined(f"payment {payment_id} is already captured")
            if payment.status != "authorized":
                raise RailDeclined(f"payment {payment_id} is {payment.status}, not authorized")
            if amount > payment.amount:
                raise RailDeclined(
                    f"capture {amount} exceeds the authorised {payment.amount} on {payment_id}"
                )

            payment.status = "captured"
            payment.captured_amount = amount
            result = PaymentResult(
                payment_id=payment_id,
                order_id=payment.order_id,
                amount=amount,
                currency=payment.currency,
                status="captured",
                method=payment.method,
                raw={"entity": "payment"},
            )
            self._remember(idempotency_key, result)
            self._emit("payment.captured", result)
            return result

    def fetch_payment(self, payment_id: str) -> PaymentResult:
        with self._lock:
            payment = self._require_payment(payment_id)
            return PaymentResult(
                payment_id=payment.payment_id,
                order_id=payment.order_id,
                amount=payment.captured_amount or payment.amount,
                currency=payment.currency,
                status=payment.status,
                method=payment.method,
                raw={
                    "entity": "payment",
                    "captured": payment.captured_amount,
                    "refunded": payment.refunded_amount,
                },
            )

    def create_refund(self, *, payment_id: str, amount: int, idempotency_key: str) -> RefundResult:
        _require_amount(amount)
        with self._lock:
            cached = self._replay(idempotency_key)
            if isinstance(cached, RefundResult):
                return cached

            payment = self._require_payment(payment_id)
            if payment.status != "captured":
                raise RailDeclined(f"cannot refund {payment_id}: status is {payment.status}")

            remaining = payment.captured_amount - payment.refunded_amount
            if amount > remaining:
                # The rail refuses too. PayNaka's gate should already have caught this;
                # a second, independent refusal here means a gate bug cannot become a
                # real over-refund even in the demo.
                raise RailDeclined(
                    f"refund {amount} exceeds the {remaining} still refundable on {payment_id}"
                )

            payment.refunded_amount += amount
            result = RefundResult(
                refund_id=self._mint("rfnd"),
                payment_id=payment_id,
                amount=amount,
                status="processed",
                raw={"entity": "refund"},
            )
            self._remember(idempotency_key, result)
            self._emit("refund.processed", result)
            return result

    # ---------------------------------------------------------------- webhooks
    def _emit(self, event: str, payload: Any) -> None:
        entry = {"event": event, "payload": payload}
        self.webhooks.append(entry)
        every = self._faults.duplicate_webhook_every
        if every and len(self.webhooks) % every == 0:
            # Real gateways re-deliver. This is not a bug being simulated -- it is normal
            # at-least-once behaviour, and the gate's idempotency is what survives it.
            self.webhooks.append(dict(entry))

    def drain_webhooks(self) -> list[dict[str, Any]]:
        with self._lock:
            pending, self.webhooks = self.webhooks, []
        return pending

    # ---------------------------------------------------------------- helpers
    def _require_payment(self, payment_id: str) -> _Payment:
        payment = self._payments.get(payment_id)
        if payment is None:
            raise RailDeclined(f"no such payment: {payment_id}")
        return payment


def _require_amount(amount: int) -> None:
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise RailDeclined(f"amount must be int paise, got {type(amount).__name__}")
    if amount <= 0:
        raise RailDeclined("amount must be positive")
    if amount > MAX_PAISE:
        raise RailDeclined("amount exceeds the money ceiling")
