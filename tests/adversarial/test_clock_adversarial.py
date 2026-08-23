"""Adversarial tests for paynaka.clock.

Time is a regulatory boundary here: RBI forbids collection contact outside 08:00-19:00
IST and NPCI blacks out debits between 10:00-13:00. Two abuse shapes matter.

1. A malformed window in ``policy.yaml`` must fail loudly at load time. A window that
   silently parses to something wider is a regulatory breach with no error message.
2. A caller in a different timezone -- or feeding a UTC instant to an IST rule -- must
   not shift the boundary. 19:30 IST is outside the window no matter where the server is.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from paynaka.clock import IST, FrozenClock, TimeWindow, parse_window

pytestmark = pytest.mark.adversarial


class TestMalformedWindows:
    @pytest.mark.parametrize(
        "text",
        [
            "",
            "   ",
            "08:00",  # no end
            "08:00-",  # missing end
            "-19:00",  # missing start
            "08:00~19:00",  # wrong separator
            "08.00-19.00",  # wrong time separator
            "8-19",  # no minutes
            "08:0-19:00",  # single-digit minute
            "08:000-19:00",  # three-digit minute
            "08:00-19:00-20:00",  # three parts
            "abc-def",
            "08:00 to 19:00",
            "0800-1900",
            "08:00-19:00\n",
        ],
    )
    def test_garbage_window_refused(self, text: str) -> None:
        with pytest.raises(ValueError, match="malformed time window"):
            parse_window(text)

    @pytest.mark.parametrize(
        "text",
        [
            "24:00-19:00",  # hour out of range
            "08:00-24:00",
            "25:00-26:00",
            "08:60-19:00",  # minute out of range
            "08:00-19:99",
            "99:99-99:99",
        ],
    )
    def test_out_of_range_times_refused(self, text: str) -> None:
        with pytest.raises(ValueError, match="out of range"):
            parse_window(text)

    def test_zero_width_window_contains_nothing(self) -> None:
        """``08:00-08:00`` is ambiguous: all day, or no time at all?

        We choose 'nothing', because a policy author who wrote a zero-width window
        almost certainly made a mistake, and the fail-closed reading of a *permitted*
        window is the empty one.
        """
        window = parse_window("08:00-08:00")
        for hour in range(24):
            instant = FrozenClock.at_ist(f"2026-08-23 {hour:02d}:00").now()
            assert window.contains(instant) is False


class TestTimezoneCannotShiftTheBoundary:
    CONTACT = parse_window("08:00-19:00")

    def test_utc_instant_is_evaluated_in_ist(self) -> None:
        """06:00 UTC == 11:30 IST, which is inside the contact window."""
        instant = datetime(2026, 8, 23, 6, 0, tzinfo=UTC)
        assert self.CONTACT.contains(instant) is True

    def test_utc_midday_is_outside_the_ist_window(self) -> None:
        """14:00 UTC == 19:30 IST -- outside, even though it is midday in UTC."""
        instant = datetime(2026, 8, 23, 14, 0, tzinfo=UTC)
        assert self.CONTACT.contains(instant) is False

    @pytest.mark.parametrize("offset_hours", [-11, -8, -5, 0, 1, 5, 9, 12, 14])
    def test_same_instant_same_answer_from_every_timezone(self, offset_hours: int) -> None:
        """The answer must depend on the instant, never on the caller's tzinfo."""
        base = datetime(2026, 8, 23, 14, 0, tzinfo=UTC)  # 19:30 IST -- outside
        shifted = base.astimezone(timezone(timedelta(hours=offset_hours)))
        assert shifted == base
        assert self.CONTACT.contains(shifted) is self.CONTACT.contains(base)
        assert self.CONTACT.contains(shifted) is False

    def test_ist_has_no_dst_to_exploit(self) -> None:
        """India observes no DST, so the offset is a constant +5:30 year-round.

        Worth pinning: a rule that silently shifted by an hour twice a year would be a
        seasonal compliance breach that no functional test would catch.
        """
        for month in range(1, 13):
            instant = datetime(2026, month, 15, 12, 0, tzinfo=IST)
            assert instant.utcoffset() == timedelta(hours=5, minutes=30)


class TestFrozenClockDiscipline:
    def test_epoch_and_now_agree_exactly(self) -> None:
        clock = FrozenClock.at_ist("2026-08-23 11:30")
        assert clock.epoch() == int(clock.now().timestamp())

    def test_advance_by_zero_is_a_noop(self) -> None:
        clock = FrozenClock.at_ist("2026-08-23 11:30")
        before = clock.epoch()
        clock.advance()
        assert clock.epoch() == before

    def test_advance_accepts_negative_for_replay_scenarios(self) -> None:
        """Chaos tests need to move time backwards to simulate out-of-order delivery."""
        clock = FrozenClock.at_ist("2026-08-23 11:30")
        before = clock.epoch()
        clock.advance(hours=-1)
        assert clock.epoch() == before - 3600

    @pytest.mark.parametrize("text", ["2026-13-01 11:30", "2026-08-32 11:30", "not-a-date", ""])
    def test_malformed_ist_string_refused(self, text: str) -> None:
        with pytest.raises(ValueError):
            FrozenClock.at_ist(text)

    def test_window_is_immutable(self) -> None:
        """A frozen dataclass means no code path can widen a regulatory window in place."""
        window = parse_window("08:00-19:00")
        with pytest.raises((AttributeError, TypeError)):
            window.start = None  # type: ignore[misc]

    def test_window_is_hashable_so_it_can_be_cached_safely(self) -> None:
        a, b = parse_window("08:00-19:00"), parse_window("08:00-19:00")
        assert a == b
        assert hash(a) == hash(b)
        # value semantics, not identity: two equal windows must collapse to one key
        assert len({a, b}) == 1
        assert {a: "contact"}[b] == "contact"
        assert isinstance(a, TimeWindow)
