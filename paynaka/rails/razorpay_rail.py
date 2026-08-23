"""The real Razorpay rail. Test mode only, enforced at construction.

This is the one module in PayNaka that holds payment credentials. Everything else asks it
to act. Two safety properties, both tested:

**No live mode.** The constructor refuses any key that does not begin with ``rzp_test_``.
There is no flag to override it and no environment variable that relaxes it. A student
project that can be pointed at a live merchant account by editing one line is a liability;
one that physically cannot is not.

**No secrets in errors.** Razorpay's client puts request context into exception messages.
Those messages end up in the audit log and on screen in a demo, so they are scrubbed
before they leave this module.

Requires the optional ``agent`` extra: ``uv sync --extra agent``.
"""

from __future__ import annotations

import os
import re
from typing import Any, Final

from paynaka.money import MAX_PAISE
from paynaka.rails.base import (
    OrderResult,
    PaymentResult,
    RailDeclined,
    RailError,
    RefundResult,
)

__all__ = ["RazorpayRail"]

_TEST_KEY: Final[re.Pattern[str]] = re.compile(r"\Arzp_test_[A-Za-z0-9]{4,}\Z")

#: Razorpay reason codes that mean "the bank said no", as distinct from "we could not
#: reach the bank". The two demand opposite retry behaviour.
_DECLINE_REASONS: Final[frozenset[str]] = frozenset(
    {
        "payment_failed",
        "BAD_REQUEST_ERROR",
        "insufficient_funds",
        "payment_declined_by_bank",
    }
)


class RazorpayRail:
    """Razorpay test-mode API adapter."""

    name = "razorpay-test"

    def __init__(self, key_id: str | None = None, key_secret: str | None = None) -> None:
        try:
            import razorpay
        except ImportError as exc:  # pragma: no cover - depends on install extras
            raise RailError(
                "the razorpay SDK is not installed; run `uv sync --extra agent`"
            ) from exc

        key_id = key_id or os.environ.get("RAZORPAY_KEY_ID", "")
        key_secret = key_secret or os.environ.get("RAZORPAY_KEY_SECRET", "")

        if not key_id or not key_secret:
            raise RailError("RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET must be set; see .env.example")

        # The refusal that makes this safe to hand to a stranger. Deliberately not
        # overridable: PayNaka is a demonstration of bounded authority, and shipping it
        # with a switch that points it at live money would undercut the entire argument.
        if not _TEST_KEY.match(key_id):
            raise RailError(
                f"refusing to start: {key_id[:12]!r}... is not a Razorpay test key. "
                "PayNaka runs in test mode only, and this is not configurable."
            )

        self._client = razorpay.Client(auth=(key_id, key_secret))
        self._client.set_app_details({"title": "PayNaka", "version": "0.1.0"})

    # ---------------------------------------------------------------- operations
    def create_order(
        self, *, amount: int, currency: str, receipt: str, idempotency_key: str
    ) -> OrderResult:
        _require_amount(amount)
        raw = self._call(
            self._client.order.create,
            {
                "amount": amount,
                "currency": currency,
                "receipt": receipt,
                "notes": {"paynaka_idempotency_key": idempotency_key},
            },
        )
        return OrderResult(
            order_id=str(raw["id"]),
            amount=int(raw["amount"]),
            currency=str(raw["currency"]),
            status=str(raw["status"]),
            receipt=raw.get("receipt"),
            raw=raw,
        )

    def pay_order(
        self, *, order_id: str, method: str, idempotency_key: str, succeed: bool = True
    ) -> PaymentResult:
        """Not automatable against the real API.

        A real payment needs a customer at a checkout, or the S2S integration and its
        approvals. The demo drives Razorpay's hosted checkout by hand -- ``success@razorpay``
        or ``failure@razorpay`` as the UPI id -- and PayNaka picks the payment up from the
        webhook. Raising here rather than faking it keeps the distinction honest.
        """
        raise RailError(
            "pay_order is not automatable on the real rail; complete checkout in the "
            "browser with success@razorpay or failure@razorpay, or use PAYNAKA_RAIL=sim"
        )

    def capture_payment(
        self, *, payment_id: str, amount: int, idempotency_key: str
    ) -> PaymentResult:
        _require_amount(amount)
        raw = self._call(self._client.payment.capture, payment_id, amount, {"currency": "INR"})
        return _payment_from(raw)

    def fetch_payment(self, payment_id: str) -> PaymentResult:
        return _payment_from(self._call(self._client.payment.fetch, payment_id))

    def create_refund(self, *, payment_id: str, amount: int, idempotency_key: str) -> RefundResult:
        _require_amount(amount)
        raw = self._call(
            self._client.payment.refund,
            payment_id,
            {
                "amount": amount,
                "speed": "normal",
                "notes": {"paynaka_idempotency_key": idempotency_key},
            },
        )
        return RefundResult(
            refund_id=str(raw["id"]),
            payment_id=payment_id,
            amount=int(raw["amount"]),
            status=str(raw.get("status", "pending")),
            raw=raw,
        )

    # ---------------------------------------------------------------- plumbing
    def _call(self, fn: Any, *args: Any) -> dict[str, Any]:
        """Invoke the SDK and normalise its failures.

        Razorpay raises a family of exceptions whose messages carry request context. Those
        strings reach the audit log and the demo screen, so nothing leaves here unscrubbed.
        """
        try:
            result = fn(*args)
        except Exception as exc:
            message = _scrub(str(exc))
            if any(reason in message for reason in _DECLINE_REASONS):
                raise RailDeclined(message) from None
            raise RailError(message) from None

        if not isinstance(result, dict):  # pragma: no cover - SDK contract
            raise RailError(f"unexpected response type from Razorpay: {type(result).__name__}")
        return result


def _payment_from(raw: dict[str, Any]) -> PaymentResult:
    return PaymentResult(
        payment_id=str(raw["id"]),
        order_id=str(raw.get("order_id", "")),
        amount=int(raw["amount"]),
        currency=str(raw.get("currency", "INR")),
        status=str(raw["status"]),
        method=raw.get("method"),
        raw=raw,
    )


def _scrub(message: str) -> str:
    """Remove anything that looks like a credential from an error string.

    Order matters and is the reason this function has its own tests. The generic
    ``key: value`` rule below consumes only one token, so running it first against
    ``Authorization: Basic <base64>`` rewrites the word ``Basic`` and leaves the actual
    credential sitting in the message. The specific scheme rules therefore run first, and
    the generic rule then takes everything to end of line rather than a single token.
    """
    message = re.sub(r"(?i)\b(Basic|Bearer)\s+[A-Za-z0-9+/=._~-]+", r"\1 ***", message)
    message = re.sub(r"rzp_(test|live)_[A-Za-z0-9]+", "rzp_***", message)
    message = re.sub(
        r"(?i)\b(authorization|auth|secret|password|token|api[_-]?key)\s*[:=]\s*[^\r\n]+",
        r"\1=***",
        message,
    )
    return message[:500]


def _require_amount(amount: int) -> None:
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise RailError(f"amount must be int paise, got {type(amount).__name__}")
    if amount <= 0:
        raise RailError("amount must be positive")
    if amount > MAX_PAISE:
        raise RailError("amount exceeds the money ceiling")
