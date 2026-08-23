"""Money, and only money.

Every rupee in PayNaka is an ``int`` count of paise. There are no floats in a money path,
ever -- ``0.1 + 0.2 != 0.3`` is a rounding error in a spreadsheet and a chargeback in a
payment system.

This module is the single place allowed to convert between paise and human strings.
Everything else passes ``int``.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Final, NewType

__all__ = [
    "MAX_PAISE",
    "MoneyError",
    "Paise",
    "add",
    "format_inr",
    "mul_qty",
    "parse_rupee_string",
    "to_paise",
]

Paise = NewType("Paise", int)

#: Hard ceiling for any single amount PayNaka will reason about: ₹100 crore.
#: Not a business rule -- a sanity rail. An agent asking to move more than this has been
#: manipulated, has overflowed something, or has found a bug. All three mean "stop".
MAX_PAISE: Final[int] = 100_00_00_000_00

#: Every character a trusted rupee string may contain. Deliberately tiny.
#:
#: This allowlist -- not the grammar below -- is what defeats the obfuscation family.
#: Python's ``\d`` matches *any* Unicode decimal digit, so ``"१९९९"`` (Devanagari) and
#: ``"١٩٩٩"`` (Arabic-Indic) both match ``\d+`` *and* parse cleanly through ``int()``.
#: Likewise ``\s`` matches U+00A0, U+200B and friends. A catalog field is attacker-
#: controlled, so a price containing any character outside this set is refused rather
#: than normalised: normalising is guessing, and guessing here moves money.
_ALLOWED_CHARS: Final[frozenset[str]] = frozenset("0123456789,. \tRrSsIiNn₹")

_RUPEE_STR: Final[re.Pattern[str]] = re.compile(
    r"""
    \A[ \t]*
    (?:INR|Rs\.?|₹)?          # optional currency marker
    [ \t]*
    (?P<amount>
        [0-9]{1,3}(?:,[0-9]{2})*(?:,[0-9]{3})   # 12,34,567  (Indian grouping)
      | [0-9]{1,3}(?:,[0-9]{3})*                # 1,234,567  (western grouping)
      | [0-9]+                                  # 1234567
    )
    (?:\.(?P<frac>[0-9]{1,2}))?                 # at most two decimal places
    [ \t]*\Z
    """,
    re.VERBOSE | re.IGNORECASE | re.ASCII,
)


class MoneyError(ValueError):
    """Raised when a value cannot be trusted as an amount of money."""


def to_paise(value: int | str | Decimal, *, allow_negative: bool = False) -> Paise:
    """Coerce ``value`` into paise, refusing anything ambiguous.

    Accepts ``int`` (already paise), ``str``/``Decimal`` (rupees). Rejects ``float``
    outright -- if a float reached a money path, the bug is upstream and silently
    rounding it here would hide the bug rather than fix it.
    """
    if isinstance(value, bool):  # bool is an int subclass; almost certainly a bug
        raise MoneyError("bool is not an amount")

    if isinstance(value, float):
        raise MoneyError(
            "float is not accepted in a money path; pass int paise or a decimal string"
        )

    if isinstance(value, int):
        paise = value
    elif isinstance(value, Decimal):
        paise = _decimal_to_paise(value)
    elif isinstance(value, str):
        paise = int(parse_rupee_string(value))
    else:  # pragma: no cover - defensive
        raise MoneyError(f"unsupported money type: {type(value).__name__}")

    return _guard(paise, allow_negative=allow_negative)


def parse_rupee_string(text: str) -> Paise:
    """Parse a human rupee string (``"₹1,999.50"``, ``"Rs 2000"``) into paise.

    Deliberately strict. A catalog that renders a price PayNaka cannot parse
    unambiguously is a catalog PayNaka refuses to transact against -- which is exactly
    the behaviour we want against the currency-confusion attack family.
    """
    if not isinstance(text, str):
        raise MoneyError(f"expected str, got {type(text).__name__}")

    if not _ALLOWED_CHARS.issuperset(text):
        stray = sorted({c for c in text if c not in _ALLOWED_CHARS})
        codes = " ".join(f"U+{ord(c):04X}" for c in stray[:4])
        raise MoneyError(f"disallowed character(s) in amount {text!r}: {codes}")

    match = _RUPEE_STR.match(text)
    if match is None:
        raise MoneyError(f"unparseable rupee amount: {text!r}")

    whole = match.group("amount").replace(",", "")
    frac = (match.group("frac") or "").ljust(2, "0")
    try:
        return _guard(int(whole) * 100 + int(frac))
    except ValueError as exc:  # pragma: no cover - regex already constrains this
        raise MoneyError(f"unparseable rupee amount: {text!r}") from exc


def format_inr(paise: int, *, symbol: bool = True) -> str:
    """Render paise as an Indian-grouped rupee string. Presentation only.

    ``format_inr(5200000) == "₹52,00,000.00"`` -- lakh/crore grouping, not thousands.
    """
    paise = int(paise)
    sign = "-" if paise < 0 else ""
    whole, frac = divmod(abs(paise), 100)

    digits = str(whole)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups: list[str] = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join([*groups, tail])

    prefix = "₹" if symbol else ""
    return f"{sign}{prefix}{digits}.{frac:02d}"


def add(*amounts: int) -> Paise:
    """Sum paise with the ceiling enforced, so overflow cannot creep in through a loop."""
    total = 0
    for amount in amounts:
        if isinstance(amount, bool) or not isinstance(amount, int):
            raise MoneyError("add() takes int paise only")
        total += amount
        _guard(total)
    return _guard(total)


def mul_qty(unit_paise: int, qty: int) -> Paise:
    """Multiply a unit price by a quantity, guarding both factors.

    Quantity inflation is an attack family; a negative or absurd qty is refused here
    rather than deep inside order construction.
    """
    if isinstance(qty, bool) or not isinstance(qty, int):
        raise MoneyError("qty must be int")
    if qty < 0:
        raise MoneyError("qty must not be negative")
    if qty > 1_000_000:
        raise MoneyError(f"implausible qty: {qty}")
    return _guard(_guard(unit_paise) * qty)


def _decimal_to_paise(value: Decimal) -> int:
    # NaN and +/-Infinity survive scaleb() and to_integral_value() intact, and NaN even
    # passes an equality check against itself by failing it. Reject non-finite up front
    # rather than letting int() raise OverflowError past the MoneyError contract.
    if not value.is_finite():
        raise MoneyError(f"non-finite amount: {value}")
    try:
        scaled = value.scaleb(2)
    except (InvalidOperation, OverflowError) as exc:
        raise MoneyError(f"unrepresentable decimal amount: {value}") from exc
    if scaled != scaled.to_integral_value():
        raise MoneyError(f"sub-paise precision is not representable: {value}")
    try:
        return int(scaled)
    except (OverflowError, ValueError) as exc:  # pragma: no cover - belt and braces
        raise MoneyError(f"unrepresentable decimal amount: {value}") from exc


def _guard(paise: int, *, allow_negative: bool = False) -> Paise:
    if not allow_negative and paise < 0:
        raise MoneyError(f"negative amount not allowed here: {paise}")
    if abs(paise) > MAX_PAISE:
        raise MoneyError(f"amount exceeds sanity ceiling: {paise}")
    return Paise(paise)
