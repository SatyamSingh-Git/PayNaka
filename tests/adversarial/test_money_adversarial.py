"""Adversarial tests for paynaka.money -- how does it break?

Money parsing sits directly on the attack surface: a poisoned catalog controls the price
string a buying agent reads. Currency confusion (rendering ``$1,999`` where ``₹1,999`` is
meant) is an attack family in HAAT, and it starts here. Anything ambiguous must raise
rather than guess, because a guess in this module is money leaving an account.
"""

from __future__ import annotations

import contextlib
from decimal import Decimal

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from paynaka.money import (
    MAX_PAISE,
    MoneyError,
    add,
    format_inr,
    mul_qty,
    parse_rupee_string,
    to_paise,
)

pytestmark = pytest.mark.adversarial


class TestFloatsAreRefused:
    """The single most important property in this module."""

    @pytest.mark.parametrize("value", [0.0, 1.0, 19.99, 1999.0, 0.1 + 0.2, -5.5, 1e10])
    def test_float_never_becomes_money(self, value: float) -> None:
        with pytest.raises(MoneyError, match="float"):
            to_paise(value)  # type: ignore[arg-type]

    def test_the_classic_rounding_error_cannot_enter(self) -> None:
        assert 0.1 + 0.2 != 0.3  # the reason this rule exists
        with pytest.raises(MoneyError):
            to_paise(0.1 + 0.2)  # type: ignore[arg-type]

    @pytest.mark.parametrize("value", [True, False])
    def test_bool_is_not_an_amount(self, value: bool) -> None:
        """bool subclasses int; ``to_paise(True) == 1`` would be a silent disaster."""
        with pytest.raises(MoneyError, match="bool"):
            to_paise(value)


class TestCurrencyConfusion:
    """HAAT family: 'currency confusion'. A foreign symbol must never be read as INR."""

    @pytest.mark.parametrize(
        "text",
        [
            "$1999",
            "$1,999.00",
            "USD 1999",
            "€1999",
            "£1999",
            "¥1999",
            "1999 USD",
            "1999$",
            "US$1999",
        ],
    )
    def test_foreign_currency_is_refused_not_coerced(self, text: str) -> None:
        with pytest.raises(MoneyError):
            parse_rupee_string(text)


class TestMalformedInput:
    @pytest.mark.parametrize(
        "text",
        [
            "",
            "   ",
            "abc",
            "₹",
            "Rs",
            ".",
            ".50",  # no whole part
            "1999.",  # trailing dot
            "1999.999",  # sub-paise precision
            "1999.1234",
            "1,99,9",  # nonsense grouping
            "1999 1999",
            "1999\n1999",
            "--1999",
            "+1999",  # explicit sign not accepted; ambiguity is refused
            "-1999",  # negative price is not a thing a catalog may assert
            "1e5",  # scientific notation
            "0x1999",
            "١٩٩٩",  # arabic-indic digits
            "१९९९",  # devanagari digits
            "१,९९९",
            "1999₹1999",
            "NaN",
            "Infinity",
            "inf",
        ],
    )
    def test_garbage_raises_rather_than_guessing(self, text: str) -> None:
        with pytest.raises(MoneyError):
            parse_rupee_string(text)

    @pytest.mark.parametrize(
        "text",
        [
            "1999​",  # zero-width space
            "1999‍",  # zero-width joiner
            "‮1999",  # right-to-left override
            "1٠9٠9٠9",  # arabic-indic zero interleaved
            "1999﻿",  # BOM
            "1999 ",  # non-breaking space
        ],
    )
    def test_invisible_characters_do_not_smuggle_a_price(self, text: str) -> None:
        """Obfuscation family: an invisible char must not change what a price parses to."""
        with pytest.raises(MoneyError):
            parse_rupee_string(text)

    @pytest.mark.parametrize("value", [None, [], {}, object(), b"1999"])
    def test_wrong_types_raise(self, value: object) -> None:
        with pytest.raises(MoneyError):
            parse_rupee_string(value)  # type: ignore[arg-type]


class TestCeiling:
    def test_at_the_ceiling_is_allowed(self) -> None:
        assert to_paise(MAX_PAISE) == MAX_PAISE

    def test_one_past_the_ceiling_is_refused(self) -> None:
        with pytest.raises(MoneyError, match="ceiling"):
            to_paise(MAX_PAISE + 1)

    def test_negative_ceiling_is_symmetric(self) -> None:
        assert to_paise(-MAX_PAISE, allow_negative=True) == -MAX_PAISE
        with pytest.raises(MoneyError, match="ceiling"):
            to_paise(-(MAX_PAISE + 1), allow_negative=True)

    def test_sum_cannot_climb_past_the_ceiling_incrementally(self) -> None:
        """A loop of individually-legal additions must not overflow the envelope."""
        half = MAX_PAISE // 2
        with pytest.raises(MoneyError, match="ceiling"):
            add(half, half, half)

    def test_python_bigint_does_not_rescue_an_absurd_amount(self) -> None:
        with pytest.raises(MoneyError):
            to_paise(10**30)


