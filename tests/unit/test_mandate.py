"""Forward tests for paynaka.mandate."""

from __future__ import annotations

import json

import pytest

from paynaka.clock import FrozenClock
from paynaka.mandate import (
    DOMAIN,
    IntentMandate,
    MandateExpired,
    MandateSigner,
    SignedMandate,
    canonical_bytes,
    generate_keypair,
)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock.at_ist("2026-08-23 11:30")


@pytest.fixture
def signer() -> MandateSigner:
    private, _ = generate_keypair()
    return MandateSigner(private)


@pytest.fixture
def mandate(clock: FrozenClock) -> IntentMandate:
    return IntentMandate.create(
        clock=clock,
        subject="cust_kirana_001",
        session_id="sess_abc",
        max_total=199900,
        allowed_skus=("ATTA-5KG",),
        allowed_destinations=("addr_home",),
    )


class TestCreate:
    def test_fields_are_as_requested(self, mandate: IntentMandate) -> None:
        assert mandate.max_total == 199900
        assert mandate.currency == "INR"
        assert mandate.allowed_skus == ("ATTA-5KG",)
        assert mandate.allowed_destinations == ("addr_home",)
        assert mandate.requires_return_for_refund is True

    def test_id_and_nonce_are_generated_not_supplied(self, mandate: IntentMandate) -> None:
        assert mandate.mandate_id.startswith("mnd_")
        assert len(mandate.nonce) >= 24

    def test_two_mandates_never_share_a_nonce(self, clock: FrozenClock) -> None:
        nonces = {
            IntentMandate.create(clock=clock, subject="c", session_id="s", max_total=1000).nonce
            for _ in range(200)
        }
        assert len(nonces) == 200

    def test_ttl_sets_expiry(self, clock: FrozenClock) -> None:
        m = IntentMandate.create(
            clock=clock, subject="c", session_id="s", max_total=1000, ttl_seconds=600
        )
        assert m.expires_at - m.issued_at == 600

    def test_default_actions_do_not_include_refund(self, mandate: IntentMandate) -> None:
        """Refund is the irreversible one. It must be opted into, never inherited."""
        assert "create_refund" not in mandate.allowed_actions
        assert "create_payout" not in mandate.allowed_actions


class TestSignAndVerify:
    def test_round_trip(self, signer: MandateSigner, mandate: IntentMandate) -> None:
        signed = signer.sign(mandate)
        assert signer.verifier().verify(signed) == mandate

    def test_signature_is_ed25519_sized(
        self, signer: MandateSigner, mandate: IntentMandate
    ) -> None:
        assert len(signer.sign(mandate).signature) == 64

    def test_verify_returns_the_payload(
        self, signer: MandateSigner, mandate: IntentMandate
    ) -> None:
        """Returning the mandate makes 'forgot to check the result' impossible."""
        assert signer.verifier().verify(signer.sign(mandate)).max_total == 199900


class TestCanonicalBytes:
    def test_carries_the_domain_tag(self, mandate: IntentMandate) -> None:
        assert canonical_bytes(mandate).startswith(DOMAIN + b"|")

    def test_is_deterministic(self, mandate: IntentMandate) -> None:
        assert canonical_bytes(mandate) == canonical_bytes(mandate)

    def test_is_pure_ascii(self, clock: FrozenClock) -> None:
        """ASCII escaping keeps the bytes identical across platforms and locales."""
        m = IntentMandate.create(clock=clock, subject="ग्राहक-१", session_id="s", max_total=1000)
        canonical_bytes(m).decode("ascii")  # must not raise

    def test_keys_are_sorted(self, mandate: IntentMandate) -> None:
        body = canonical_bytes(mandate).split(b"|", 1)[1].decode("ascii")
        keys = list(json.loads(body))
        assert keys == sorted(keys)

    def test_no_insignificant_whitespace(self, mandate: IntentMandate) -> None:
        body = canonical_bytes(mandate).split(b"|", 1)[1]
        assert b", " not in body
        assert b": " not in body


class TestSerialisation:
    def test_mandate_dict_round_trip(self, mandate: IntentMandate) -> None:
        assert IntentMandate.from_dict(mandate.to_dict()) == mandate

    def test_signed_dict_round_trip(self, signer: MandateSigner, mandate: IntentMandate) -> None:
        signed = signer.sign(mandate)
        restored = SignedMandate.from_dict(signed.to_dict())
        assert restored == signed
        assert signer.verifier().verify(restored) == mandate

    def test_survives_a_json_round_trip(
        self, signer: MandateSigner, mandate: IntentMandate
    ) -> None:
        """The real transport is JSON over MCP, so prove it survives that specifically."""
        signed = signer.sign(mandate)
        wire = json.dumps(signed.to_dict())
        restored = SignedMandate.from_dict(json.loads(wire))
        assert signer.verifier().verify(restored) == mandate


class TestLifecycle:
    def test_live_mandate_passes(self, clock: FrozenClock, mandate: IntentMandate) -> None:
        mandate.assert_live(clock)
        assert not mandate.is_expired(clock)

    def test_expires_exactly_at_expires_at(self, clock: FrozenClock) -> None:
        m = IntentMandate.create(
            clock=clock, subject="c", session_id="s", max_total=1000, ttl_seconds=600
        )
        clock.advance(seconds=599)
        m.assert_live(clock)

        clock.advance(seconds=1)  # now exactly expires_at -- exclusive, so expired
        assert m.is_expired(clock)
        with pytest.raises(MandateExpired, match="expired"):
            m.assert_live(clock)
