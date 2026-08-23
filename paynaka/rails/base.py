"""The payment rail interface.

Two implementations exist and they are not interchangeable by accident:

``SimRail``  a deterministic in-process simulator. Used by the test suite, by CI, and by
             the HAAT benchmark, where thousands of runs against a live sandbox would be
             slow, rate-limited and non-reproducible.

``RazorpayRail``  the real Razorpay **test-mode** API. Used by the demo and the video,
             because a real payment id on screen is worth more than a simulated one.

Which one runs is chosen by ``PAYNAKA_RAIL``. There is no third option and there is no
live mode: ``RazorpayRail`` refuses to construct against a key that is not ``rzp_test_``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "OrderResult",
    "PaymentResult",
    "Rail",
    "RailDeclined",
    "RailError",
    "RefundResult",
]


class RailError(Exception):
    """The rail could not complete the operation."""


class RailDeclined(RailError):
    """The rail actively declined, as opposed to failing to respond.

    Distinguished from a transport failure because the two demand opposite responses: a
    decline is final and must not be retried blindly, while a timeout may have succeeded
    and must be resolved by idempotent replay rather than by trying again.
    """


@dataclass(frozen=True, slots=True)
class OrderResult:
    order_id: str
    amount: int
    currency: str
    status: str
    receipt: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PaymentResult:
    payment_id: str
    order_id: str
    amount: int
    currency: str
    status: str
    method: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def captured(self) -> bool:
        return self.status == "captured"


@dataclass(frozen=True, slots=True)
class RefundResult:
    refund_id: str
    payment_id: str
    amount: int
    status: str
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Rail(Protocol):
    """What PayNaka needs from a payment provider. Deliberately small.

    Every method takes an explicit ``idempotency_key``. A rail that cannot be asked to be
    idempotent cannot be safely retried, and an agent that cannot safely retry will
    eventually double-charge someone.
    """

    name: str

    def create_order(
        self, *, amount: int, currency: str, receipt: str, idempotency_key: str
    ) -> OrderResult: ...

    def pay_order(
        self, *, order_id: str, method: str, idempotency_key: str, succeed: bool = True
    ) -> PaymentResult: ...

    def capture_payment(
        self, *, payment_id: str, amount: int, idempotency_key: str
    ) -> PaymentResult: ...

    def fetch_payment(self, payment_id: str) -> PaymentResult: ...

    def create_refund(
        self, *, payment_id: str, amount: int, idempotency_key: str
    ) -> RefundResult: ...
