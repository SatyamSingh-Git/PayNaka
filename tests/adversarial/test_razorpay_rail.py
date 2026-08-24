"""The only file that touches Razorpay, and the one with the least standing behind it.

At 43% coverage this was the worst-covered module in the repository and the sole path to a
real payment provider — a combination an independent review was right to call out. The
reason was structural rather than lazy: exercising it means making network calls, and a
test suite that needs the internet and a live account is a test suite nobody runs.

So the client is replaced with a double and the *logic* is tested: the guard that refuses a
live key, the scrubbing that keeps a secret out of an error, the notes that carry an
idempotency key and the audit anchor, the amount validation, and the mapping from
Razorpay's exception family onto this project's two.

What this deliberately does not test is whether Razorpay's API behaves as documented. That
is not testable from here and is not this file's job — `scripts/razorpay_test_lifecycle.py`
answers it against the real API, and its committed responses in `var/evidence/` are the
evidence for that half.
"""

from __future__ import annotations

from typing import Any

import pytest

from paynaka.rails.base import RailDeclined, RailError
from paynaka.rails.razorpay_rail import RazorpayRail, _notes, _payment_from, _require_amount, _scrub

pytestmark = pytest.mark.adversarial

TEST_KEY = "rzp_test_TTfBnH0lnrIIB0"
SECRET = "a-plausible-looking-secret"


class FakeEndpoint:
    """Records what it was called with, and returns whatever it was told to."""

    def __init__(self, result: Any = None, raises: Exception | None = None) -> None:
        self.result = result
        self.raises = raises
        self.calls: list[tuple[Any, ...]] = []

    def __call__(self, *args: Any) -> Any:
        self.calls.append(args)
        if self.raises is not None:
            raise self.raises
        return self.result


class FakeClient:
    def __init__(self) -> None:
        self.order = type("O", (), {"create": FakeEndpoint()})()
        self.payment = type(
            "P", (), {"capture": FakeEndpoint(), "fetch": FakeEndpoint(), "refund": FakeEndpoint()}
        )()


@pytest.fixture
def rail(monkeypatch: pytest.MonkeyPatch) -> RazorpayRail:
    monkeypatch.setenv("RAZORPAY_KEY_ID", TEST_KEY)
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", SECRET)
    built = RazorpayRail()
    built._client = FakeClient()
    return built


ORDER = {
    "id": "order_TTfEwDFYYo2xsb",
    "amount": 199_900,
    "currency": "INR",
    "status": "created",
    "receipt": "rcpt_1",
}
PAYMENT = {
    "id": "pay_TTfuF5dtZY8YdI",
    "order_id": "order_TTfEwDFYYo2xsb",
    "amount": 199_900,
    "currency": "INR",
    "status": "captured",
    "captured": True,
}


