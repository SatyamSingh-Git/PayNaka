"""Adversarial tests for paynaka.mandate -- the crown jewels.

The entire PayNaka argument reduces to one claim: *an agent under attacker control cannot
widen its own authority*. That claim is false the moment any of these tests fails.

The attacker model is deliberately generous. Assume the adversary can:

- read the mandate in full (it travels with the request);
- choose arbitrary bytes for every field they can influence;
- replay, truncate, pad, reorder and re-encode anything on the wire;
- sign whatever they like with a key that is not ours.

They must still be unable to produce a mandate that verifies and permits more than the
shopper authorised.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from paynaka.clock import FrozenClock
from paynaka.mandate import (
    DOMAIN,
    IntentMandate,
    MandateExpired,
    MandateMalformed,
    MandateSigner,
    SignatureInvalid,
    SignedMandate,
    canonical_bytes,
    generate_keypair,
)

pytestmark = pytest.mark.adversarial


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
        max_total=199900,  # ₹1,999 -- the authorised amount throughout
        allowed_skus=("ATTA-5KG",),
        allowed_destinations=("addr_home",),
    )


ATTACK_AMOUNT = 5_200_000  # ₹52,000 -- the gift-card overpayment from the demo


class TestTampering:
    """Change one field, keep the signature. Every case must fail to verify."""

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("max_total", ATTACK_AMOUNT),
            ("max_total", 200000),
            ("max_total", 199901),  # one paise more
            ("max_qty_per_sku", 9999),
            ("allowed_skus", ("ATTA-5KG", "GIFT-50K")),
            ("allowed_skus", ()),  # widen to "any SKU"
            ("allowed_destinations", ("addr_attacker",)),
            ("allowed_destinations", ()),
            ("allowed_actions", ("create_order", "capture_payment", "create_refund")),
            ("allowed_actions", ("create_payout",)),
            ("requires_return_for_refund", False),
            # A *plausible* expiry extension, computed relative to the mandate so it
            # stays inside the 24h structural limit and therefore reaches the signature
            # check rather than being caught earlier by validation.
            ("expires_at", lambda m: m.issued_at + 86_399),
            ("subject", "cust_attacker"),
            ("session_id", "sess_attacker"),
            ("nonce", "replayed-nonce-value"),
            ("mandate_id", "mnd_attacker"),
        ],
    )
    def test_mutating_any_field_breaks_the_signature(
        self, signer: MandateSigner, mandate: IntentMandate, field: str, value: object
    ) -> None:
        resolved = value(mandate) if callable(value) else value
        signed = signer.sign(mandate)
        forged = SignedMandate(replace(mandate, **{field: resolved}), signed.signature)  # type: ignore[arg-type]
        with pytest.raises(SignatureInvalid):
            signer.verifier().verify(forged)

    def test_absurd_expiry_is_caught_by_validation_before_the_signature_check(
        self, mandate: IntentMandate
    ) -> None:
        """Defence in depth: a year-2100 expiry never even reaches signature verification.

        Structural validation runs at construction, so a forged mandate with an immortal
        lifetime dies as malformed rather than as unsigned. Both are refusals; this one is
        just earlier and cheaper.
        """
        with pytest.raises(MandateMalformed, match="24h"):
            replace(mandate, expires_at=4_102_444_800)

    def test_the_headline_attack_specifically(
        self, signer: MandateSigner, mandate: IntentMandate
    ) -> None:
        """₹1,999 authorised; attacker wants ₹52,000. This is the demo, as a unit test."""
        signed = signer.sign(mandate)
        forged = SignedMandate(replace(mandate, max_total=ATTACK_AMOUNT), signed.signature)
        with pytest.raises(SignatureInvalid):
            signer.verifier().verify(forged)

    def test_tampering_through_the_wire_format_also_fails(
        self, signer: MandateSigner, mandate: IntentMandate
    ) -> None:
        """Edit the JSON directly, the way a real interceptor would."""
        wire = json.loads(json.dumps(signer.sign(mandate).to_dict()))
        wire["mandate"]["max_total"] = ATTACK_AMOUNT
        with pytest.raises(SignatureInvalid):
            signer.verifier().verify(SignedMandate.from_dict(wire))


class TestSignatureForgery:
    def test_a_different_key_does_not_verify(self, mandate: IntentMandate) -> None:
        """The attacker can sign perfectly well. Just not as us."""
        ours = MandateSigner(generate_keypair()[0])
        theirs = MandateSigner(generate_keypair()[0])
        with pytest.raises(SignatureInvalid):
            ours.verifier().verify(theirs.sign(mandate))

    @pytest.mark.parametrize("index", [0, 1, 31, 32, 62, 63])
    def test_flipping_any_signature_bit_fails(
        self, signer: MandateSigner, mandate: IntentMandate, index: int
    ) -> None:
        signed = signer.sign(mandate)
        corrupted = bytearray(signed.signature)
        corrupted[index] ^= 0x01
        with pytest.raises(SignatureInvalid):
            signer.verifier().verify(SignedMandate(mandate, bytes(corrupted)))

    def test_zero_signature_fails(self, signer: MandateSigner, mandate: IntentMandate) -> None:
        with pytest.raises(SignatureInvalid):
            signer.verifier().verify(SignedMandate(mandate, b"\x00" * 64))

    @pytest.mark.parametrize("length", [0, 1, 32, 63, 65, 128])
    def test_wrong_length_signature_is_refused_at_parse(
        self, signer: MandateSigner, mandate: IntentMandate, length: int
    ) -> None:
        wire = signer.sign(mandate).to_dict()
        wire["signature"] = ("ab" * 128)[: length * 2]
        with pytest.raises(MandateMalformed, match="64 bytes"):
            SignedMandate.from_dict(wire)

    def test_signature_swap_between_two_mandates(
        self, signer: MandateSigner, clock: FrozenClock
    ) -> None:
        """A genuine signature over a *cheap* mandate must not authorise an expensive one."""
        cheap = IntentMandate.create(clock=clock, subject="c", session_id="s", max_total=199900)
        pricey = IntentMandate.create(
            clock=clock, subject="c", session_id="s", max_total=ATTACK_AMOUNT
        )
        cheap_sig = signer.sign(cheap).signature
        with pytest.raises(SignatureInvalid):
            signer.verifier().verify(SignedMandate(pricey, cheap_sig))

    def test_non_hex_signature_refused(self, signer: MandateSigner, mandate: IntentMandate) -> None:
        wire = signer.sign(mandate).to_dict()
        wire["signature"] = "not-hex-at-all"
        with pytest.raises(MandateMalformed, match="hex"):
            SignedMandate.from_dict(wire)


class TestDomainSeparation:
    def test_signature_over_bare_json_does_not_verify(
        self, signer: MandateSigner, mandate: IntentMandate
    ) -> None:
        """A signature over the payload *without* the domain tag must be worthless.

        This is what stops a signature minted for some other PayNaka structure -- a policy
        blob, an audit record -- from being replayed as mandate authority.
        """
        body = canonical_bytes(mandate).split(b"|", 1)[1]
        rogue = signer._key.sign(body)
        with pytest.raises(SignatureInvalid):
            signer.verifier().verify(SignedMandate(mandate, rogue))

    def test_signature_under_a_different_domain_does_not_verify(
        self, signer: MandateSigner, mandate: IntentMandate
    ) -> None:
        body = canonical_bytes(mandate).split(b"|", 1)[1]
        rogue = signer._key.sign(b"paynaka.audit.v1|" + body)
        with pytest.raises(SignatureInvalid):
            signer.verifier().verify(SignedMandate(mandate, rogue))

    def test_domain_tag_is_versioned(self) -> None:
        assert DOMAIN.endswith(b".v1")


class TestCanonicalisationAttacks:
    def test_key_order_cannot_change_the_signed_bytes(self, mandate: IntentMandate) -> None:
        shuffled = dict(reversed(list(mandate.to_dict().items())))
        assert canonical_bytes(IntentMandate.from_dict(shuffled)) == canonical_bytes(mandate)

    def test_duplicate_json_keys_cannot_smuggle_a_second_value(
        self, signer: MandateSigner, mandate: IntentMandate
    ) -> None:
        """``{"max_total": 199900, "max_total": 5200000}`` -- last-wins in most parsers.

        Python keeps the last occurrence, so the forged value is what gets parsed. It must
        then fail the signature check, because the signature covers ₹1,999.
        """
        signed = signer.sign(mandate)
        body = json.dumps(mandate.to_dict())
        assert body.endswith("}")
        forged_body = body[:-1] + f', "max_total": {ATTACK_AMOUNT}}}'
        parsed = json.loads(forged_body)
        assert parsed["max_total"] == ATTACK_AMOUNT  # the parser did take the second one
        with pytest.raises(SignatureInvalid):
            signer.verifier().verify(
                SignedMandate(IntentMandate.from_dict(parsed), signed.signature)
            )

    def test_unicode_escaping_is_stable(self, clock: FrozenClock) -> None:
        """Two spellings of the same string must canonicalise identically."""
        a = IntentMandate.create(clock=clock, subject="café", session_id="s", max_total=1000)
        b = replace(a, subject="café")
        assert canonical_bytes(a) == canonical_bytes(b)


class TestStrictParsing:
    def test_unknown_field_is_refused(self, mandate: IntentMandate) -> None:
        """An ignored field is a field an attacker can use. Refuse, do not drop."""
        payload = mandate.to_dict() | {"max_total_override": ATTACK_AMOUNT}
        with pytest.raises(MandateMalformed, match="unknown field"):
            IntentMandate.from_dict(payload)

    @pytest.mark.parametrize(
        "extra",
        ["signature", "admin", "__class__", "allowed_actions_extra", "max_total ", " max_total"],
    )
    def test_various_smuggled_fields_refused(self, mandate: IntentMandate, extra: str) -> None:
        with pytest.raises(MandateMalformed, match="unknown field"):
            IntentMandate.from_dict(mandate.to_dict() | {extra: "x"})

    @pytest.mark.parametrize(
        "missing",
        ["max_total", "expires_at", "nonce", "allowed_actions", "subject", "currency"],
    )
    def test_missing_required_field_is_refused(self, mandate: IntentMandate, missing: str) -> None:
        payload = mandate.to_dict()
        del payload[missing]
        with pytest.raises(MandateMalformed, match="missing field"):
            IntentMandate.from_dict(payload)

    @pytest.mark.parametrize("payload", [None, [], "mandate", 42, True])
    def test_non_object_payload_refused(self, payload: object) -> None:
        with pytest.raises(MandateMalformed):
            IntentMandate.from_dict(payload)

    def test_signed_wrapper_rejects_extra_keys(
        self, signer: MandateSigner, mandate: IntentMandate
    ) -> None:
        wire = signer.sign(mandate).to_dict() | {"trusted": True}
        with pytest.raises(MandateMalformed, match="exactly"):
            SignedMandate.from_dict(wire)


class TestTypeConfusion:
    @pytest.mark.parametrize("value", ["199900", 199900.0, True, None, [199900], {"v": 199900}])
    def test_max_total_must_be_a_real_int(self, mandate: IntentMandate, value: object) -> None:
        with pytest.raises(MandateMalformed):
            IntentMandate.from_dict(mandate.to_dict() | {"max_total": value})

    @pytest.mark.parametrize("value", [0, -1, -199900])
    def test_non_positive_max_total_refused(self, mandate: IntentMandate, value: int) -> None:
        with pytest.raises(MandateMalformed, match=r"positive|valid amount"):
            IntentMandate.from_dict(mandate.to_dict() | {"max_total": value})

    def test_absurd_max_total_refused(self, mandate: IntentMandate) -> None:
        with pytest.raises(MandateMalformed, match="valid amount"):
            IntentMandate.from_dict(mandate.to_dict() | {"max_total": 10**20})

    @pytest.mark.parametrize("value", ["true", 1, 0, None, "yes"])
    def test_requires_return_must_be_a_real_bool(
        self, mandate: IntentMandate, value: object
    ) -> None:
        with pytest.raises(MandateMalformed, match="bool"):
            IntentMandate.from_dict(mandate.to_dict() | {"requires_return_for_refund": value})

    @pytest.mark.parametrize("value", ["ATTA-5KG", 42, None, {"a": 1}])
    def test_allow_lists_must_be_lists(self, mandate: IntentMandate, value: object) -> None:
        with pytest.raises(MandateMalformed, match="must be a list"):
            IntentMandate.from_dict(mandate.to_dict() | {"allowed_skus": value})

    @pytest.mark.parametrize("value", [[""], [None], [42], [["nested"]], [{"a": 1}]])
    def test_allow_list_entries_must_be_non_empty_strings(
        self, mandate: IntentMandate, value: list[object]
    ) -> None:
        with pytest.raises(MandateMalformed, match="non-empty strings"):
            IntentMandate.from_dict(mandate.to_dict() | {"allowed_skus": value})


class TestAuthorityWidening:
    """Structurally valid mandates that try to grant more than the system permits."""

    @pytest.mark.parametrize(
        "action",
        ["admin", "transfer_all", "create_order_unlimited", "CREATE_REFUND", "", "*"],
    )
    def test_unknown_action_refused(self, mandate: IntentMandate, action: str) -> None:
        with pytest.raises(MandateMalformed, match=r"unknown action|non-empty"):
            IntentMandate.from_dict(mandate.to_dict() | {"allowed_actions": [action]})

    def test_empty_action_list_is_legal_and_means_nothing_is_permitted(
        self, mandate: IntentMandate
    ) -> None:
        """Fail closed: an empty permission list grants nothing, rather than everything."""
        empty = IntentMandate.from_dict(mandate.to_dict() | {"allowed_actions": []})
        assert empty.allowed_actions == ()

    @pytest.mark.parametrize("qty", [0, -1, 10_001, 10**9])
    def test_qty_ceiling_out_of_range_refused(self, mandate: IntentMandate, qty: int) -> None:
        with pytest.raises(MandateMalformed, match=r"out of range|must be int"):
            IntentMandate.from_dict(mandate.to_dict() | {"max_qty_per_sku": qty})

    @pytest.mark.parametrize("currency", ["USD", "inr", "INR ", "", "EUR", "XXX"])
    def test_only_inr_is_supported(self, mandate: IntentMandate, currency: str) -> None:
        with pytest.raises(MandateMalformed, match=r"currency|non-empty"):
            IntentMandate.from_dict(mandate.to_dict() | {"currency": currency})

    def test_version_bump_is_refused(self, mandate: IntentMandate) -> None:
        """A future version might mean something different. Refuse rather than guess."""
        with pytest.raises(MandateMalformed, match="version"):
            IntentMandate.from_dict(mandate.to_dict() | {"version": 2})

    def test_duplicate_allow_list_entries_refused(self, mandate: IntentMandate) -> None:
        """Duplicates are a smell: at best sloppy, at worst a parser-confusion probe."""
        with pytest.raises(MandateMalformed, match="duplicate"):
            IntentMandate.from_dict(mandate.to_dict() | {"allowed_skus": ["ATTA-5KG", "ATTA-5KG"]})


class TestResourceExhaustion:
    def test_oversized_allow_list_refused(self, mandate: IntentMandate) -> None:
        with pytest.raises(MandateMalformed, match="exceeds"):
            IntentMandate.from_dict(
                mandate.to_dict() | {"allowed_skus": [f"SKU-{i}" for i in range(257)]}
            )

    def test_oversized_string_field_refused(self, mandate: IntentMandate) -> None:
        with pytest.raises(MandateMalformed, match="exceeds"):
            IntentMandate.from_dict(mandate.to_dict() | {"subject": "A" * 257})

    def test_oversized_allow_list_entry_refused(self, mandate: IntentMandate) -> None:
        with pytest.raises(MandateMalformed, match="exceeds"):
            IntentMandate.from_dict(mandate.to_dict() | {"allowed_skus": ["S" * 257]})


class TestTemporalAttacks:
    def test_expired_mandate_is_refused_even_though_signed(
        self, signer: MandateSigner, clock: FrozenClock, mandate: IntentMandate
    ) -> None:
        """A perfectly genuine signature does not make a stale mandate usable."""
        signed = signer.sign(mandate)
        clock.advance(hours=2)
        assert signer.verifier().verify(signed) == mandate  # signature is still fine
        with pytest.raises(MandateExpired):
            mandate.assert_live(clock)

    def test_future_stamped_mandate_is_refused(
        self, clock: FrozenClock, mandate: IntentMandate
    ) -> None:
        """Only a clock skew or a forgery produces one. Both deserve a refusal."""
        clock.advance(hours=-2)
        with pytest.raises(MandateExpired, match="future"):
            mandate.assert_live(clock)

    def test_expiry_before_issue_refused_at_construction(self, mandate: IntentMandate) -> None:
        payload = mandate.to_dict()
        payload["expires_at"] = payload["issued_at"] - 1
        with pytest.raises(MandateMalformed, match="after issued_at"):
            IntentMandate.from_dict(payload)

    def test_zero_length_lifetime_refused(self, mandate: IntentMandate) -> None:
        payload = mandate.to_dict()
        payload["expires_at"] = payload["issued_at"]
        with pytest.raises(MandateMalformed, match="after issued_at"):
            IntentMandate.from_dict(payload)

    def test_immortal_mandate_refused(self, mandate: IntentMandate) -> None:
        """A mandate valid for a year is a standing authorisation, not an intent."""
        payload = mandate.to_dict()
        payload["expires_at"] = payload["issued_at"] + 86_401
        with pytest.raises(MandateMalformed, match="24h"):
            IntentMandate.from_dict(payload)

    @pytest.mark.parametrize("ttl", [0, -1, 86_401, 10**9])
    def test_bad_ttl_refused_at_create(self, clock: FrozenClock, ttl: int) -> None:
        with pytest.raises(MandateMalformed):
            IntentMandate.create(
                clock=clock, subject="c", session_id="s", max_total=1000, ttl_seconds=ttl
            )


class TestImmutability:
    def test_mandate_cannot_be_mutated_in_place(self, mandate: IntentMandate) -> None:
        """If a mandate could be edited after verification, verification would be theatre."""
        with pytest.raises((AttributeError, TypeError)):
            mandate.max_total = ATTACK_AMOUNT  # type: ignore[misc]

    def test_allow_lists_are_tuples_not_lists(self, mandate: IntentMandate) -> None:
        assert isinstance(mandate.allowed_skus, tuple)
        with pytest.raises(AttributeError):
            mandate.allowed_skus.append("GIFT-50K")  # type: ignore[attr-defined]

    def test_to_dict_returns_a_copy(self, mandate: IntentMandate) -> None:
        """Mutating the exported dict must not reach back into the mandate."""
        exported = mandate.to_dict()
        exported["max_total"] = ATTACK_AMOUNT
        exported["allowed_skus"].append("GIFT-50K")
        assert mandate.max_total == 199900
        assert mandate.allowed_skus == ("ATTA-5KG",)


class TestVerifierApiSafety:
    @pytest.mark.parametrize("value", [None, "signed", 42, {"mandate": {}}])
    def test_verify_refuses_non_signed_mandate(self, signer: MandateSigner, value: object) -> None:
        with pytest.raises(MandateMalformed, match="expected SignedMandate"):
            signer.verifier().verify(value)  # type: ignore[arg-type]

    def test_verifier_cannot_sign(self, signer: MandateSigner) -> None:
        """A leaked verifier must not be upgradeable into a minting capability."""
        verifier = signer.verifier()
        assert not hasattr(verifier, "sign")
        assert not hasattr(verifier, "_private_key")
