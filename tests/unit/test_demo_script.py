"""Tests for the one command a reviewer actually runs.

`make demo` is the first thing anybody sees, so the things worth asserting are not about
plumbing. They are about what it claims: that the order is the order of the argument, that
the caveats are not optional output, and that one act failing still tells the rest of the
story rather than aborting the demo in front of somebody.
"""

from __future__ import annotations

import pytest
from scripts.demo import ACTS, main


class TestTheStoryItTells:
    def test_the_attacks_that_need_no_model_come_first(self) -> None:
        """The reordering this script exists for. `make demo-attack` used to lead with the
        poisoned catalogue -- the one result the project's own evidence says does not
        reliably work."""
        assert ACTS[0].module == "haat.toctou"
        assert ACTS[1].module == "chaos.runner"
        assert ACTS[2].module == "buyer.cli"

    def test_every_act_says_why_it_is_there(self) -> None:
        """A demo that shows numbers without saying what they mean is a screensaver."""
        for act in ACTS:
            assert len(act.why) > 80, act.title
            assert act.title[0].isdigit(), act.title

    def test_no_act_needs_a_key_or_a_network(self) -> None:
        """The whole promise: a reviewer with a clone and one command sees the story."""
        offline = {"haat.toctou", "chaos.runner", "buyer.cli", "haat.latency"}
        assert {act.module for act in ACTS} <= offline


class TestItRuns:
    def test_it_completes_and_reports_success(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["--skip-latency"]) == 0
        out = capsys.readouterr().out
        assert "acts in" in out

    def test_skipping_latency_drops_exactly_one_act(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["--skip-latency"])
        out = capsys.readouterr().out
        assert f"{len(ACTS) - 1}/{len(ACTS) - 1} acts" in out

    @pytest.mark.parametrize(
        "phrase",
        [
            "prompt injection is not solved",
            "stops a persuaded agent moving money outside its mandate",
            "64.4%",
            "no LLM SDK",
        ],
    )
    def test_the_caveats_are_not_optional_output(
        self, capsys: pytest.CaptureFixture[str], phrase: str
    ) -> None:
        """The claims this project refuses to make are printed by the demo itself, not left
        to a document nobody opens. A demo that only shows wins is a sales pitch.

        Whitespace is normalised first: the framing is hand-wrapped to the terminal, so a
        sentence is several lines and an exact-substring check would only be testing where
        the line breaks happen to fall.
        """
        main(["--skip-latency"])
        flowed = " ".join(capsys.readouterr().out.split())
        assert phrase in flowed


class TestWrapping:
    def test_no_line_exceeds_the_terminal_width(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Reflowed by hand rather than by textwrap so the framing survives a narrow window
        without a dependency. The failure mode is a wall of text in a screen recording."""
        main(["--skip-latency"])
        from paynaka.tty import strip_colour

        framing = [
            line
            for line in capsys.readouterr().out.splitlines()
            if line.startswith(("The attack", "A duplicate", "A poisoned", "Two attacks"))
        ]
        assert all(len(strip_colour(line)) <= 80 for line in framing)
