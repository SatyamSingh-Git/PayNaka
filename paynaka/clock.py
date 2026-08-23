"""An injectable clock.

PayNaka encodes real Indian payments regulation that is expressed in wall-clock terms:
RBI's 08:00-19:00 collection-contact window, NPCI's 10:00-13:00 debit blackout. Rules
like these are untestable if the code reaches for ``datetime.now()``, so it never does --
every check that cares about time takes a ``Clock``.

All reasoning happens in Asia/Kolkata, because that is the timezone the regulations are
written in. Storage is always UTC epoch seconds.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Final, Protocol
from zoneinfo import ZoneInfo

__all__ = ["IST", "Clock", "FrozenClock", "SystemClock", "TimeWindow", "parse_window"]

IST: Final[ZoneInfo] = ZoneInfo("Asia/Kolkata")

# Anchored with \A and \Z, never ^ and $. Python's `$` also matches *before* a trailing
# newline, so a policy value of "08:00-19:00\n" would silently parse -- and a regulatory
# window is not a place for silent tolerance. Digit classes are explicit [0-9] plus
# re.ASCII, so Devanagari and Arabic-Indic digits cannot smuggle in a different window.
_WINDOW: Final[re.Pattern[str]] = re.compile(
    r"\A[ \t]*(?P<sh>[0-9]{1,2}):(?P<sm>[0-9]{2})"
    r"[ \t]*-[ \t]*"
    r"(?P<eh>[0-9]{1,2}):(?P<em>[0-9]{2})[ \t]*\Z",
    re.ASCII,
)


class Clock(Protocol):
    """The only source of 'now' in PayNaka."""

    def now(self) -> datetime:
        """Current instant as a timezone-aware UTC datetime."""
        ...

    def epoch(self) -> int:
        """Current instant as UTC epoch seconds."""
        ...


class SystemClock:
    """Wall-clock time. The only implementation allowed in production."""

    __slots__ = ()

    def now(self) -> datetime:
        return datetime.now(UTC)

    def epoch(self) -> int:
        return int(datetime.now(UTC).timestamp())


@dataclass(slots=True)
class FrozenClock:
    """A clock that stands still until told otherwise. Tests only.

    ``FrozenClock.at_ist("2026-08-23 11:30")`` reads far better in a test than an epoch
    integer, and time-window bugs are found by reading tests, not by squinting at them.
    """

    instant: datetime

    def __post_init__(self) -> None:
        if self.instant.tzinfo is None:
            raise ValueError("FrozenClock requires a timezone-aware datetime")
        self.instant = self.instant.astimezone(UTC)

    @classmethod
    def at_ist(cls, text: str) -> FrozenClock:
        """Build from ``"YYYY-MM-DD HH:MM"`` interpreted in Asia/Kolkata."""
        naive = datetime.strptime(text, "%Y-%m-%d %H:%M")  # noqa: DTZ007 - tz applied next
        return cls(naive.replace(tzinfo=IST))

    def now(self) -> datetime:
        return self.instant

    def epoch(self) -> int:
        return int(self.instant.timestamp())

    def advance(self, *, seconds: int = 0, minutes: int = 0, hours: int = 0) -> None:
        self.instant += timedelta(seconds=seconds, minutes=minutes, hours=hours)


@dataclass(frozen=True, slots=True)
class TimeWindow:
    """A daily window in IST, inclusive of ``start`` and exclusive of ``end``.

    Windows that wrap midnight (``"21:30-06:00"``) are supported, because NPCI's
    permitted debit windows genuinely do wrap.
    """

    start: time
    end: time

    @property
    def wraps_midnight(self) -> bool:
        return self.start > self.end

    def contains(self, instant: datetime) -> bool:
        local = instant.astimezone(IST).time()
        if self.start == self.end:
            return False  # a zero-width window contains nothing
        if self.wraps_midnight:
            return local >= self.start or local < self.end
        return self.start <= local < self.end

    def __str__(self) -> str:
        return f"{self.start:%H:%M}-{self.end:%H:%M} IST"


def parse_window(text: str) -> TimeWindow:
    """Parse ``"08:00-19:00"`` into a :class:`TimeWindow`.

    Strict on purpose: a policy file with a malformed window must fail loudly at load
    time, not silently widen a regulatory boundary at runtime.
    """
    match = _WINDOW.match(text)
    if match is None:
        raise ValueError(f"malformed time window: {text!r} (expected 'HH:MM-HH:MM')")

    sh, sm, eh, em = (int(match.group(g)) for g in ("sh", "sm", "eh", "em"))
    for hour, minute in ((sh, sm), (eh, em)):
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"time out of range in window: {text!r}")

    return TimeWindow(start=time(sh, sm), end=time(eh, em))
