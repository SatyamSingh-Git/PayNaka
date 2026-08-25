"""The ways a benchmark run can lie because the *provider* failed, not the system.

A sweep against a rate-limited free tier produces HTTP 429s. What the harness does with
those decides whether the resulting table is evidence or decoration, and the failure is
silent in both directions that matter:

**Resume.** If an errored row counts as completed, one transient 429 permanently records
that case as ``attack_succeeded=False``. The case is never retried and the benchmark scores
a network failure as a successful defence.

**Scoring.** If an errored run stays in the attack denominator, attack success is diluted
in direct proportion to how flaky the provider was that afternoon. Both of these move the
number the *same* way -- they make the undefended baseline look safer than it is, which
shrinks the exact gap the benchmark exists to demonstrate. A benchmark whose errors flatter
its author is worse than no benchmark.

Found for real: a first attempt at the full sweep on OpenRouter's free tier came back with
6 of the first 14 rows 429ing, every one of them written as a clean defence for the ``none``
row.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from haat.report import summarise
from haat.runner import Throttle, _completed, _is_transient
from haat.schema import RunResult

pytestmark = pytest.mark.adversarial


def row(case_id: str, defence: str = "none", error: str | None = None) -> str:
    return json.dumps({"case_id": case_id, "defence": defence, "error": error})


class TestAnErroredRunIsNotAResult:
    def test_a_clean_row_counts_as_done(self, tmp_path: Path) -> None:
        """The control. Without this, "retry everything" would also pass every test
        below and the resume feature would be gone."""
        path = tmp_path / "visible.jsonl"
        path.write_text(row("case_a") + "\n", encoding="utf-8")
        assert _completed(path) == {("case_a", "none")}

    @pytest.mark.parametrize(
        "error",
        [
            "model call failed: Error code: 429 - rate limit exceeded",
            "ConnectionError: connection reset",
            "TimeoutError: read timed out",
            "x",
        ],
        ids=["rate-limit", "connection", "timeout", "terse"],
    )
    def test_an_errored_row_is_retried_rather_than_believed(
        self, tmp_path: Path, error: str
    ) -> None:
        path = tmp_path / "visible.jsonl"
        path.write_text(row("case_a", error=error) + "\n", encoding="utf-8")
        assert _completed(path) == set()

    def test_a_later_clean_run_supersedes_an_earlier_error(self, tmp_path: Path) -> None:
        """The whole point of retrying: the second attempt is the result."""
        path = tmp_path / "visible.jsonl"
        path.write_text(row("case_a", error="429") + "\n" + row("case_a") + "\n", encoding="utf-8")
        assert _completed(path) == {("case_a", "none")}

    def test_one_defence_erroring_does_not_retry_the_others(self, tmp_path: Path) -> None:
        path = tmp_path / "visible.jsonl"
        path.write_text(
            row("case_a", "none") + "\n" + row("case_a", "naka", error="429") + "\n",
            encoding="utf-8",
        )
        assert _completed(path) == {("case_a", "none")}

    def test_a_torn_final_line_is_still_survivable(self, tmp_path: Path) -> None:
        path = tmp_path / "visible.jsonl"
        path.write_text(row("case_a") + "\n" + '{"case_id": "case_b", "def', encoding="utf-8")
        assert _completed(path) == {("case_a", "none")}

    def test_an_empty_file_is_not_a_completed_sweep(self, tmp_path: Path) -> None:
        path = tmp_path / "visible.jsonl"
        path.write_text("", encoding="utf-8")
        assert _completed(path) == set()


def result(
    *, case_id: str, succeeded: bool, error: str | None = None, family: str = "line_item_append"
) -> RunResult:
    return RunResult(
        case_id=case_id,
        defence="none",
        family=family,
        money_moved=5_200_000 if succeeded else 0,
        authorised=199_900,
        attack_succeeded=succeeded,
        error=error,
    )


class TestAnErroredRunIsNotADefendedAttack:
    def test_it_is_excluded_from_the_denominator(self) -> None:
        """Two real attacks, one of which succeeded, and eight cases that never ran. The
        honest rate is 1 in 2. Counting the errors makes it 1 in 10."""
        results = [
            result(case_id="a", succeeded=True),
            result(case_id="b", succeeded=False),
            *[result(case_id=f"e{i}", succeeded=False, error="429") for i in range(8)],
        ]
        summary = summarise(results)["none"]
        assert summary.attacks == 2
        assert summary.attacks_succeeded == 1
        assert summary.attack_success_rate == pytest.approx(0.5)

    def test_the_errors_are_reported_rather_than_hidden(self) -> None:
        """Excluded from the score, never from the page. A sweep that half failed must say
        so, or the coverage is invisible and the number looks solid."""
        results = [
            result(case_id="a", succeeded=True),
            *[result(case_id=f"e{i}", succeeded=False, error="429") for i in range(8)],
        ]
        assert summarise(results)["none"].errors == 8

    def test_an_all_errors_run_reports_no_attacks_rather_than_perfect_defence(self) -> None:
        """The pathological case, and the one that would be quoted. Zero successes out of
        zero attacks must not render as a flawless defence."""
        summary = summarise(
            [result(case_id=f"e{i}", succeeded=False, error="429") for i in range(20)]
        )["none"]
        assert summary.attacks == 0
        assert summary.attacks_succeeded == 0
        assert summary.errors == 20

    def test_an_errored_row_does_not_enter_a_family_breakdown(self) -> None:
        results = [
            result(case_id="a", succeeded=True, family="currency_confusion"),
            result(case_id="e", succeeded=False, error="429", family="currency_confusion"),
        ]
        summary = summarise(results)["none"]
        assert summary.by_family["currency_confusion"] == [True]

    def test_an_errored_benign_row_is_not_a_false_positive(self) -> None:
        """The mirror. An errored benign case counted as "wrongly blocked" would invent a
        false-positive rate out of provider flakiness."""
        rows = [
            RunResult(
                case_id="b1",
                defence="none",
                family="benign",
                money_moved=0,
                authorised=199_900,
                attack_succeeded=True,  # reused field: "wrongly blocked" for benign rows
                error="429",
            )
        ]
        summary = summarise(rows)["none"]
        assert summary.benign == 0
        assert summary.benign_wrongly_blocked == 0


class TestTheThrottle:
    def test_disabled_by_default_costs_nothing(self) -> None:
        """A paid key should not be slowed down by a free tier's problem."""
        throttle = Throttle(0)
        started = time.monotonic()
        for _ in range(50):
            throttle.wait()
        assert time.monotonic() - started < 0.1

    @pytest.mark.parametrize("rpm", [-1, -100])
    def test_a_nonsensical_rate_disables_rather_than_blocking_forever(self, rpm: int) -> None:
        throttle = Throttle(rpm)
        started = time.monotonic()
        throttle.wait()
        throttle.wait()
        assert time.monotonic() - started < 0.1

    def test_it_spaces_calls_out(self) -> None:
        """600 a minute is 100ms apart. Three calls is two gaps."""
        throttle = Throttle(600)
        started = time.monotonic()
        for _ in range(3):
            throttle.wait()
        assert time.monotonic() - started >= 0.18

    def test_the_first_call_is_not_delayed(self) -> None:
        """A sweep should start immediately and pay the interval afterwards."""
        throttle = Throttle(60)
        started = time.monotonic()
        throttle.wait()
        assert time.monotonic() - started < 0.05

    def test_the_schedule_is_shared_rather_than_per_caller(self) -> None:
        """Workers are threads against one provider quota. A per-thread interval would let
        six workers issue six times the permitted rate, which is the bug this prevents."""
        throttle = Throttle(600)
        started = time.monotonic()
        with __import__("concurrent.futures", fromlist=["ThreadPoolExecutor"]).ThreadPoolExecutor(
            max_workers=6
        ) as pool:
            list(pool.map(lambda _: throttle.wait(), range(6)))
        assert time.monotonic() - started >= 0.45


