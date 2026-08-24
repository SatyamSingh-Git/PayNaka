"""Attacks on the thing that grants authority in the first place.

Everything downstream checks a mandate against a request. The issuer is upstream of all of
it: whatever it signs *is* the authority, and no later check can notice that too much was
granted, because too much was granted legitimately. That makes it the highest-leverage
component in the system and the one with the least to check it.

So the tests are arranged around the ways authority gets wider than anybody said:

* **Widening** -- the issued mandate permitting more than the stated intent, by any field.
* **Malformed intent** -- an unbounded allow-list, an absurd window, a budget that is a
  typo, all of which produce a mandate that is technically valid and practically a blank
  cheque.
* **Custody** -- the gate acquiring the ability to mint what it is supposed to check.
"""

from __future__ import annotations

import pytest

from paynaka.clock import FrozenClock
from paynaka.issuer import (
    MAX_TTL_SECONDS,
    MIN_BUDGET_PAISE,
    Issuer,
    IssuerError,
    ShopperIntent,
    _assert_not_widened,
)
from paynaka.mandate import IntentMandate, MandateSigner, generate_keypair

pytestmark = pytest.mark.adversarial

ATTA = "ATTA-5KG"
GIFT = "GIFT-50K"
HOME = "addr_home"
ELSEWHERE = "addr_attacker"


@pytest.fixture
def issuer(signer: MandateSigner) -> Issuer:
    return Issuer(signer)


def an_intent(**overrides: object) -> ShopperIntent:
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


# ============================================================== unbounded authority
class TestAnUnboundedIntentIsRefused:
    def test_an_empty_sku_list_is_a_blank_cheque(self) -> None:
        """A mandate with no SKU allow-list permits any item inside the budget, which is
        exactly the attack the whole envelope exists to stop."""
        with pytest.raises(IssuerError, match="blank cheque"):
            an_intent(skus=())

    def test_an_empty_destination_list_lets_goods_go_anywhere(self) -> None:
        with pytest.raises(IssuerError, match="goods go anywhere"):
            an_intent(destinations=())

    @pytest.mark.parametrize("budget", [0, 1, 99, MIN_BUDGET_PAISE - 1, -1, -199_900])
    def test_a_budget_that_is_a_typo_is_refused(self, budget: int) -> None:
        with pytest.raises(IssuerError):
            an_intent(budget_paise=budget)

    @pytest.mark.parametrize("budget", [MIN_BUDGET_PAISE, MIN_BUDGET_PAISE + 1, 10**12])
    def test_a_real_budget_is_accepted(self, budget: int) -> None:
        """The other side of the floor, so it is a floor and not a wall."""
        assert an_intent(budget_paise=budget).budget_paise == budget

    @pytest.mark.parametrize("ttl", [0, -1, MAX_TTL_SECONDS + 1, MAX_TTL_SECONDS * 365, 10**9])
    def test_an_absurd_window_is_refused(self, ttl: int) -> None:
        """Nobody shops for a year. A wide window is authority left lying around."""
        with pytest.raises(IssuerError, match="ttl_seconds"):
            an_intent(ttl_seconds=ttl)

    @pytest.mark.parametrize("ttl", [1, 900, MAX_TTL_SECONDS])
    def test_a_reasonable_window_is_accepted(self, ttl: int) -> None:
        assert an_intent(ttl_seconds=ttl).ttl_seconds == ttl

    @pytest.mark.parametrize("qty", [0, -1, -99])
    def test_a_nonsensical_quantity_ceiling_is_refused(self, qty: int) -> None:
        with pytest.raises(IssuerError, match="max_qty_per_sku"):
            an_intent(max_qty_per_sku=qty)

    @pytest.mark.parametrize(
        ("field", "value"),
        [("subject", ""), ("subject", "   "), ("session_id", ""), ("session_id", "  ")],
        ids=repr,
    )
    def test_an_unattributable_intent_is_refused(self, field: str, value: str) -> None:
        """An unattributable mandate cannot be revoked, counted against a breaker, or
        explained to anybody afterwards."""
        with pytest.raises(IssuerError, match="needs a"):
            an_intent(**{field: value})

    @pytest.mark.parametrize("budget", [True, False, 1999.0, "199900", None, [199_900]], ids=repr)
    def test_a_budget_that_is_not_int_paise_is_refused(self, budget: object) -> None:
        """``True`` is an ``int`` in Python. A one-paisa mandate from a boolean is a
        number somebody would spend an afternoon explaining."""
        with pytest.raises(IssuerError):
            an_intent(budget_paise=budget)


class TestAReferencePriceCannotSmuggleAnything:
    def test_it_cannot_name_a_sku_the_shopper_did_not_ask_for(self) -> None:
        """Otherwise the reference list becomes a second, unchecked allow-list."""
        with pytest.raises(IssuerError, match="did not ask for"):
            an_intent(reference_prices=((GIFT, 5_000_000),))

    @pytest.mark.parametrize("price", [0, -1, True, "1999", None], ids=repr)
    def test_a_nonsensical_reference_price_is_refused(self, price: object) -> None:
        with pytest.raises(IssuerError):
            an_intent(reference_prices=((ATTA, price),))


