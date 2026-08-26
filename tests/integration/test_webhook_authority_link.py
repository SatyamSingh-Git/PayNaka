"""The lifecycle has to survive the checkpoint, not only be stopped by it.

The authority graph asks ``payment -> order -> mandate, subject`` before any capture or
refund. Orders record their own authority when PayNaka creates them. The *payment* id is
the half PayNaka cannot know: it exists only after a human authenticates at Checkout, and
it arrives afterwards, in a webhook.

That link was missing. `/webhooks/razorpay` recorded the capture on the ledger and returned
``capture_recorded``, and nothing joined the payment to the order it settled -- so the very
next legitimate action on that payment was refused with ``payment.unknown_origin``. A
containment check that also blocks the normal path is not containment; it is an outage, and
it is the failure mode that gets a security control deleted six weeks later.

A second audit caught it, correctly, and it was introduced by the fix for the first one.
So the tests here are about the honest path working end to end, and the guard still biting
where it should.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from merchant.app import reset_catalog
from paynaka.app import app, hub

pytestmark = pytest.mark.integration

SECRET = "an-endpoint-secret-long-enough-to-use"
PAYMENT = "pay_from_checkout"
ORDER = "order_from_checkout"
AMOUNT = 199_900


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", SECRET)
    reset_catalog()
    with TestClient(app) as test_client:
        yield test_client
    reset_catalog()


def captured(payment_id: str = PAYMENT, order_id: str | None = ORDER) -> bytes:
    """A `payment.captured` body in Razorpay's shape, carrying the order it settled."""
    entity: dict[str, object] = {"id": payment_id, "amount": AMOUNT}
    if order_id is not None:
        entity["order_id"] = order_id
    return json.dumps(
        {"event": "payment.captured", "payload": {"payment": {"entity": entity}}}
    ).encode("utf-8")


def issue_the_order(subject: str = "cust_kirana_001", order_id: str = ORDER) -> None:
    """What PayNaka records when *it* creates the order, before any of this happens.

    The order half of the graph and the payment half are learned by different parties at
    different times, which is why they are separate tables. This is our half.
    """
    hub.naka.state.record_order(
        order_id,
        mandate_id="mnd_webhook_test",
        subject=subject,
        session_id="sess_webhook_test",
    )


def deliver(client: TestClient, body: bytes, event_id: str = "evt_1") -> dict[str, object]:
    signature = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    response = client.post(
        "/webhooks/razorpay",
        content=body,
        headers={
            "X-Razorpay-Signature": signature,
            "X-Razorpay-Event-Id": event_id,
            "content-type": "application/json",
        },
    )
    return dict(response.json())


class TestTheProvidersHalfOfTheGraph:
    def test_a_capture_webhook_links_the_payment_to_its_order(self, client: TestClient) -> None:
        """The regression. Without this the capture was recorded and the payment remained
        an orphan, so the next action on it was refused."""
        issue_the_order()
        assert deliver(client, captured())["applied"] == "capture_recorded"

        authority = hub.naka.state.authority_for(PAYMENT)
        assert authority is not None, (
            "the capture was recorded but the payment was never joined to its order, so "
            "every later action on it is payment.unknown_origin"
        )
        assert authority.order_id == ORDER

    def test_the_capture_still_reaches_the_ledger(self, client: TestClient) -> None:
        """Both halves. A link written instead of the ledger entry would pass the test
        above and lose the money."""
        issue_the_order()
        deliver(client, captured())
        assert hub.naka.state.captured_amount(PAYMENT) == AMOUNT

    def test_a_redelivery_does_not_disturb_the_link(self, client: TestClient) -> None:
        issue_the_order()
        deliver(client, captured(), event_id="evt_dup")
        second = deliver(client, captured(), event_id="evt_dup")
        assert second["duplicate"] is True

        authority = hub.naka.state.authority_for(PAYMENT)
        assert authority is not None
        assert authority.order_id == ORDER
        assert hub.naka.state.captured_amount(PAYMENT) == AMOUNT, "a duplicate was applied twice"

    def test_a_webhook_naming_no_order_links_nothing(self, client: TestClient) -> None:
        """Fail closed. A payload with no order id says nothing about where the payment
        came from, and inventing a link would be the graph asserting a fact nobody sent."""
        issue_the_order()
        assert deliver(client, captured(order_id=None))["applied"] == "capture_recorded"
        assert hub.naka.state.authority_for(PAYMENT) is None

    def test_an_unsigned_delivery_writes_nothing_at_all(self, client: TestClient) -> None:
        """The link is written after the signature is checked, like everything else here.
        A payload anyone can post must not be able to attach a payment to an order."""
        issue_the_order()
        body = captured()
        response = client.post(
            "/webhooks/razorpay",
            content=body,
            headers={
                "X-Razorpay-Signature": "not-the-signature",
                "content-type": "application/json",
            },
        )
        assert response.status_code >= 400
        assert hub.naka.state.authority_for(PAYMENT) is None
        assert hub.naka.state.captured_amount(PAYMENT) == 0


class TestTheGuardStillBites:
    def test_a_link_to_an_order_nobody_issued_resolves_to_nothing(self, client: TestClient) -> None:
        """The webhook supplies the payment half. The order half is written only when
        PayNaka creates the order, so a payment for an order this checkpoint never issued
        still has no authority -- which is the whole point of two tables."""
        issue_the_order()  # our order exists; the webhook names a different one
        deliver(client, captured(order_id="order_created_somewhere_else"))
        assert hub.naka.state.authority_for(PAYMENT) is None
