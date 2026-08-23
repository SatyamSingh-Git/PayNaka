"""Forward tests for paynaka.clock."""

from __future__ import annotations

from datetime import UTC, datetime, time

import pytest

from paynaka.clock import IST, FrozenClock, SystemClock, parse_window


class TestFrozenClock:
    def test_at_ist_reads_as_written(self) -> None:
        clock = FrozenClock.at_ist("2026-08-23 11:30")
        assert clock.now().astimezone(IST).strftime("%Y-%m-%d %H:%M") == "2026-08-23 11:30"

    def test_stores_utc_internally(self) -> None:
        clock = FrozenClock.at_ist("2026-08-23 11:30")
        assert clock.now().tzinfo is UTC
        # IST is UTC+5:30, so 11:30 IST is 06:00 UTC
        assert clock.now().strftime("%H:%M") == "06:00"

    def test_does_not_move_on_its_own(self) -> None:
        clock = FrozenClock.at_ist("2026-08-23 11:30")
        assert clock.epoch() == clock.epoch()

    def test_advance(self) -> None:
        clock = FrozenClock.at_ist("2026-08-23 11:30")
        before = clock.epoch()
        clock.advance(hours=2, minutes=30)
        assert clock.epoch() == before + 9000

    def test_naive_datetime_refused(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            FrozenClock(datetime(2026, 8, 23, 11, 30))  # noqa: DTZ001


class TestSystemClock:
    def test_now_is_utc_aware(self) -> None:
        assert SystemClock().now().tzinfo is UTC

    def test_epoch_agrees_with_now(self) -> None:
        clock = SystemClock()
        assert abs(clock.epoch() - int(clock.now().timestamp())) <= 1


class TestParseWindow:
    @pytest.mark.parametrize(
        ("text", "start", "end"),
        [
            ("08:00-19:00", time(8, 0), time(19, 0)),  # RBI contact window
            ("10:00-13:00", time(10, 0), time(13, 0)),  # NPCI debit blackout
            ("00:00-23:59", time(0, 0), time(23, 59)),
            ("21:30-06:00", time(21, 30), time(6, 0)),  # wraps midnight
            ("  08:00 - 19:00  ", time(8, 0), time(19, 0)),
            ("8:00-19:00", time(8, 0), time(19, 0)),
        ],
    )
    def test_parses(self, text: str, start: time, end: time) -> None:
        window = parse_window(text)
        assert window.start == start
        assert window.end == end

    def test_detects_midnight_wrap(self) -> None:
        assert parse_window("21:30-06:00").wraps_midnight
        assert not parse_window("08:00-19:00").wraps_midnight

    def test_str_is_readable_in_an_audit_log(self) -> None:
        assert str(parse_window("08:00-19:00")) == "08:00-19:00 IST"


class TestWindowContains:
    """RBI: no collection contact outside 08:00-19:00 IST. The boundary is the rule."""

    CONTACT = parse_window("08:00-19:00")

    @pytest.mark.parametrize(
        ("ist", "inside"),
        [
            ("2026-08-23 07:59", False),  # one minute early
            ("2026-08-23 08:00", True),  # start is inclusive
            ("2026-08-23 12:00", True),
            ("2026-08-23 18:59", True),
            ("2026-08-23 19:00", False),  # end is exclusive
            ("2026-08-23 19:01", False),
            ("2026-08-23 00:00", False),
            ("2026-08-23 23:59", False),
        ],
    )
    def test_boundaries_are_exact(self, ist: str, inside: bool) -> None:
        assert self.CONTACT.contains(FrozenClock.at_ist(ist).now()) is inside

    @pytest.mark.parametrize(
        ("ist", "inside"),
        [
            ("2026-08-23 21:29", False),
            ("2026-08-23 21:30", True),
            ("2026-08-23 23:59", True),
            ("2026-08-23 00:00", True),
            ("2026-08-23 05:59", True),
            ("2026-08-23 06:00", False),
            ("2026-08-23 12:00", False),
        ],
    )
    def test_midnight_wrap_boundaries(self, ist: str, inside: bool) -> None:
        window = parse_window("21:30-06:00")
        assert window.contains(FrozenClock.at_ist(ist).now()) is inside
