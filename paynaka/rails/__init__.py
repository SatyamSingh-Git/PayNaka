"""Payment rails. Exactly two, chosen by ``PAYNAKA_RAIL``."""

from __future__ import annotations

import os

from paynaka.rails.base import (
    OrderResult,
    PaymentResult,
    Rail,
    RailDeclined,
    RailError,
    RefundResult,
)
from paynaka.rails.sim import FaultSchedule, SimRail

__all__ = [
    "FaultSchedule",
    "OrderResult",
    "PaymentResult",
    "Rail",
    "RailDeclined",
    "RailError",
    "RefundResult",
    "SimRail",
    "build_rail",
]


def build_rail(name: str | None = None) -> Rail:
    """Construct the configured rail.

    ``sim`` is the default on purpose. Reaching the network should be an explicit choice,
    not what happens when an environment variable is unset.
    """
    choice = (name or os.environ.get("PAYNAKA_RAIL", "sim")).strip().lower()

    if choice == "sim":
        return SimRail()
    if choice == "test":
        from paynaka.rails.razorpay_rail import RazorpayRail

        return RazorpayRail()
    if choice == "live":
        raise RailError(
            "there is no live rail. PayNaka runs against Razorpay test mode only, "
            "and that is not configurable."
        )
    raise RailError(f"unknown rail {choice!r}; expected 'sim' or 'test'")