class TestQuantityAbuse:
    """HAAT family: 'quantity inflation'."""

    @pytest.mark.parametrize("qty", [-1, -100, -(10**9)])
    def test_negative_qty_refused(self, qty: int) -> None:
        with pytest.raises(MoneyError, match="negative"):
            mul_qty(199900, qty)

    @pytest.mark.parametrize("qty", [1_000_001, 10**9, 10**18])
    def test_implausible_qty_refused(self, qty: int) -> None:
        with pytest.raises(MoneyError, match="implausible"):
            mul_qty(199900, qty)

    def test_qty_boundary_is_exact(self) -> None:
        assert mul_qty(1, 1_000_000) == 1_000_000
        with pytest.raises(MoneyError):
            mul_qty(1, 1_000_001)

    @pytest.mark.parametrize("qty", [True, False, 1.0, "3", None])
    def test_non_int_qty_refused(self, qty: object) -> None:
        with pytest.raises(MoneyError):
            mul_qty(199900, qty)  # type: ignore[arg-type]

    def test_legal_qty_can_still_breach_the_ceiling(self) -> None:
        """Both factors legal, product absurd -- the guard is on the result too."""
        with pytest.raises(MoneyError, match="ceiling"):
            mul_qty(MAX_PAISE // 2, 100)


class TestDecimalEdges:
    @pytest.mark.parametrize(
        "value",
        [Decimal("1999.001"), Decimal("0.001"), Decimal("1E-10")],
    )
    def test_sub_paise_precision_refused(self, value: Decimal) -> None:
        with pytest.raises(MoneyError, match="sub-paise"):
            to_paise(value)

    @pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
    def test_non_finite_decimals_refused(self, value: Decimal) -> None:
        with pytest.raises(MoneyError):
            to_paise(value)

    def test_exponent_notation_that_is_exact_is_accepted(self) -> None:
        assert to_paise(Decimal("1E+3")) == 100000  # ₹1000 exactly


class TestAddTypeDiscipline:
    @pytest.mark.parametrize("value", [1.0, "100", None, True, Decimal("1")])
    def test_add_takes_int_only(self, value: object) -> None:
        with pytest.raises(MoneyError, match="int paise"):
            add(100, value)  # type: ignore[arg-type]


class TestRegressions:
    """Bugs the adversarial suite found in the first implementation. Each one was real.

    Kept as named tests rather than folded into the tables above, because a regression
    here is a money-moving vulnerability and deserves to fail loudly and legibly.
    """

    @pytest.mark.parametrize(
        ("text", "script"),
        [
            ("١٩٩٩", "Arabic-Indic"),
            ("१९९९", "Devanagari"),
            ("１９９９", "fullwidth Latin"),
            ("۱۹۹۹", "Extended Arabic-Indic"),
            ("1٠9٠9٠9", "Arabic-Indic zero interleaved"),
        ],
    )
    def test_unicode_digits_are_not_rupees(self, text: str, script: str) -> None:
        r"""Regression: ``\d`` matches every Unicode decimal digit and ``int()`` parses them.

        The first implementation read ``"१९९९"`` as ₹1,999 — a homoglyph price the
        merchant never wrote. Fixed by an explicit character allowlist plus ``re.ASCII``.
        """
        with pytest.raises(MoneyError, match="disallowed character") as exc:
            parse_rupee_string(text)
        assert "disallowed" in str(exc.value), f"{script} digits leaked through"

    @pytest.mark.parametrize(
        ("text", "name"),
        [
            ("1999 ", "no-break space"),
            ("1999​", "zero-width space"),
            ("‮1999", "right-to-left override"),
            ("1999﻿", "byte-order mark"),
            ("19‌99", "zero-width non-joiner"),
        ],
    )
    def test_invisible_whitespace_is_not_whitespace(self, text: str, name: str) -> None:
        r"""Regression: ``\s`` matches U+00A0 and friends, so invisible padding parsed fine."""
        with pytest.raises(MoneyError, match="disallowed character") as exc:
            parse_rupee_string(text)
        assert "disallowed" in str(exc.value), f"{name} leaked through"

    @pytest.mark.parametrize("value", [Decimal("Infinity"), Decimal("-Infinity"), Decimal("NaN")])
    def test_non_finite_decimal_raises_money_error_not_overflow(self, value: Decimal) -> None:
        """Regression: ``int(Decimal('Infinity'))`` raised OverflowError past the contract.

        Callers catch MoneyError. An escaping OverflowError would crash a gate check
        rather than deny it — and a crashed check is an unenforced check.
        """
        with pytest.raises(MoneyError):
            to_paise(value)

    def test_error_message_names_the_offending_codepoint(self) -> None:
        """An operator reading the audit log must be able to see *what* was smuggled."""
        with pytest.raises(MoneyError, match=r"U\+200B"):
            parse_rupee_string("1999​")


class TestProperties:
    @given(st.integers(min_value=0, max_value=MAX_PAISE))
    def test_format_parse_round_trip_is_lossless(self, paise: int) -> None:
        assert parse_rupee_string(format_inr(paise, symbol=False)) == paise

    @given(st.integers(min_value=0, max_value=MAX_PAISE))
    def test_format_always_has_exactly_two_decimals(self, paise: int) -> None:
        _, _, frac = format_inr(paise).rpartition(".")
        assert len(frac) == 2 and frac.isdigit()

    @given(
        st.integers(min_value=0, max_value=MAX_PAISE // 4),
        st.integers(min_value=0, max_value=MAX_PAISE // 4),
    )
    def test_add_is_commutative(self, a: int, b: int) -> None:
        assert add(a, b) == add(b, a)

    @given(st.integers(min_value=0, max_value=10_000_000), st.integers(min_value=0, max_value=1000))
    def test_mul_qty_agrees_with_repeated_add(self, unit: int, qty: int) -> None:
        assume(unit * qty <= MAX_PAISE)
        assert mul_qty(unit, qty) == add(*([unit] * qty))

    @given(st.text(max_size=40))
    def test_parse_never_raises_anything_but_money_error(self, text: str) -> None:
        """No stray ValueError/IndexError/TypeError escapes to a caller."""
        with contextlib.suppress(MoneyError):
            parse_rupee_string(text)