class TestATransientFailureIsRetriedAndAPermanentOneIsNot:
    @pytest.mark.parametrize(
        "error",
        [
            "model call failed: Error code: 429 - Rate limit exceeded",
            "Rate limit exceeded: free-models-per-min.",
            "rate_limit_error",
            "TimeoutError: read timed out",
            "the request timed out",
            "ConnectionError: connection reset by peer",
            "Error code: 503 - temporarily unavailable",
            "Error code: 502 - bad gateway",
            "Provider overloaded, try again",
        ],
        ids=repr,
    )
    def test_a_transient_error_is_recognised(self, error: str) -> None:
        """Matched on substrings because the message arrives as a string from whichever
        SDK the brain used. A provider inventing a new phrasing should widen the list, not
        silently poison a benchmark."""
        assert _is_transient(error) is True

    @pytest.mark.parametrize(
        "error",
        [
            "BrainError: unrecognised model spec: 'nonsense'",
            "KeyError: 'ATTA-5KG'",
            "AssertionError",
            "invalid api key",
            "model not found",
            "",
        ],
        ids=repr,
    )
    def test_a_permanent_error_is_not_retried(self, error: str) -> None:
        """Retrying a bad model slug five times reaches the same answer slowly, and hides
        a configuration mistake behind a long wait."""
        assert _is_transient(error) is False

    def test_the_matching_is_case_insensitive(self) -> None:
        assert _is_transient("RATE LIMIT EXCEEDED") is True
        assert _is_transient("Connection Reset") is True


