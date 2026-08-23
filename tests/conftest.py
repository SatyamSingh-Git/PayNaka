"""Shared fixtures.

The scenario throughout the suite is the one from the demo: a shopper authorises ₹1,999
for a 5kg bag of atta, and an attacker who controls a product review tries to turn that
into ₹52,000 including a gift card nobody asked for.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from paynaka.clock import FrozenClock
from paynaka.gate import LineItem, MoneyRequest
from paynaka.mandate import IntentMandate, MandateSigner, generate_keypair
from paynaka.policy import Policy
from paynaka.state import SqliteState

# ---------------------------------------------------------------- the scenario
AUTHORISED = 199_900  # ₹1,999 -- what the shopper actually agreed to
ATTACK = 5_200_000  # ₹52,000 -- what the poisoned review tries to spend
ATTA = "ATTA-5KG"
GIFT_CARD = "GIFT-50K"
HOME = "addr_home"
ATTACKER_ADDR = "addr_attacker"


@pytest.fixture
def clock() -> FrozenClock:
    """11:30 IST -- inside RBI contact hours, inside the NPCI debit blackout."""
    return FrozenClock.at_ist("2026-08-23 11:30")


@pytest.fixture
def signer() -> MandateSigner:
    return MandateSigner(generate_keypair()[0])


@pytest.fixture
def state(clock: FrozenClock) -> Iterator[SqliteState]:
    with SqliteState(":memory:", clock=clock) as st:
        yield st


@pytest.fixture
def policy() -> Policy:
    return Policy.from_yaml("policy.yaml")


@pytest.fixture
def mandate(clock: FrozenClock) -> IntentMandate:
    """₹1,999, one SKU, one destination, orders and captures only."""
    return IntentMandate.create(
        clock=clock,
        subject="cust_kirana_001",
        session_id="sess_demo",
        max_total=AUTHORISED,
        allowed_skus=(ATTA,),
        allowed_destinations=(HOME,),
        max_qty_per_sku=3,
    )


@pytest.fixture
def refund_mandate(clock: FrozenClock) -> IntentMandate:
    """A mandate that does permit refunds, for exercising the refund checks."""
    return IntentMandate.create(
        clock=clock,
        subject="cust_kirana_001",
        session_id="sess_refund",
        max_total=AUTHORISED,
        allowed_actions=("create_refund",),
    )


def order(
    *,
    sku: str = ATTA,
    qty: int = 1,
    unit: int = AUTHORISED,
    destination: str | None = HOME,
    key: str = "idem_1",
    currency: str = "INR",
) -> MoneyRequest:
    """A single-line order request. The default is the legitimate purchase."""
    return MoneyRequest(
        action="create_order",
        request_id=f"req_{key}",
        idempotency_key=key,
        items=(LineItem(sku=sku, qty=qty, unit_paise=unit),),
        currency=currency,
        destination=destination,
    )


@pytest.fixture
def legit_order() -> MoneyRequest:
    return order()


@pytest.fixture
def poisoned_order() -> MoneyRequest:
    """What the agent proposes after reading the poisoned review.

    The atta is still there and still correct. The gift card is the injection, and it is
    what takes the total from ₹1,999 to ₹52,000.
    """
    return MoneyRequest(
        action="create_order",
        request_id="req_poisoned",
        idempotency_key="idem_poisoned",
        items=(
            LineItem(sku=ATTA, qty=1, unit_paise=AUTHORISED),
            LineItem(sku=GIFT_CARD, qty=1, unit_paise=ATTACK - AUTHORISED),
        ),
        destination=HOME,
    )