# ============================================================== the live-key guard
class TestItRefusesAnythingThatIsNotATestKey:
    @pytest.mark.parametrize(
        "key_id",
        [
            "rzp_live_TTfBnH0lnrIIB0",
            "rzp_live_x",
            "RZP_TEST_TTfBnH0lnrIIB0",
            "rzp_test",
            "rzp_test_",
            " rzp_test_TTfBnH0lnrIIB0",
            "rzp_test_TTfBnH0lnrIIB0 ",
            "rzp_test_TTfBnH0lnrIIB0\n",
            "not-a-key",
            "",
        ],
        ids=repr,
    )
    def test_a_non_test_key_refuses_to_construct(
        self, monkeypatch: pytest.MonkeyPatch, key_id: str
    ) -> None:
        """No override, no environment escape. The trailing-whitespace cases matter because
        a key pasted with a newline is the ordinary way this reaches a process."""
        monkeypatch.setenv("RAZORPAY_KEY_ID", key_id)
        monkeypatch.setenv("RAZORPAY_KEY_SECRET", SECRET)
        with pytest.raises(RailError):
            RazorpayRail()

    def test_a_live_key_refusal_says_what_is_wrong(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_live_TTfBnH0lnrIIB0")
        monkeypatch.setenv("RAZORPAY_KEY_SECRET", SECRET)
        with pytest.raises(RailError, match="test"):
            RazorpayRail()

    def test_the_refusal_does_not_echo_the_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An error is a thing that gets logged, and a live key in a log is an incident."""
        live = "rzp_live_SUPERSECRETVALUE99"
        monkeypatch.setenv("RAZORPAY_KEY_ID", live)
        monkeypatch.setenv("RAZORPAY_KEY_SECRET", SECRET)
        try:
            RazorpayRail()
        except RailError as exc:
            assert "SUPERSECRETVALUE99" not in str(exc)
        else:  # pragma: no cover - construction must fail
            pytest.fail("expected RailError")

    @pytest.mark.parametrize("missing", ["RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET"])
    def test_missing_credentials_refuse_to_construct(
        self, monkeypatch: pytest.MonkeyPatch, missing: str
    ) -> None:
        monkeypatch.setenv("RAZORPAY_KEY_ID", TEST_KEY)
        monkeypatch.setenv("RAZORPAY_KEY_SECRET", SECRET)
        monkeypatch.delenv(missing, raising=False)
        with pytest.raises(RailError, match="must be set"):
            RazorpayRail()

    def test_a_valid_test_key_constructs(self, rail: RazorpayRail) -> None:
        """The control. A guard that refuses everything is not a guard."""
        assert rail.name == "razorpay-test"


# ============================================================== the calls
class TestTheCallsItMakes:
    def test_create_order_sends_paise_and_reads_the_response(self, rail: RazorpayRail) -> None:
        rail._client.order.create = FakeEndpoint(result=ORDER)
        result = rail.create_order(
            amount=199_900, currency="INR", receipt="rcpt_1", idempotency_key="idem_1"
        )
        sent = rail._client.order.create.calls[0][0]
        assert sent["amount"] == 199_900
        assert isinstance(sent["amount"], int)
        assert result.order_id == "order_TTfEwDFYYo2xsb"
        assert result.raw is ORDER

    def test_capture_sends_the_payment_and_the_amount(self, rail: RazorpayRail) -> None:
        rail._client.payment.capture = FakeEndpoint(result=PAYMENT)
        result = rail.capture_payment(
            payment_id="pay_1", amount=199_900, idempotency_key="idem_cap"
        )
        args = rail._client.payment.capture.calls[0]
        assert args[0] == "pay_1"
        assert args[1] == 199_900
        assert result.payment_id == "pay_TTfuF5dtZY8YdI"

    def test_fetch_payment_maps_the_response(self, rail: RazorpayRail) -> None:
        rail._client.payment.fetch = FakeEndpoint(result=PAYMENT)
        assert rail.fetch_payment("pay_1").status == "captured"

    def test_pay_order_refuses_rather_than_faking_it(self, rail: RazorpayRail) -> None:
        """A real payment needs a customer at a checkout. Faking one here would make the
        demo look more autonomous than the design allows, which is the overclaim this
        project keeps refusing to make."""
        with pytest.raises(RailError, match="not automatable"):
            rail.pay_order(order_id="order_1", method="upi", idempotency_key="k")


class TestAmountValidation:
    @pytest.mark.parametrize("amount", [0, -1, -199_900])
    def test_a_nonpositive_amount_is_refused(self, amount: int) -> None:
        with pytest.raises(RailError, match="positive"):
            _require_amount(amount)

    @pytest.mark.parametrize("amount", [1999.0, "199900", None, True, False], ids=repr)
    def test_an_amount_that_is_not_int_paise_is_refused(self, amount: object) -> None:
        """``True`` is an ``int`` in Python, and a one-paisa charge from a boolean is a
        number somebody would spend an afternoon explaining."""
        with pytest.raises(RailError, match="int paise"):
            _require_amount(amount)  # type: ignore[arg-type]

    def test_an_absurd_amount_is_refused(self) -> None:
        with pytest.raises(RailError, match="ceiling"):
            _require_amount(10**15)

    @pytest.mark.parametrize("amount", [1, 199_900])
    def test_a_real_amount_passes(self, amount: int) -> None:
        _require_amount(amount)


# ============================================================== error handling
class TestRazorpaysExceptionsBecomeOurs:
    def test_a_decline_is_distinguished_from_a_failure(self, rail: RazorpayRail) -> None:
        """A declined payment is the rail working. A broken call is not, and a caller that
        cannot tell them apart will retry the wrong one."""
        rail._client.order.create = FakeEndpoint(
            raises=RuntimeError("BAD_REQUEST_ERROR: payment_failed")
        )
        with pytest.raises(RailDeclined):
            rail.create_order(amount=199_900, currency="INR", receipt="r", idempotency_key="k")

    @pytest.mark.parametrize(
        "reason",
        ["payment_failed", "BAD_REQUEST_ERROR", "insufficient_funds", "payment_declined_by_bank"],
    )
    def test_each_decline_reason_is_recognised(self, rail: RazorpayRail, reason: str) -> None:
        """Matched on substrings, because the reason arrives inside a longer SDK message. A
        provider inventing a new phrasing should widen the list rather than silently
        reclassify a decline as a broken call."""
        rail._client.order.create = FakeEndpoint(raises=RuntimeError(f"call failed: {reason}"))
        with pytest.raises(RailDeclined):
            rail.create_order(amount=199_900, currency="INR", receipt="r", idempotency_key="k")

    def test_an_unexpected_exception_becomes_a_rail_error(self, rail: RazorpayRail) -> None:
        rail._client.order.create = FakeEndpoint(raises=RuntimeError("socket exploded"))
        with pytest.raises(RailError):
            rail.create_order(amount=199_900, currency="INR", receipt="r", idempotency_key="k")

    def test_a_non_dict_response_is_refused(self, rail: RazorpayRail) -> None:
        """A provider is free to change what it returns. Reading fields off a string would
        raise somewhere far away from the cause."""
        rail._client.order.create = FakeEndpoint(result="not a dict")
        with pytest.raises(RailError, match="unexpected response type"):
            rail.create_order(amount=199_900, currency="INR", receipt="r", idempotency_key="k")


class TestASecretNeverReachesAnError:
    """The scrubber exists because an error message is a thing that gets logged, put on an
    audit chain, and rendered into a console. Any of those is a fine place for a key to
    end up and a terrible place for it to be found."""

    @pytest.mark.parametrize(
        "message",
        [
            "auth failed for rzp_live_SECRET99",
            "key rzp_test_TTfBnH0lnrIIB0 rejected",
            "Authorization: Basic cnpwX3Rlc3Q6c2VjcmV0",
            "{'key_id': 'rzp_live_ABCDEF123456'}",
        ],
        ids=["live-key", "test-key", "basic-auth", "json"],
    )
    def test_key_material_is_removed(self, message: str) -> None:
        scrubbed = _scrub(message)
        assert "rzp_live_SECRET99" not in scrubbed
        assert "rzp_live_ABCDEF123456" not in scrubbed
        assert "rzp_test_TTfBnH0lnrIIB0" not in scrubbed

    def test_an_ordinary_message_survives(self) -> None:
        """A scrubber that eats everything makes errors useless, which is its own failure."""
        assert "amount" in _scrub("amount must be positive")


class TestTheNotesItAttaches:
    def test_the_idempotency_key_travels_with_the_call(self) -> None:
        """So a human in the dashboard can tie a Razorpay record back to a business event."""
        assert _notes("idem_1", None)["paynaka_idempotency_key"] == "idem_1"

    def test_caller_notes_are_preserved(self) -> None:
        notes = _notes("idem_1", {"paynaka_audit_head": "abc123"})
        assert notes["paynaka_audit_head"] == "abc123"
        assert notes["paynaka_idempotency_key"] == "idem_1"

    def test_every_value_is_a_string(self) -> None:
        """Razorpay's notes are string-to-string. A number here fails at the API rather
        than here, which is a worse place to find out."""
        assert all(isinstance(v, str) for v in _notes("idem_1", {"a": "1"}).values())


class TestTheResponseMapper:
    def test_it_reads_the_fields_the_ledger_needs(self) -> None:
        mapped = _payment_from(PAYMENT)
        assert mapped.payment_id == "pay_TTfuF5dtZY8YdI"
        assert mapped.amount == 199_900
        assert mapped.status == "captured"

    def test_a_sparse_response_does_not_raise(self) -> None:
        """Read defensively: a provider adding fields is normal, and one omitting them
        should produce a usable object rather than a KeyError."""
        mapped = _payment_from({"id": "pay_1", "amount": 100, "currency": "INR", "status": "x"})
        assert mapped.payment_id == "pay_1"