class TestBackoffMovesTheSharedSchedule:
    def test_it_sleeps_for_the_requested_time(self) -> None:
        throttle = Throttle(0)
        started = time.monotonic()
        throttle.backoff(0.2)
        assert time.monotonic() - started >= 0.18

    @pytest.mark.parametrize("seconds", [0, -1, -100])
    def test_a_nonpositive_backoff_returns_immediately(self, seconds: float) -> None:
        throttle = Throttle(0)
        started = time.monotonic()
        throttle.backoff(seconds)
        assert time.monotonic() - started < 0.05

    def test_a_concurrent_caller_is_held_back_during_a_backoff(self) -> None:
        """Backing off without moving the shared schedule lets the other workers keep
        issuing at full rate into the limit this one just hit, so the retry lands in the
        same exhausted window it was waiting out.

        Measured from a *second* thread, because that is who the push is for. By the time
        the backing-off caller wakes, the delay it scheduled has already elapsed for it.
        """
        throttle = Throttle(600)
        backing_off = threading.Thread(target=throttle.backoff, args=(0.4,))
        backing_off.start()
        time.sleep(0.05)  # let it take the lock and move the schedule

        started = time.monotonic()
        throttle.wait()
        held = time.monotonic() - started
        backing_off.join()

        assert held >= 0.2, f"a concurrent caller was only held {held:.3f}s"


def row_m(case_id: str, model: str, defence: str = "none", error: str | None = None) -> str:
    return json.dumps({"case_id": case_id, "defence": defence, "model": model, "error": error})


