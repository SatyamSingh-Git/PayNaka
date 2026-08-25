"""The task runner, attacked on the axis that hid a real bug for a month: its output.

`make.py` echoes each command and then hands the terminal to a child process. Two writers,
one stream, and Python buffers its half differently depending on whether that stream is a
terminal or a pipe. On a terminal it is line-buffered and everything appeared in order; in
a redirected log it was block-buffered, so every echoed command came out *after* the output
it was introducing and a failure was printed underneath the wrong task.

Nobody saw it, because a person reading a terminal sees the correct order and a person
reading a CI log has no reason to suspect the runner rather than the tool. The tests here
drive the real `make.py` as a subprocess with its stdout on a pipe -- the failing
configuration -- because the property under test belongs to the process, not the functions.

The rest is the heartbeat, which is a thread, and a thread that outlives its command prints
over the next one's output or over an error message.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from functools import partial
from pathlib import Path

import pytest
from make import MAKEFILE, SLOW_SECONDS, format_seconds, heartbeat, parse, run

pytestmark = pytest.mark.adversarial

MAKE_PY = MAKEFILE.with_name("make.py")


def drive(tmp_path: Path, text: str, task: str) -> subprocess.CompletedProcess[str]:
    """Run make.py over a Makefile written for the test, stdout on a pipe."""
    makefile = tmp_path / "Makefile"
    makefile.write_text(text, encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(MAKE_PY), "--file", str(makefile), task],
        capture_output=True,
        text=True,
        check=False,
    )


class TestOutputArrivesInTheOrderItHappened:
    def test_each_command_is_echoed_before_its_own_output(self, tmp_path: Path) -> None:
        """The regression. Unflushed, all four `$` lines arrived together at the end, after
        both children had written -- so the log said the second command produced the first
        command's output."""
        result = drive(tmp_path, "two: ## x\n\techo alpha\n\techo beta\n", "two")
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]

        # Compared as whole lines, not substrings: "alpha" also occurs inside "$ echo alpha",
        # and a substring search would pass against the exact bug this is here to catch.
        assert lines.index("$ echo alpha") < lines.index("alpha"), lines
        assert lines.index("alpha") < lines.index("$ echo beta"), lines
        assert lines.index("$ echo beta") < lines.index("beta"), lines

    def test_a_failure_is_reported_under_the_command_that_failed(self, tmp_path: Path) -> None:
        """The consequence that matters. Two commands, the first fine and the second broken:
        a reader must be able to tell which one broke by looking at what precedes the error.
        """
        result = drive(
            tmp_path,
            "boom: ## x\n\techo fine\n\tthis-command-does-not-exist-anywhere\n",
            "boom",
        )
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        assert result.returncode != 0
        assert lines[-1].startswith("$ this-command-does-not-exist"), lines

    def test_a_quiet_command_still_shows_its_output_in_place(self, tmp_path: Path) -> None:
        """`@` suppresses the echo, not the output, and the output must not drift."""
        result = drive(tmp_path, "hush: ## x\n\t@echo one\n\techo two\n", "hush")
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        assert "$ echo one" not in lines
        assert lines.index("one") < lines.index("$ echo two"), lines


class TestTheHeartbeatCannotOutliveItsCommand:
    def test_it_stops_even_when_the_command_raises(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The interesting path: an exception must not leave a thread printing ticks over
        the traceback."""
        with pytest.raises(RuntimeError), heartbeat(enabled=True, interval=0.02):
            time.sleep(0.05)
            raise RuntimeError("the command exploded")
        capsys.readouterr()
        time.sleep(0.12)
        assert capsys.readouterr().err == "", "a tick arrived after the command had failed"

    def test_it_leaves_no_thread_behind(self) -> None:
        before = threading.active_count()
        for _ in range(5):
            with heartbeat(enabled=True, interval=0.01):
                time.sleep(0.03)
        time.sleep(0.1)
        assert threading.active_count() <= before, "heartbeat threads accumulated"

    def test_the_thread_is_a_daemon(self) -> None:
        """A non-daemon tick thread would hold the interpreter open after the last task,
        which on a CI runner looks exactly like the hang this was written to explain."""
        seen: list[threading.Thread] = []
        with heartbeat(enabled=True, interval=5.0):
            seen = [t for t in threading.enumerate() if t.name == "make.py-heartbeat"]
        assert seen and all(t.daemon for t in seen)

    def test_an_interval_longer_than_the_command_prints_nothing(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with heartbeat(enabled=True, interval=30.0):
            pass
        assert capsys.readouterr().err == ""


class TestTheTimingLine:
    def test_it_appears_only_past_the_threshold(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Driven by a clock the test controls, because the alternative is a test that
        sleeps for five seconds to prove a five-second threshold."""
        import make

        makefile = tmp_path / "Makefile"
        makefile.write_text("t: ## x\n\techo hi\n", encoding="utf-8")
        tasks, variables = parse(makefile)

        for elapsed, should_print in ((SLOW_SECONDS - 0.01, False), (SLOW_SECONDS, True)):
            # `partial(next, ...)` rather than a lambda: a lambda closing over a loop
            # variable is the bug ruff's B023 exists to catch, and it would read the
            # last iteration's clock in both passes.
            ticks = iter([0.0, elapsed])
            monkeypatch.setattr(make.time, "monotonic", partial(next, ticks))
            assert run("t", tasks, variables, seen=set()) == 0
            printed = capsys.readouterr().out
            assert (format_seconds(elapsed) in printed) is should_print, (elapsed, printed)

    @pytest.mark.parametrize("seconds", [-1.0, 0.0, 1e6])
    def test_it_never_raises_on_an_odd_duration(self, seconds: float) -> None:
        """A clock that steps backwards across a suspend must not take the runner down."""
        assert isinstance(format_seconds(seconds), str)
