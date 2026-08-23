"""Printing to a terminal that cannot print, and the rule that stops it recurring.

Every demonstration in this project prints rupees, and a Windows console defaults to
cp1252, which has no code point for ``₹``. That is not a cosmetic problem: the first line
of output raises ``UnicodeEncodeError`` and the command dies, on the platform most likely
to be running a fresh clone.

It happened twice. The second time was worse: ``paynaka/tty.py`` already existed to fix
it, and ``buyer/cli.py`` -- the file behind ``make demo-attack``, the command the README
opens with -- still had its own copy of the colour constants and its own ``say``. So the
headline demo crashed on its own second line while every other command was fine.

Hence :class:`TestNobodyRollsTheirOwn`, which is the test that actually prevents this
rather than the ones that describe it.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from paynaka.money import format_inr
from paynaka.tty import BOLD, DIM, GREEN, OFF, RED, YELLOW, ascii_safe, say, strip_colour


class _Cp1252(io.TextIOBase):
    """A stream that behaves like a legacy Windows console: it cannot encode a rupee."""

    encoding = "cp1252"

    def __init__(self) -> None:
        self.written: list[str] = []

    def isatty(self) -> bool:
        return False

    def write(self, text: str) -> int:
        text.encode("cp1252")  # raises exactly where a real console would
        self.written.append(text)
        return len(text)


class _Utf8(_Cp1252):
    encoding = "utf-8"

    def write(self, text: str) -> int:
        self.written.append(text)
        return len(text)


class TestItSurvivesALegacyConsole:
    def test_a_rupee_amount_prints_as_rs_rather_than_raising(self) -> None:
        stream = _Cp1252()
        say(f"money moved {format_inr(199_900)}", stream=stream)  # type: ignore[arg-type]
        assert "Rs 1,999.00" in "".join(stream.written)

    def test_a_utf8_stream_keeps_the_rupee_sign(self) -> None:
        stream = _Utf8()
        say(f"money moved {format_inr(199_900)}", stream=stream)  # type: ignore[arg-type]
        assert "₹1,999.00" in "".join(stream.written)

    @pytest.mark.parametrize("char", ["₹", "→", "—", "✓", "×"])
    def test_every_character_in_the_fallback_table_survives(self, char: str) -> None:
        stream = _Cp1252()
        say(f"before {char} after", stream=stream)  # type: ignore[arg-type]
        assert stream.written

    def test_an_unmapped_character_degrades_rather_than_raising(self) -> None:
        """A glyph nobody anticipated must still not take the command down."""
        stream = _Cp1252()
        say("देवनागरी", stream=stream)  # type: ignore[arg-type]
        assert stream.written

    def test_colour_is_stripped_off_a_pipe(self) -> None:
        stream = _Utf8()
        say(f"{RED}danger{OFF}", stream=stream)  # type: ignore[arg-type]
        assert "".join(stream.written).strip() == "danger"


class TestHelpers:
    def test_strip_colour_removes_every_code(self) -> None:
        painted = f"{DIM}a{BOLD}b{RED}c{GREEN}d{YELLOW}e{OFF}"
        assert strip_colour(painted) == "abcde"

    def test_ascii_safe_prefers_a_readable_substitute(self) -> None:
        assert ascii_safe("₹1,999") == "Rs 1,999"
        assert ascii_safe("a → b") == "a -> b"

    def test_ascii_safe_never_raises_on_anything(self) -> None:
        assert isinstance(ascii_safe("नाका ₹ 🙏"), str)

    def test_plain_text_is_untouched(self) -> None:
        assert ascii_safe("plain ascii") == "plain ascii"
        assert strip_colour("plain ascii") == "plain ascii"


class TestNobodyRollsTheirOwn:
    """The structural rule. One place knows how to print; everywhere else asks it.

    A local copy of these constants is how the bug came back the second time, and a
    grep is the only thing that would have caught it before a user did.
    """

    #: Files allowed to contain a raw ANSI escape. Exactly one.
    OWNER = "paynaka/tty.py"

    def _sources(self) -> list[Path]:
        roots = ("paynaka", "buyer", "haat", "chaos", "merchant", "scripts")
        return [path for root in roots for path in Path(root).rglob("*.py")]

    def test_only_tty_contains_raw_ansi_escapes(self) -> None:
        offenders = [
            str(path).replace("\\", "/")
            for path in self._sources()
            if "\033[" in path.read_text(encoding="utf-8")
            and str(path).replace("\\", "/") != self.OWNER
        ]
        assert offenders == [], (
            f"{offenders} define their own colour codes. Import them from paynaka.tty "
            f"instead -- a local copy is how make demo-attack ended up unable to print a "
            f"rupee sign on Windows while everything else was fine."
        )

    def test_only_tty_defines_its_own_say(self) -> None:
        offenders = [
            str(path).replace("\\", "/")
            for path in self._sources()
            if "\ndef say(" in path.read_text(encoding="utf-8")
            and str(path).replace("\\", "/") != self.OWNER
        ]
        assert offenders == []

    def test_every_module_that_formats_money_can_print_it(self) -> None:
        """If a file calls format_inr, it emits a rupee sign and needs the safe printer."""
        offenders: list[str] = []
        for path in self._sources():
            source = path.read_text(encoding="utf-8")
            slug = str(path).replace("\\", "/")
            if "format_inr(" not in source or slug == self.OWNER:
                continue
            prints_it = "print(" in source or "say(" in source
            if prints_it and "paynaka.tty" not in source:
                offenders.append(slug)
        assert offenders == [], (
            f"{offenders} print rupee amounts without paynaka.tty, so they will raise "
            f"UnicodeEncodeError on a cp1252 console."
        )
