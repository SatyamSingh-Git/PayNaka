"""Reference prices: the gap between "inside the budget" and "what was agreed".

A budget bounds the total. It does not bound the price of the thing, and those come apart
whenever the budget is looser than the price -- which is nearly always, because shoppers
say round numbers. "Atta, under two and a half thousand" against a bag listed at Rs 1,999
hands a merchant Rs 501 of room to reprice into, and every rupee of it is authorised.

That is the quantifiable half of *bad-but-authorised*. The mandate now carries what the
shopper was shown, frozen at the same moment as everything else, and the gate compares
against it.

The other half is not closed and no test here pretends it is: an agent steered into a
worse seller, or a worse product at an honest price, is still a real loss and still
authorised. Price is checkable; judgment is not, and inventing a number for it would be
inventing an intent the shopper never expressed.
"""

from __future__ import annotations

import pytest

from haat.runner import _fresh_stack
from paynaka.clock import FrozenClock
from paynaka.gate import LineItem, MoneyRequest, Verdict
from paynaka.mandate import IntentMandate, MandateMalformed
from paynaka.money import MAX_PAISE

pytestmark = pytest.mark.adversarial

LISTED = 199_900  # Rs 1,999 -- what the page said when the shopper agreed
BUDGET = 250_000  # Rs 2,500 -- what they actually authorised, being a person


def _stack(seed: str, *, refs: dict[str, int], tolerance: int = 0, budget: int = BUDGET):  # type: ignore[no-untyped-def]
    naka, signer, _rail, clock = _fresh_stack(seed)
    mandate = IntentMandate.create(
        clock=clock,
        subject="cust_1",
        session_id="sess_1",
        max_total=budget,
        allowed_skus=("ATTA-5KG", "GHEE-1L"),
        allowed_destinations=("addr_home",),
        max_qty_per_sku=3,
        allowed_actions=("create_order",),
        reference_prices=refs,
        price_tolerance_bps=tolerance,
    )
    return naka, signer.sign(mandate)


def _order(unit: int, *, sku: str = "ATTA-5KG", qty: int = 1, n: int = 1) -> MoneyRequest:
    return MoneyRequest(
        action="create_order",
        request_id=f"r{n}",
        idempotency_key=f"k{n}",
        items=(LineItem(sku=sku, qty=qty, unit_paise=unit),),
        destination="addr_home",
    )


# ====================================================================== the gap


