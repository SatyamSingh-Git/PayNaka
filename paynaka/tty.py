"""Printing to a terminal that may not be able to print.

Every number in this project is rupees, every rupee renders through ``format_inr``, and
``format_inr`` emits ``₹``. A Windows console defaults to cp1252, which has no code point
for it, so the first line of every demo raises ``UnicodeEncodeError`` and the whole
command dies -- not degraded, dead, on the machine most likely to be running the clone.

Two things happen here, in order:

**Ask the stream to speak UTF-8.** Python 3.7+ exposes ``reconfigure`` on the standard
streams, and a Windows terminal has handled UTF-8 for years. This is almost always enough.

**If it still cannot, transliterate rather than crash.** ``₹`` becomes ``Rs ``, and any
other unencodable character is replaced. A demo that prints ``Rs 1,999.00`` has lost a
glyph. A demo that raises has lost the reviewer.

Colour is stripped when the destination is not a terminal, so piping to a file or into CI
logs produces something a person can read rather than a field of escape codes.
"""

from __future__ import annotations

import contextlib
import io
import sys
from typing import Final

__all__ = ["BOLD", "DIM", "GREEN", "OFF", "RED", "YELLOW", "ascii_safe", "say", "strip_colour"]

DIM: Final = "\033[2m"
BOLD: Final = "\033[1m"
RED: Final = "\033[31m"
GREEN: Final = "\033[32m"
YELLOW: Final = "\033[33m"
OFF: Final = "\033[0m"

_CODES: Final[tuple[str, ...]] = (DIM, BOLD, RED, GREEN, YELLOW, OFF)

#: Characters this project actually emits that a legacy codepage may not carry, mapped to
#: something a reader still understands. Kept explicit: a silent "?" is worse than "Rs".
_FALLBACKS: Final[dict[str, str]] = {
    "₹": "Rs ",  # rupee sign
    "→": "->",  # rightwards arrow
    "—": "--",  # em dash
    "✓": "ok",  # check mark
    "×": "x",  # multiplication sign, written escaped so the table can hold it
}

_prepared = False


def _prepare() -> None:
    """Try once to make stdout and stderr speak UTF-8. Harmless if they already do."""
    global _prepared
    if _prepared:
        return
    _prepared = True
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        with contextlib.suppress(ValueError, OSError):  # a stream that refuses is fine
            reconfigure(encoding="utf-8")


def _encodable(text: str, stream: object) -> bool:
    encoding = getattr(stream, "encoding", None)
    if not encoding:
        return True
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def ascii_safe(text: str) -> str:
    """Replace characters this terminal cannot render with ones it can."""
    for char, replacement in _FALLBACKS.items():
        text = text.replace(char, replacement)
    return text.encode("ascii", "replace").decode("ascii")


def strip_colour(text: str) -> str:
    for code in _CODES:
        text = text.replace(code, "")
    return text


def say(text: str = "", *, stream: io.TextIOBase | None = None) -> None:
    """Print ``text``, dropping colour off a pipe and transliterating if we must."""
    _prepare()
    out = stream or sys.stdout
    if not out.isatty():
        text = strip_colour(text)
    if not _encodable(text, out):
        text = ascii_safe(text)
    print(text, file=out)