# ============================================================== widening
class TestTheIssuedMandateNeverExceedsTheIntent:
    @pytest.mark.parametrize(
        "intent",
        [
            an_intent(),
            an_intent(budget_paise=5_000_000, skus=(ATTA, GIFT)),
            an_intent(max_qty_per_sku=9, destinations=(HOME, ELSEWHERE)),
            an_intent(allow_refunds=True),
            an_intent(currency="INR", ttl_seconds=60),
        ],
        ids=["demo", "wide-basket", "many-destinations", "refunds", "short-window"],
    )
    def test_every_field_stays_inside_what_was_said(
        self, issuer: Issuer, clock: FrozenClock, intent: ShopperIntent
    ) -> None:
        mandate = issuer.issue(intent, clock=clock).signed.mandate
        assert mandate.max_total <= intent.budget_paise
        assert set(mandate.allowed_skus) <= set(intent.skus)
        assert set(mandate.allowed_destinations) <= set(intent.destinations)
        assert mandate.max_qty_per_sku <= intent.max_qty_per_sku
        assert mandate.currency == intent.currency
        if not intent.allow_refunds:
            assert "create_refund" not in mandate.allowed_actions

    @pytest.mark.parametrize(
        ("overrides", "expected"),
        [
            ({"max_total": 5_000_000}, "exceeds the stated"),
            ({"allowed_skus": (ATTA, GIFT)}, "SKUs nobody asked for"),
            ({"allowed_destinations": (HOME, ELSEWHERE)}, "destinations nobody asked for"),
            ({"max_qty_per_sku": 99}, "quantity ceiling"),
            (
                {"allowed_actions": ("create_order", "create_refund")},
                "permits refunds and the shopper did not",
            ),
        ],
        ids=[
            "budget",
            "extra-sku",
            "extra-destination",
            "quantity",
            "refunds",
        ],
    )
    def test_the_self_audit_catches_a_mandate_that_grants_more(
        self, clock: FrozenClock, overrides: dict[str, object], expected: str
    ) -> None:
        """The guard that exists because the mapping is short and obvious *today*. The way
        this goes wrong later is a convenience default granting a little more than asked,
        so the issuer audits its own output on every issue.

        Driven directly, because the correct issuer cannot produce these -- which is the
        point: if this test ever passes trivially, the guard has stopped guarding.
        """
        intent = an_intent()
        base = IntentMandate.create(
            clock=clock,
            subject=intent.subject,
            session_id=intent.session_id,
            max_total=intent.budget_paise,
            allowed_skus=intent.skus,
            allowed_destinations=intent.destinations,
            max_qty_per_sku=intent.max_qty_per_sku,
            allowed_actions=("create_order", "capture_payment"),
        )
        import dataclasses

        widened = dataclasses.replace(base, **overrides)  # type: ignore[arg-type]
        with pytest.raises(IssuerError, match=expected):
            _assert_not_widened(widened, intent)

    def test_currency_widening_is_unreachable_and_still_guarded(self, clock: FrozenClock) -> None:
        """The one widening the self-audit checks that cannot actually be constructed.

        ``IntentMandate`` refuses any currency but INR in ``__post_init__``, so there is no
        way to build a mandate whose currency differs from the intent's. The issuer's check
        stays anyway: it costs nothing, and the day a second currency is supported is the
        day that guard becomes the only thing standing between "the shopper said INR" and
        a mandate that says otherwise.
        """
        from paynaka.mandate import MandateMalformed

        with pytest.raises(MandateMalformed, match="unsupported currency"):
            IntentMandate.create(
                clock=clock,
                subject="c",
                session_id="s",
                max_total=199_900,
                currency="USD",
                allowed_skus=(ATTA,),
                allowed_destinations=(HOME,),
            )

    def test_the_self_audit_passes_an_honest_mandate(
        self, issuer: Issuer, clock: FrozenClock
    ) -> None:
        """The control. A guard that rejects everything would satisfy every test above."""
        intent = an_intent()
        _assert_not_widened(issuer.issue(intent, clock=clock).signed.mandate, intent)


# ============================================================== custody
class TestTheGateCannotMintWhatItChecks:
    def test_the_handed_out_object_has_no_signing_capability(self, issuer: Issuer) -> None:
        handed = issuer.public_key_holder
        assert not hasattr(handed, "sign")
        assert not hasattr(handed, "_signer")
        # It holds a public key and nothing else that could reconstruct a private one.
        assert not any("private" in name.lower() for name in dir(handed))

    def test_a_mandate_forged_without_the_key_does_not_verify(
        self, issuer: Issuer, clock: FrozenClock
    ) -> None:
        from paynaka.mandate import SignedMandate

        genuine = issuer.issue(an_intent(), clock=clock).signed
        forged = SignedMandate(mandate=genuine.mandate, signature=b"\x00" * 64)
        with pytest.raises(Exception, match="signature does not verify"):
            issuer.public_key_holder.verify(forged)

    def test_a_tampered_mandate_does_not_verify_under_its_own_signature(
        self, issuer: Issuer, clock: FrozenClock
    ) -> None:
        """The whole point of signing: raising the budget after issue invalidates it."""
        import dataclasses

        from paynaka.mandate import SignedMandate

        genuine = issuer.issue(an_intent(), clock=clock).signed
        raised = dataclasses.replace(genuine.mandate, max_total=5_000_000)
        with pytest.raises(Exception, match="signature does not verify"):
            issuer.public_key_holder.verify(
                SignedMandate(mandate=raised, signature=genuine.signature)
            )

    def test_two_issuers_do_not_share_authority(self, clock: FrozenClock) -> None:
        first = Issuer(MandateSigner(generate_keypair()[0]))
        second = Issuer(MandateSigner(generate_keypair()[0]))
        issued = first.issue(an_intent(), clock=clock)
        with pytest.raises(Exception, match="signature does not verify"):
            second.public_key_holder.verify(issued.signed)
