"""Shared fixtures.

The scenario throughout the suite is the one from the demo: a shopper authorises ₹1,999
for a 5kg bag of atta, and an attacker who controls a product review tries to turn that
into ₹52,000 including a gift card nobody asked for.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from hypothesis import HealthCheck, settings

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

# ---------------------------------------------------------------- hypothesis profiles
#
# The property tests in tests/adversarial/test_gate_properties.py explore generated
# inputs, and how many they explore is a straight trade against how long `make check`
# takes. Locally that budget is small enough to keep the loop fast; in CI, where nobody is
# waiting at a keyboard, it is worth far more, because the whole point of a property test
# is that the counterexample is the one nobody thought of.
#
# `HYPOTHESIS_PROFILE=thorough` runs the deeper sweep on demand.
settings.register_profile(
    "dev",
    max_examples=120,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
settings.register_profile(
    "ci",
    max_examples=500,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
settings.register_profile(
    "thorough",
    max_examples=5_000,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
settings.load_profile(
    os.environ.get("HYPOTHESIS_PROFILE") or ("ci" if os.environ.get("CI") else "dev")
)


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


@pytest.fixture(autouse=True)
def _ephemeral_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """No test inherits a developer's durable configuration.

    `.env` sets PAYNAKA_AUDIT_DB, and the moment the app started honouring it the suite
    began writing into the committed audit fixture -- a 3-record chain of evidence became
    31 records of test traffic. Nothing failed loudly; the fixture was simply no longer
    what it claimed to be.

    So the runtime is pinned ephemeral here. A test that wants durability sets these
    itself, on a tmp_path, and says so.

    Pinned to `:memory:` rather than deleted: `load_env()` re-reads `.env` at app startup
    and puts back anything that is merely absent, so deleting them looks like it works and
    does not.
    """
    monkeypatch.setenv("PAYNAKA_STATE_DB", ":memory:")
    monkeypatch.setenv("PAYNAKA_AUDIT_DB", ":memory:")
    monkeypatch.delenv("PAYNAKA_DEMO_CLOCK", raising=False)
    # A signing key has no `:memory:`, so it gets a throwaway path instead. Left alone, a
    # test run signs with -- and creates -- the developer's real `var/mandate_ed25519.key`.
    monkeypatch.setenv(
        "PAYNAKA_SIGNING_KEY_PATH", str(tmp_path_factory.mktemp("keys") / "signing.key")
    )
