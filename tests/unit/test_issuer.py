"""Forward tests for mandate issuance: does stated intent become the mandate it should?

The hostile half -- widening, malformed intent, key custody -- lives in
``tests/adversarial/test_issuer_adversarial.py``. This file establishes that an ordinary
shopping intent produces a working mandate at all, without which every refusal test over
there would pass against an issuer that refuses everything.
"""

from __future__ import annotations

import pytest

from paynaka.clock import FrozenClock
from paynaka.issuer import Issuer, ShopperIntent
from paynaka.mandate import MandateSigner, MandateVerifier, generate_keypair

ATTA = "ATTA-5KG"
HOME = "addr_home"


@pytest.fixture
def issuer(signer: MandateSigner) -> Issuer:
    return Issuer(signer)


def an_intent(**overrides: object) -> ShopperIntent:
    """The demo scenario: ₹1,999 for one bag of atta, delivered home."""
    fields: dict[str, object] = {
        "subject": "cust_kirana_001",
        "session_id": "sess_demo",
        "budget_paise": 199_900,
        "skus": (ATTA,),
        "destinations": (HOME,),
        "max_qty_per_sku": 1,
    }
    fields.update(overrides)
    return ShopperIntent(**fields)  # type: ignore[arg-type]


class TestIssuing:
    def test_the_mandate_says_what_the_shopper_said(
        self, issuer: Issuer, clock: FrozenClock
    ) -> None:
        issued = issuer.issue(an_intent(), clock=clock)
        mandate = issued.signed.mandate
        assert mandate.max_total == 199_900
        assert mandate.allowed_skus == (ATTA,)
        assert mandate.allowed_destinations == (HOME,)
        assert mandate.max_qty_per_sku == 1
        assert mandate.currency == "INR"

    def test_the_signature_verifies_against_the_public_key(
        self, issuer: Issuer, signer: MandateSigner, clock: FrozenClock
    ) -> None:
        """The end-to-end point of the module: what comes out is usable by the gate."""
        issued = issuer.issue(an_intent(), clock=clock)
        verified = signer.verifier().verify(issued.signed)
        assert verified.mandate_id == issued.signed.mandate.mandate_id

    def test_refunds_are_not_granted_unless_asked_for(
        self, issuer: Issuer, clock: FrozenClock
    ) -> None:
        """ "Buy me atta" is not "and you may issue refunds"."""
        plain = issuer.issue(an_intent(), clock=clock).signed.mandate
        assert "create_refund" not in plain.allowed_actions

        asked = issuer.issue(an_intent(allow_refunds=True), clock=clock).signed.mandate
        assert "create_refund" in asked.allowed_actions

    def test_the_window_comes_from_the_intent(self, issuer: Issuer, clock: FrozenClock) -> None:
        issued = issuer.issue(an_intent(ttl_seconds=300), clock=clock)
        mandate = issued.signed.mandate
        assert mandate.expires_at - mandate.issued_at == 300

    def test_reference_prices_are_carried_through(self, issuer: Issuer, clock: FrozenClock) -> None:
        """The budget bounds the basket; the reference bounds the thing."""
        issued = issuer.issue(
            an_intent(reference_prices=((ATTA, 199_900),), price_tolerance_bps=100), clock=clock
        )
        assert issued.signed.mandate.reference_for(ATTA) == 199_900

    def test_two_issues_produce_different_mandates(
        self, issuer: Issuer, clock: FrozenClock
    ) -> None:
        """Single-use authority. Two shopping trips are two mandates and two nonces."""
        first = issuer.issue(an_intent(), clock=clock).signed.mandate
        second = issuer.issue(an_intent(), clock=clock).signed.mandate
        assert first.mandate_id != second.mandate_id
        assert first.nonce != second.nonce


class TestTheRecordItLeaves:
    def test_it_records_when_intent_was_frozen(self, issuer: Issuer, clock: FrozenClock) -> None:
        """The design's claim is that intent is captured *before* exposure. This is what
        makes that ordering checkable rather than narrated."""
        issued = issuer.issue(an_intent(), clock=clock)
        assert issued.frozen_at == clock.epoch()

    def test_the_intent_travels_with_the_mandate(self, issuer: Issuer, clock: FrozenClock) -> None:
        """So a review can read what the shopper said beside what the system was told."""
        issued = issuer.issue(an_intent(), clock=clock)
        assert issued.intent.budget_paise == 199_900
        assert issued.to_dict()["intent"]["skus"] == [ATTA]

    def test_the_dict_carries_no_private_key_material(
        self, issuer: Issuer, clock: FrozenClock
    ) -> None:
        rendered = str(issuer.issue(an_intent(), clock=clock).to_dict())
        assert "PRIVATE" not in rendered.upper()


class TestKeyCustody:
    def test_what_the_issuer_hands_downstream_cannot_sign(self, issuer: Issuer) -> None:
        """The separation the whole design rests on: a compromised gate can refuse a
        mandate and cannot mint one."""
        handed = issuer.public_key_holder
        assert isinstance(handed, MandateVerifier)
        assert not hasattr(handed, "sign")

    def test_a_mandate_from_another_issuer_does_not_verify(
        self, issuer: Issuer, clock: FrozenClock
    ) -> None:
        other = Issuer(MandateSigner(generate_keypair()[0]))
        issued = other.issue(an_intent(), clock=clock)
        with pytest.raises(Exception, match="signature does not verify"):
            issuer.public_key_holder.verify(issued.signed)
