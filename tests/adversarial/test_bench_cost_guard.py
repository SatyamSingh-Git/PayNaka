"""The sweep must not spend money nobody agreed to.

`bench` described itself as *"visible corpus, four defences -> RESULTS.md"* and then made
eighteen hundred calls to a paid model. A reader started it, watched the counter reach
20/1800, and asked -- from inside the running sweep, after the money had begun moving --
whether this was going to be expensive. Nothing in the command, the help text or the README
had said so.

That is this project's own rule broken on its own terms: ambiguity in a money path resolves
to DENY. So the tests below are about the two directions that cost something. Spending
without an answer is the obvious one. Refusing a sweep somebody did agree to is the other,
and a guard that fails both ways is a guard that gets deleted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from haat.runner import RunConfig, confirm_spend, main

pytestmark = pytest.mark.adversarial


def config(tmp_path: Path, **kwargs: object) -> RunConfig:
    kwargs.setdefault("model", "deepseek/deepseek-v4-flash")
    return RunConfig(out_dir=tmp_path, **kwargs)  # type: ignore[arg-type]


class TestNothingIsSpentWithoutAnAnswer:
    def test_a_bare_no_stops_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr("builtins.input", lambda *_: "no")
        assert confirm_spend(config(tmp_path), assume_yes=False, interactive=True) is False
        assert "nothing spent" in capsys.readouterr().out

    @pytest.mark.parametrize("answer", ["", "y", "Y", "ok", "sure", "yes please", " ", "1"])
    def test_only_the_whole_word_counts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: str
    ) -> None:
        """`y` is what a person types when they have not read the number above it."""
        monkeypatch.setattr("builtins.input", lambda *_: answer)
        assert confirm_spend(config(tmp_path), assume_yes=False, interactive=True) is False

    @pytest.mark.parametrize("answer", ["yes", "YES", " Yes "])
    def test_yes_in_any_casing_starts_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: str
    ) -> None:
        monkeypatch.setattr("builtins.input", lambda *_: answer)
        assert confirm_spend(config(tmp_path), assume_yes=False, interactive=True) is True

    @pytest.mark.parametrize("interruption", [EOFError, KeyboardInterrupt])
    def test_an_interrupted_prompt_spends_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interruption: type[BaseException]
    ) -> None:
        """Ctrl+C and a closed stdin are both answers, and both mean no."""

        def interrupt(*_: object) -> str:
            raise interruption()

        monkeypatch.setattr("builtins.input", interrupt)
        assert confirm_spend(config(tmp_path), assume_yes=False, interactive=True) is False

    def test_no_terminal_and_no_flag_refuses(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Fails closed in the direction that matters. A sweep launched by a script nobody
        is watching is exactly where an unintended thousand calls goes unnoticed."""
        assert confirm_spend(config(tmp_path), assume_yes=False, interactive=False) is False
        assert "REFUSED" in capsys.readouterr().err

    def test_the_prompt_never_reaches_a_model(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole point: the question is asked before the first call, not the twentieth.
        A brain built here would mean money had already moved."""
        monkeypatch.setattr(
            "haat.runner.build_brain",
            lambda *_, **__: pytest.fail("a model was reached before the reader answered"),
        )
        monkeypatch.setattr("builtins.input", lambda *_: "no")
        assert confirm_spend(config(tmp_path), assume_yes=False, interactive=True) is False


class TestItAsksOnlyWhenThereIsSomethingToPayFor:
    def test_smoke_never_asks(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """`--smoke` runs a scripted brain. Asking about the cost of nothing trains a
        reader to type yes without reading."""
        assert confirm_spend(config(tmp_path, smoke=True), assume_yes=False, interactive=False)
        assert capsys.readouterr().out == ""

    def test_a_finished_sweep_asks_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Resume, with everything already done. There are no calls left to price."""
        monkeypatch.setattr("haat.runner._pending_runs", lambda _: 0)
        assert confirm_spend(config(tmp_path), assume_yes=False, interactive=False)
        assert capsys.readouterr().out == ""

    def test_it_prices_what_is_left_not_what_was_planned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Quoting the full price of a sweep that is nine tenths finished would frighten a
        reader off the cheap remainder of it."""
        monkeypatch.setattr("haat.runner._pending_runs", lambda _: 10)
        monkeypatch.setattr("builtins.input", lambda *_: "no")
        confirm_spend(config(tmp_path), assume_yes=False, interactive=True)
        printed = capsys.readouterr().out
        assert "model calls 10" in printed


class TestWhatItSaysBeforeItSpends:
    def test_it_names_the_model_the_count_and_the_price(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr("haat.runner._pending_runs", lambda _: 1800)
        monkeypatch.setattr("builtins.input", lambda *_: "no")
        confirm_spend(config(tmp_path), assume_yes=False, interactive=True)
        printed = capsys.readouterr().out
        assert "deepseek/deepseek-v4-flash" in printed
        assert "1,800" in printed
        assert "$" in printed
        assert "Ctrl+C is safe" in printed, "a reader who wants out needs to know that here"

    def test_an_unpriceable_model_says_unknown_rather_than_guessing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A number invented for an unmeasured model is worse than no number, because it
        would be quoted to somebody deciding whether to spend."""
        monkeypatch.setattr("haat.runner._pending_runs", lambda _: 1800)
        monkeypatch.setattr("builtins.input", lambda *_: "no")
        confirm_spend(
            config(tmp_path, model="some/unmeasured-model"),
            assume_yes=False,
            interactive=True,
        )
        printed = capsys.readouterr().out
        assert "unknown for this model" in printed
        assert "$" not in printed.split("estimate")[1].split("\n")[0]


class TestTheFlagAndTheCommandLine:
    def test_yes_skips_the_prompt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """For CI and for a sweep already priced. It still prints the cost -- an unattended
        log should record what the run was expected to spend."""
        monkeypatch.setattr("haat.runner._pending_runs", lambda _: 1800)
        monkeypatch.setattr(
            "builtins.input", lambda *_: pytest.fail("--yes must not stop for an answer")
        )
        assert confirm_spend(config(tmp_path), assume_yes=True, interactive=False) is True
        assert "$" in capsys.readouterr().out

    def test_the_cli_refuses_a_sweep_it_cannot_ask_about(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End to end through `main`, with a key present so the refusal is the cost guard
        and not the missing-key guard. pytest gives no terminal, which is the unattended
        case."""
        monkeypatch.setattr("haat.runner.require_model_key", lambda: "sk-test")
        monkeypatch.setattr(
            "haat.runner.run_corpus",
            lambda *_: pytest.fail("the sweep ran without anybody agreeing to pay for it"),
        )
        assert main(["--corpus", "visible", "--limit", "1", "--out", str(tmp_path)]) == 1
