"""Forging a webhook, and the ways a verifier fails open.

A webhook endpoint is an instruction to write the ledger, arriving over the open internet
from a source anybody can imitate. Signature verification is the only thing between that
and a forge-a-capture hole with a URL, so the interesting tests are all about the ways
verification can look present and not be:

* **The wrong bytes.** Signing a re-serialised parse instead of the body as it arrived is
  the classic mistake, and it fails *open* -- tampering the parser normalises away verifies
  against its own normalisation.
* **The wrong comparison.** An early return leaks how much of a forgery was right.
* **The wrong configuration.** No secret must mean nothing is accepted, never that
  everything is.
* **The wrong trust.** A verified webhook is trusted to have come from Razorpay. What it
  *claims* is a separate question, and this file draws that line explicitly.

Both halves live here because the module is one predicate and one parser, and the forward
cases are meaningless without the refusals beside them.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import pytest

from paynaka.webhooks import (
    DIGEST_HEX_LENGTH,
    MAX_BODY_BYTES,
    MIN_SECRET_LENGTH,
    WEBHOOK_SECRET_ENV_VAR,
    WebhookError,
    load_webhook_secret,
    parse_event,
    verify_signature,
)

pytestmark = pytest.mark.adversarial

SECRET = "a-real-endpoint-secret-value"


def sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def captured(amount: int = 199_900, payment_id: str = "pay_123") -> bytes:
    """A realistically-shaped Razorpay `payment.captured`."""
    return json.dumps(
        {
            "entity": "event",
            "event": "payment.captured",
            "id": "evt_abc123",
            "payload": {
                "payment": {
                    "entity": {
                        "id": payment_id,
                        "order_id": "order_456",
                        "amount": amount,
                        "currency": "INR",
                        "status": "captured",
                    }
                }
            },
        }
    ).encode("utf-8")


# ============================================================== it works at all
class TestAGenuineWebhookIsAccepted:
    def test_a_correct_signature_verifies(self) -> None:
        """The control. Without it every refusal below would pass against a verifier that
        refuses everything."""
        body = captured()
        assert verify_signature(body, sign(body), SECRET) is True

    def test_the_event_parses_into_the_fields_the_ledger_needs(self) -> None:
        event = parse_event(captured(amount=199_900))
        assert event.event == "payment.captured"
        assert event.payment_id == "pay_123"
        assert event.order_id == "order_456"
        assert event.amount == 199_900
        assert event.event_id == "evt_abc123"

    def test_the_provider_event_id_survives(self) -> None:
        """It is what makes duplicate suppression possible: Razorpay repeats it on a
        redelivery."""
        assert parse_event(captured()).event_id == "evt_abc123"


# ============================================================== forgery
class TestAForgedWebhookIsRefused:
    @pytest.mark.parametrize(
        "signature",
        [
            None,
            "",
            "   ",
            "0" * DIGEST_HEX_LENGTH,
            "f" * DIGEST_HEX_LENGTH,
            "not-a-hex-digest",
            "deadbeef",
        ],
        ids=repr,
    )
    def test_a_wrong_or_absent_signature_is_refused(self, signature: str | None) -> None:
        assert verify_signature(captured(), signature, SECRET) is False

    def test_a_signature_from_a_different_secret_is_refused(self) -> None:
        body = captured()
        assert verify_signature(body, sign(body, "some-other-endpoint-secret"), SECRET) is False

    def test_a_signature_for_a_different_body_is_refused(self) -> None:
        """A replayed signature from an earlier genuine event."""
        assert verify_signature(captured(amount=500_000), sign(captured()), SECRET) is False

    @pytest.mark.parametrize(
        "length", [0, 1, 32, DIGEST_HEX_LENGTH - 1, DIGEST_HEX_LENGTH + 1, 200]
    )
    def test_a_signature_of_the_wrong_length_is_refused_on_shape(self, length: int) -> None:
        assert verify_signature(captured(), "a" * length, SECRET) is False

    def test_one_flipped_character_is_refused(self) -> None:
        body = captured()
        genuine = sign(body)
        flipped = ("0" if genuine[0] != "0" else "1") + genuine[1:]
        assert verify_signature(body, flipped, SECRET) is False

    @pytest.mark.parametrize("payload", ["\ud800" * 32, "…" * 32, "ｆ" * 64], ids=repr)
    def test_a_non_ascii_signature_is_a_refusal_not_a_crash(self, payload: str) -> None:
        """The same class of defect as the one found in `identity.py`: comparing text
        rather than bytes turned a non-ASCII header into an unhandled exception on the
        auth path."""
        assert verify_signature(captured(), payload, SECRET) is False


class TestTheSignatureCoversTheBytesThatArrived:
    def test_a_reserialised_body_does_not_verify(self) -> None:
        """The mistake that fails open. JSON round-tripping reorders keys and changes
        whitespace, so a signature checked against a re-serialised parse is a signature
        over a different document -- and tampering the parser normalises away would slip
        through."""
        original = captured()
        signature = sign(original)
        reserialised = json.dumps(json.loads(original), sort_keys=True).encode("utf-8")
        assert reserialised != original
        assert verify_signature(reserialised, signature, SECRET) is False

    def test_whitespace_added_to_the_body_invalidates_it(self) -> None:
        body = captured()
        assert verify_signature(body + b" ", sign(body), SECRET) is False

    def test_a_changed_amount_invalidates_it(self) -> None:
        """The attack that matters: a redelivery whose amount was altered in flight."""
        body = captured(amount=199_900)
        tampered = body.replace(b'"amount": 199900', b'"amount": 5200000')
        assert tampered != body
        assert verify_signature(tampered, sign(body), SECRET) is False


class TestOversizeAndMalformed:
    def test_an_oversize_body_is_refused_without_hashing_it(self) -> None:
        assert (
            verify_signature(b"x" * (MAX_BODY_BYTES + 1), "a" * DIGEST_HEX_LENGTH, SECRET) is False
        )

    def test_an_oversize_body_is_refused_by_the_parser_too(self) -> None:
        with pytest.raises(WebhookError, match="too large"):
            parse_event(b"x" * (MAX_BODY_BYTES + 1))

    @pytest.mark.parametrize(
        "body",
        [b"", b"not json", b"{", b"[]", b'"a string"', b"123", b"null", b"true"],
        ids=repr,
    )
    def test_a_body_that_is_not_an_event_object_is_refused(self, body: bytes) -> None:
        with pytest.raises(WebhookError):
            parse_event(body)

    @pytest.mark.parametrize(
        "payload",
        [{}, {"event": ""}, {"event": None}, {"event": 42}, {"event": []}],
        ids=repr,
    )
    def test_a_body_naming_no_event_is_refused(self, payload: dict[str, Any]) -> None:
        with pytest.raises(WebhookError, match="names no event"):
            parse_event(json.dumps(payload).encode("utf-8"))


class TestAVerifiedBodyIsStillReadDefensively:
    """Verification proves *who sent it*, not what shape it is. A provider is free to add
    fields and send a payload this code has never seen."""

    @pytest.mark.parametrize(
        "amount", [None, "199900", 1999.0, True, False, -1, [199_900], {"v": 1}], ids=repr
    )
    def test_an_amount_that_is_not_positive_int_paise_becomes_none(self, amount: object) -> None:
        """Never coerced. Coercion is how a ledger learns to believe "1999.00" is 1999
        paise, and ``True`` is an ``int`` in Python."""
        body = json.dumps(
            {"event": "payment.captured", "payload": {"payment": {"entity": {"amount": amount}}}}
        ).encode("utf-8")
        assert parse_event(body).amount is None

    @pytest.mark.parametrize(
        "payload",
        [
            {"event": "payment.captured"},
            {"event": "payment.captured", "payload": None},
            {"event": "payment.captured", "payload": []},
            {"event": "payment.captured", "payload": {"payment": None}},
            {"event": "payment.captured", "payload": {"payment": {"entity": "x"}}},
            {"event": "payment.captured", "payload": {"unknown_kind": {"entity": {}}}},
        ],
        ids=["no-payload", "null", "list", "null-payment", "entity-not-dict", "unknown-kind"],
    )
    def test_an_unnavigable_body_still_yields_an_event(self, payload: dict[str, Any]) -> None:
        """It arrived and it verified. Refusing to record it because the nesting is
        unfamiliar loses the fact that something happened."""
        event = parse_event(json.dumps(payload).encode("utf-8"))
        assert event.event == "payment.captured"
        assert event.amount is None

    def test_a_refund_event_reads_its_payment_id(self) -> None:
        body = json.dumps(
            {
                "event": "refund.processed",
                "payload": {"refund": {"entity": {"payment_id": "pay_999", "amount": 49_900}}},
            }
        ).encode("utf-8")
        event = parse_event(body)
        assert event.payment_id == "pay_999"
        assert event.amount == 49_900


# ============================================================== configuration
class TestNoSecretMeansNothingIsAccepted:
    @pytest.mark.parametrize("secret", ["", "   ", "short", "x" * (MIN_SECRET_LENGTH - 1)])
    def test_a_missing_or_weak_secret_verifies_nothing(self, secret: str) -> None:
        """Not "verification off". A weak shared key in front of a money path is the
        absence of security with a label on."""
        body = captured()
        assert verify_signature(body, sign(body, secret), secret) is False

    def test_loading_an_unset_secret_raises_rather_than_returning_falsy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Returning "" would let a caller treat it as "verification off", which is the
        bypass this module exists not to have."""
        monkeypatch.delenv(WEBHOOK_SECRET_ENV_VAR, raising=False)
        with pytest.raises(WebhookError, match="no webhook can be verified"):
            load_webhook_secret(rail="sim")

    def test_the_message_says_there_is_no_development_bypass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A guard nobody understands is a guard somebody deletes."""
        monkeypatch.delenv(WEBHOOK_SECRET_ENV_VAR, raising=False)
        with pytest.raises(WebhookError, match="no development mode"):
            load_webhook_secret(rail="sim")

    def test_a_real_rail_is_named_in_the_refusal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(WEBHOOK_SECRET_ENV_VAR, raising=False)
        with pytest.raises(WebhookError, match="reaches a real payment API"):
            load_webhook_secret(rail="test")

    @pytest.mark.parametrize("length", [1, 8, MIN_SECRET_LENGTH - 1])
    def test_a_weak_configured_secret_is_refused_at_load(
        self, monkeypatch: pytest.MonkeyPatch, length: int
    ) -> None:
        monkeypatch.setenv(WEBHOOK_SECRET_ENV_VAR, "x" * length)
        with pytest.raises(WebhookError, match="is the minimum"):
            load_webhook_secret(rail="sim")

    @pytest.mark.parametrize("length", [MIN_SECRET_LENGTH, MIN_SECRET_LENGTH + 1, 64])
    def test_a_real_secret_loads(self, monkeypatch: pytest.MonkeyPatch, length: int) -> None:
        """The other side of the floor."""
        monkeypatch.setenv(WEBHOOK_SECRET_ENV_VAR, "x" * length)
        assert load_webhook_secret(rail="sim") == "x" * length

    def test_the_refusal_does_not_echo_the_configured_secret(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(WEBHOOK_SECRET_ENV_VAR, "sekrit")
        try:
            load_webhook_secret(rail="sim")
        except WebhookError as exc:
            assert "sekrit" not in str(exc)
            assert "6 characters" in str(exc)
        else:  # pragma: no cover - the call above must raise
            pytest.fail("expected WebhookError")