class TestOneModelsResultsAreNotAnothersEvidence:
    """Susceptibility is a property of a model. A row naming a different one is a row
    about a different question, and resuming across a model change used to credit it to
    whichever name was passed last.

    Found in a real sweep: five ``deepseek-v4-flash`` rows from an earlier test run sat in
    a ``nemotron`` output file and were treated as completed.
    """

    def test_a_matching_model_counts_as_done(self, tmp_path: Path) -> None:
        path = tmp_path / "visible.jsonl"
        path.write_text(row_m("case_a", "model-x") + "\n", encoding="utf-8")
        assert _completed(path, "model-x") == {("case_a", "none")}

    def test_a_different_model_is_re_run(self, tmp_path: Path) -> None:
        path = tmp_path / "visible.jsonl"
        path.write_text(row_m("case_a", "model-x") + "\n", encoding="utf-8")
        assert _completed(path, "model-y") == set()

    def test_a_row_with_no_model_is_re_run(self, tmp_path: Path) -> None:
        """An older file, or a row written before the field existed. It cannot be
        attributed, so it cannot be counted."""
        path = tmp_path / "visible.jsonl"
        path.write_text(json.dumps({"case_id": "a", "defence": "none"}) + "\n", encoding="utf-8")
        assert _completed(path, "model-x") == set()

    def test_a_mixed_file_yields_only_the_asked_for_model(self, tmp_path: Path) -> None:
        path = tmp_path / "visible.jsonl"
        path.write_text(
            row_m("a", "model-x")
            + "\n"
            + row_m("b", "model-y")
            + "\n"
            + row_m("c", "model-x")
            + "\n",
            encoding="utf-8",
        )
        assert _completed(path, "model-x") == {("a", "none"), ("c", "none")}

    def test_asking_for_no_model_keeps_the_old_permissive_behaviour(self, tmp_path: Path) -> None:
        """So a caller that genuinely wants every row -- a report over a multi-model file --
        is not forced to name one."""
        path = tmp_path / "visible.jsonl"
        path.write_text(
            row_m("a", "model-x") + "\n" + row_m("b", "model-y") + "\n", encoding="utf-8"
        )
        assert _completed(path) == {("a", "none"), ("b", "none")}

    def test_a_model_change_does_not_resurrect_an_error(self, tmp_path: Path) -> None:
        """Both guards at once: wrong model *and* errored."""
        path = tmp_path / "visible.jsonl"
        path.write_text(row_m("a", "model-x", error="429") + "\n", encoding="utf-8")
        assert _completed(path, "model-x") == set()
        assert _completed(path, "model-y") == set()


class TestAModelCallCannotHangForever:
    """The failure that looks exactly like a working sweep, and then is not one.

    Three paid sweeps were launched in parallel. They wrote six, nine and zero rows and then
    stopped dead -- no error, no exit, three live processes producing nothing for ten
    minutes. The cause was not the provider: the OpenAI SDK defaults to a **600 second**
    request timeout, and these calls run behind a thread pool, so one provider that accepts
    a request and goes quiet parks a worker for ten minutes. Six workers park the sweep.

    The retry logic could not help, because retries only run once a call *returns*. A hung
    call never returns, so backoff never fires and the transient/permanent split never gets
    asked. An unbounded timeout does not degrade a benchmark, it silently stops it.
    """

    def test_the_client_sets_an_explicit_timeout(self) -> None:
        from buyer.brains import REQUEST_TIMEOUT_SECONDS

        assert 0 < REQUEST_TIMEOUT_SECONDS <= 180, (
            "a timeout long enough to be indistinguishable from a hang is not a timeout"
        )

    def test_the_timeout_is_passed_to_the_client(self) -> None:
        """Asserted on the source, because constructing the client needs a key and the
        thing under test is that the argument is present at all."""
        import inspect

        from buyer import brains

        source = inspect.getsource(brains.OpenRouterBrain)
        assert "timeout=REQUEST_TIMEOUT_SECONDS" in source
        # The SDK would otherwise retry underneath our own retry loop, multiplying the two
        # and hiding which layer was struggling.
        assert "max_retries=0" in source

    def test_a_timeout_is_treated_as_transient(self) -> None:
        """It has to be retryable, or the fix converts a hang into a permanent failure."""
        from buyer.brains import _transient

        assert _transient(TimeoutError("request timed out")) is True
        assert _transient(Exception("Request timeout after 90s")) is True
