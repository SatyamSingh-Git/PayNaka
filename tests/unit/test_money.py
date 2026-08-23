"""Forward tests for paynaka.money -- does it do the right thing?"""

from __future__ import annotations

from decimal import Decimal

import pytest

from paynaka.money import (
    MAX_PAISE,
    MoneyError,
    add,
    format_inr,
    mul_qty,
    parse_rupee_string,
    to_paise,
)


class TestToPaise:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0, 0),
            (1, 1),
            (199900, 199900),
            (MAX_PAISE, MAX_PAISE),
        ],
    )
    def test_int_passes_through_as_paise(self, value: int, expected: int) -> None:
        assert to_paise(value) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (Decimal("0"), 0),
            (Decimal("1"), 100),
            (Decimal("1999.00"), 199900),
            (Decimal("1999.5"), 199950),
            (Decimal("0.01"), 1),
        ],
    )
    def test_decimal_is_rupees(self, value: Decimal, expected: int) -> None:
        assert to_paise(value) == expected

    def test_string_is_rupees(self) -> None:
        assert to_paise("1999.00") == 199900

    def test_negative_allowed_only_when_asked(self) -> None:
        assert to_paise(-500, allow_negative=True) == -500
        with pytest.raises(MoneyError, match="negative"):
            to_paise(-500)


class TestParseRupeeString:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            # plain
            ("1999", 199900),
            ("1999.00", 199900),
            ("1999.5", 199950),
            ("1999.50", 199950),
            ("0.01", 1),
            ("0", 0),
            # currency markers
            ("₹1999", 199900),
            ("Rs 1999", 199900),
            ("Rs. 1999", 199900),
            ("INR 1999", 199900),
            ("inr 1999", 199900),
            # Indian grouping (lakh/crore)
            ("1,999", 199900),
            ("12,34,567", 123456700),
            ("1,00,000", 10000000),
            # western grouping
            ("1,234,567", 123456700),
            # whitespace tolerance
            ("  ₹ 1,999.50  ", 199950),
        ],
    )
    def test_accepts_real_world_formats(self, text: str, expected: int) -> None:
        assert parse_rupee_string(text) == expected


class TestFormatInr:
    @pytest.mark.parametrize(
        ("paise", "expected"),
        [
            (0, "₹0.00"),
            (1, "₹0.01"),
            (100, "₹1.00"),
            (199900, "₹1,999.00"),
            (199950, "₹1,999.50"),
            # Indian grouping kicks in past a thousand
            (10000000, "₹1,00,000.00"),
            (5200000, "₹52,000.00"),
            (123456700, "₹12,34,567.00"),
            (-199900, "-₹1,999.00"),
        ],
    )
    def test_indian_grouping(self, paise: int, expected: str) -> None:
        assert format_inr(paise) == expected

    def test_symbol_can_be_suppressed(self) -> None:
        assert format_inr(199900, symbol=False) == "1,999.00"

    @pytest.mark.parametrize("paise", [0, 1, 99, 100, 199900, 10000000, 123456789])
    def test_round_trips_through_parse(self, paise: int) -> None:
        """Formatting then reparsing must be lossless -- the console depends on it."""
        assert parse_rupee_string(format_inr(paise, symbol=False)) == paise


class TestAdd:
    def test_sums(self) -> None:
        assert add(100, 200, 300) == 600

    def test_empty_is_zero(self) -> None:
        assert add() == 0

    def test_single(self) -> None:
        assert add(199900) == 199900


class TestMulQty:
    @pytest.mark.parametrize(
        ("unit", "qty", "expected"),
        [
            (199900, 0, 0),
            (199900, 1, 199900),
            (199900, 3, 599700),
            (1, 1_000_000, 1_000_000),
        ],
    )
    def test_multiplies(self, unit: int, qty: int, expected: int) -> None:
        assert mul_qty(unit, qty) == expected