class TestTheSkimInsideTheBudget:
    def test_without_a_reference_the_skim_is_authorised(self) -> None:
        """The gap, demonstrated before it is closed.

        Rs 2,098 against a Rs 2,500 budget for a bag listed at Rs 1,999. Nothing the
        shopper said is violated. They are simply a hundred rupees down.
        """
        naka, signed = _stack("no-ref", refs={})
        decision = naka.execute(_order(209_895), signed).decision
        assert decision.check_id != "envelope.price_moved"
        assert decision.verdict is not Verdict.DENY

    def test_with_a_reference_it_is_refused(self) -> None:
        naka, signed = _stack("ref", refs={"ATTA-5KG": LISTED})
        result = naka.execute(_order(209_895), signed)

        assert result.decision.verdict is Verdict.DENY
        assert result.decision.check_id == "envelope.price_moved"
        assert result.money_moved == 0

    def test_the_denial_says_both_numbers(self) -> None:
        """An operator has to be able to see what moved and by how much."""
        naka, signed = _stack("evidence", refs={"ATTA-5KG": LISTED})
        evidence = naka.execute(_order(209_895), signed).decision.evidence
        assert evidence["reference"] == LISTED
        assert evidence["presented"] == 209_895
        assert evidence["sku"] == "ATTA-5KG"

    @pytest.mark.parametrize("unit", [LISTED + 1, 209_895, 250_000, MAX_PAISE // 2])
    def test_any_rise_above_the_reference_is_refused(self, unit: int) -> None:
        naka, signed = _stack(f"rise-{unit}", refs={"ATTA-5KG": LISTED}, budget=MAX_PAISE)
        assert naka.execute(_order(unit), signed).decision.check_id == "envelope.price_moved"


class TestItDoesNotRefuseHonestTraffic:
    def test_the_reference_price_itself_goes_through(self) -> None:
        naka, signed = _stack("exact", refs={"ATTA-5KG": LISTED})
        result = naka.execute(_order(LISTED), signed)
        assert result.decision.verdict is Verdict.ALLOW
        assert result.money_moved == LISTED

    @pytest.mark.parametrize("unit", [1, 99_950, LISTED - 1])
    def test_a_price_that_fell_goes_through(self, unit: int) -> None:
        """A sale is not an attack. A gate that refuses one is an outage."""
        naka, signed = _stack(f"sale-{unit}", refs={"ATTA-5KG": LISTED})
        assert naka.execute(_order(unit), signed).decision.verdict is Verdict.ALLOW

    def test_a_sku_with_no_reference_is_unaffected(self) -> None:
        """Only what the shopper was actually shown is bounded by what they were shown.

        Asserted as "not a price denial" rather than "allowed", because the merchant
        policy has its own ceiling and step-up band on orders. Those are separate bounds
        with their own tests; the claim here is that *this* check stayed quiet.
        """
        naka, signed = _stack("partial", refs={"ATTA-5KG": LISTED}, budget=MAX_PAISE)
        decision = naka.execute(_order(400_000, sku="GHEE-1L"), signed).decision
        assert decision.check_id != "envelope.price_moved"

    def test_an_empty_reference_set_disables_the_check_entirely(self) -> None:
        """An open-ended budget is a shopper who genuinely said "anything under X"."""
        naka, signed = _stack("open", refs={}, budget=MAX_PAISE)
        decision = naka.execute(_order(400_000), signed).decision
        assert decision.check_id != "envelope.price_moved"

    def test_quantity_does_not_confuse_it(self) -> None:
        """The bound is per unit, so buying three at the agreed price is still agreed."""
        naka, signed = _stack("qty", refs={"ATTA-5KG": 49_900}, budget=MAX_PAISE)
        result = naka.execute(_order(49_900, qty=3), signed)
        assert result.decision.verdict is Verdict.ALLOW
        assert result.money_moved == 149_700


class TestTolerance:
    @pytest.mark.parametrize(
        ("bps", "unit", "allowed"),
        [
            (0, LISTED, True),  # exactly, with no slack
            (0, LISTED + 1, False),
            (100, 201_899, True),  # 1% of Rs 1,999 is Rs 19.99
            (100, 201_900, False),
            (200, 203_898, True),
            (200, 203_899, False),
            (10_000, 399_800, True),  # 100%: the ceiling is double
            (10_000, 399_801, False),
        ],
    )
    def test_the_band_is_exact_at_the_boundary(self, bps: int, unit: int, allowed: bool) -> None:
        """Integer basis points, integer paise, no float anywhere near the comparison."""
        naka, signed = _stack(
            f"tol-{bps}-{unit}", refs={"ATTA-5KG": LISTED}, tolerance=bps, budget=MAX_PAISE
        )
        verdict = naka.execute(_order(unit), signed).decision.verdict
        assert (verdict is not Verdict.DENY) is allowed

    def test_the_ceiling_is_computed_with_integer_arithmetic(self) -> None:
        clock = FrozenClock.at_ist("2026-08-24 10:00")
        mandate = IntentMandate.create(
            clock=clock,
            subject="s",
            session_id="x",
            max_total=MAX_PAISE,
            reference_prices={"A": 199_900},
            price_tolerance_bps=333,
        )
        ceiling = mandate.price_ceiling_for("A")
        assert isinstance(ceiling, int) and not isinstance(ceiling, bool)
        assert ceiling == 199_900 + (199_900 * 333) // 10_000


class TestTheMandateHoldsItSafely:
    def test_the_reference_is_covered_by_the_signature(self) -> None:
        """Otherwise an attacker could raise the ceiling on a mandate somebody else signed."""
        import dataclasses

        from paynaka.mandate import MandateSigner, SignatureInvalid, generate_keypair

        clock = FrozenClock.at_ist("2026-08-24 10:00")
        signer = MandateSigner(generate_keypair()[0])
        mandate = IntentMandate.create(
            clock=clock,
            subject="s",
            session_id="x",
            max_total=BUDGET,
            reference_prices={"ATTA-5KG": LISTED},
        )
        signed = signer.sign(mandate)
        tampered = dataclasses.replace(
            signed, mandate=dataclasses.replace(mandate, reference_prices=(("ATTA-5KG", 900_000),))
        )
        with pytest.raises(SignatureInvalid):
            signer.verifier().verify(tampered)

    def test_the_tolerance_is_covered_too(self) -> None:
        import dataclasses

        from paynaka.mandate import MandateSigner, SignatureInvalid, generate_keypair

        clock = FrozenClock.at_ist("2026-08-24 10:00")
        signer = MandateSigner(generate_keypair()[0])
        mandate = IntentMandate.create(
            clock=clock,
            subject="s",
            session_id="x",
            max_total=BUDGET,
            reference_prices={"ATTA-5KG": LISTED},
        )
        signed = signer.sign(mandate)
        tampered = dataclasses.replace(
            signed, mandate=dataclasses.replace(mandate, price_tolerance_bps=9_000)
        )
        with pytest.raises(SignatureInvalid):
            signer.verifier().verify(tampered)

    def test_order_does_not_change_the_signed_bytes(self) -> None:
        """One intent, one encoding, whatever order the caller happened to supply."""
        clock = FrozenClock.at_ist("2026-08-24 10:00")
        common = {
            "clock": clock,
            "subject": "s",
            "session_id": "x",
            "max_total": BUDGET,
        }
        a = IntentMandate.create(**common, reference_prices=(("B", 2), ("A", 1)))  # type: ignore[arg-type]
        b = IntentMandate.create(**common, reference_prices=(("A", 1), ("B", 2)))  # type: ignore[arg-type]
        assert a.to_dict()["reference_prices"] == b.to_dict()["reference_prices"]

    def test_it_round_trips_through_json(self) -> None:
        clock = FrozenClock.at_ist("2026-08-24 10:00")
        mandate = IntentMandate.create(
            clock=clock,
            subject="s",
            session_id="x",
            max_total=BUDGET,
            reference_prices={"ATTA-5KG": LISTED, "GHEE-1L": 45_000},
            price_tolerance_bps=150,
        )
        assert IntentMandate.from_dict(mandate.to_dict()) == mandate

    @pytest.mark.parametrize(
        ("refs", "match"),
        [
            ((("A", 0),), "positive"),
            ((("A", -1),), "positive"),
            ((("A", 1.5),), "int paise"),
            ((("A", True),), "int paise"),
            ((("", 1),), "malformed"),
            (((None, 1),), "malformed"),
            ((("A", 1), ("A", 2)), "duplicate"),
            ((("A",),), "pair"),
            ((("A", 1, 2),), "pair"),
        ],
    )
    def test_a_malformed_reference_is_refused(self, refs: object, match: str) -> None:
        clock = FrozenClock.at_ist("2026-08-24 10:00")
        with pytest.raises(MandateMalformed, match=match):
            IntentMandate.create(
                clock=clock,
                subject="s",
                session_id="x",
                max_total=BUDGET,
                reference_prices=refs,  # type: ignore[arg-type]
            )

    @pytest.mark.parametrize("bps", [-1, 10_001, 1.5, True, "100"])
    def test_a_malformed_tolerance_is_refused(self, bps: object) -> None:
        clock = FrozenClock.at_ist("2026-08-24 10:00")
        with pytest.raises(MandateMalformed):
            IntentMandate.create(
                clock=clock,
                subject="s",
                session_id="x",
                max_total=BUDGET,
                price_tolerance_bps=bps,  # type: ignore[arg-type]
            )

    def test_a_huge_reference_list_is_refused(self) -> None:
        """An allow-list is a DoS vector if nothing bounds its size."""
        clock = FrozenClock.at_ist("2026-08-24 10:00")
        with pytest.raises(MandateMalformed, match="exceeds"):
            IntentMandate.create(
                clock=clock,
                subject="s",
                session_id="x",
                max_total=BUDGET,
                reference_prices=tuple((f"SKU-{i}", 100) for i in range(500)),
            )


class TestWhatItDoesNotClose:
    def test_a_worse_product_at_an_honest_price_is_still_authorised(self) -> None:
        """Stated as a test so nobody reads this feature as more than it is.

        The shopper wanted atta and said "under Rs 2,500". The agent buys the ghee, which
        is on the allow-list, at exactly its reference price. That is a worse outcome and
        it is entirely authorised. Price is checkable; judgment is not, and a number
        invented for it would be an intent the shopper never expressed.
        """
        naka, signed = _stack(
            "judgment", refs={"ATTA-5KG": LISTED, "GHEE-1L": 45_000}, budget=BUDGET
        )
        result = naka.execute(_order(45_000, sku="GHEE-1L", qty=3), signed)
        assert result.decision.verdict is Verdict.ALLOW
        assert result.money_moved == 135_000
