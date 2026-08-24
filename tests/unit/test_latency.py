"""Tests for the latency measurement.

A benchmark is a claim, so it gets tested like one. Both halves are here because the unit
under test is one small pure function plus a harness: the forward cases establish that the
percentiles are the percentiles, and the degenerate cases establish that a measurement
never reports a number that is arithmetically impossible -- a p99 below a p50 in a README
is worse than no benchmark, because somebody will quote it.

The timing assertions are deliberately loose. A tight bound would be a test that fails on
a loaded CI runner and teaches everyone to ignore it; these are wide enough to pass on
anything and still catch the regression that matters, which is an accidentally quadratic
check turning microseconds into milliseconds.
"""

from __future__ import annotations

import json

import pytest

from haat.latency import RAIL_CALL_MS, Timing, _percentiles, main, measure


class TestPercentiles:
    def test_a_known_distribution_lands_on_known_ranks(self) -> None:
        """Nearest-rank: every reported figure is a duration some call actually took."""
        samples = [n * 1_000 for n in range(1, 101)]  # 1..100 microseconds
        t = _percentiles("x", samples)
        assert t.samples == 100
        assert t.p50 == 50.0
        assert t.p95 == 95.0
        assert t.p99 == 99.0
        assert t.worst == 100.0

    def test_unsorted_input_is_sorted_first(self) -> None:
        ordered = _percentiles("x", [1_000, 2_000, 3_000])
        shuffled = _percentiles("x", [3_000, 1_000, 2_000])
        assert ordered == shuffled

    def test_nanoseconds_are_reported_as_microseconds(self) -> None:
        assert _percentiles("x", [5_000]).p50 == 5.0

    def test_the_millisecond_view_agrees_with_the_microsecond_one(self) -> None:
        t = _percentiles("x", [2_500_000])
        assert t.p99 == 2500.0
        assert t.p99_ms == 2.5

    def test_the_dict_carries_both_units(self) -> None:
        payload = _percentiles("label", [1_000]).to_dict()
        assert payload["label"] == "label"
        assert payload["p99"] == 1.0
        assert payload["p99_ms"] == 0.001


class TestDegenerateInputCannotProduceAnImpossibleNumber:
    @pytest.mark.parametrize("count", [1, 2, 3, 5, 10, 99, 100, 101, 1_000])
    def test_the_percentiles_are_always_ordered(self, count: int) -> None:
        """The invariant that makes the table quotable, at every size where an off-by-one
        in the rank calculation would break it."""
        t = _percentiles("x", [n * 17 for n in range(count)])
        assert t.p50 <= t.p95 <= t.p99 <= t.worst

    @pytest.mark.parametrize("count", [1, 2, 7, 250])
    def test_identical_samples_give_identical_percentiles(self, count: int) -> None:
        t = _percentiles("x", [4_000] * count)
        assert (t.p50, t.p95, t.p99, t.worst) == (4.0, 4.0, 4.0, 4.0)

    def test_a_single_sample_is_every_percentile(self) -> None:
        t = _percentiles("x", [9_000])
        assert (t.p50, t.p95, t.p99, t.worst) == (9.0, 9.0, 9.0, 9.0)

    def test_one_enormous_outlier_does_not_drag_the_median(self) -> None:
        """A mean would be destroyed by this, which is why the table reports neither."""
        t = _percentiles("x", [1_000] * 999 + [60_000_000_000])
        assert t.p50 == 1.0
        assert t.worst == 60_000_000.0

    def test_zero_duration_samples_are_not_an_error(self) -> None:
        """A clock too coarse to resolve a fast call reports zero, and that is data."""
        t = _percentiles("x", [0, 0, 1_000])
        assert t.p50 == 0.0
        assert t.worst == 1.0


@pytest.fixture(scope="module")
def timings() -> dict[str, Timing]:
    """One small run, shared across the harness tests. Enough iterations to be a
    measurement, few enough that the suite does not become a benchmark."""
    return measure(iterations=40, warmup=5)


class TestTheHarness:
    def test_every_layer_is_measured(self, timings: dict[str, Timing]) -> None:
        assert set(timings) == {"envelope", "gate", "rail", "engine"}
        assert all(t.samples == 40 for t in timings.values())

    def test_the_envelope_checks_cost_less_than_the_whole_gate(
        self, timings: dict[str, Timing]
    ) -> None:
        """The decomposition's whole point: the checks are nearly free and the state store
        is where a decision's time goes. If this ever inverts, the claim has changed."""
        assert timings["envelope"].p50 < timings["gate"].p50

    def test_the_full_path_costs_more_than_the_gate_alone(self, timings: dict[str, Timing]) -> None:
        assert timings["engine"].p50 > timings["gate"].p50

    def test_the_checks_stay_in_microseconds(self, timings: dict[str, Timing]) -> None:
        """Loose on purpose -- a bound that fails on a loaded runner teaches people to
        ignore it. This catches an accidentally quadratic check, nothing subtler."""
        assert timings["envelope"].p99 < 5_000  # 5 ms for seven pure functions

    def test_the_reference_rail_figure_is_not_flattering(self) -> None:
        """A checkpoint that looks cheap only next to a slow network is not a result, so
        the comparison uses the optimistic end of a hosted API's range."""
        assert RAIL_CALL_MS <= 150.0

    def test_a_single_iteration_run_completes(self) -> None:
        """The smallest run that is still a run, because the percentile ranks are where an
        off-by-one on n=1 would hide."""
        timings = measure(iterations=1, warmup=0)
        assert all(t.samples == 1 for t in timings.values())


class TestTheEntryPoint:
    def test_it_runs_and_reports_success(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["--iterations", "5", "--warmup", "0"]) == 0
        out = capsys.readouterr().out
        assert "envelope checks" in out
        assert "floor" in out  # the caveat is not optional output

    def test_the_json_is_machine_readable_and_names_the_hardware(
        self, tmp_path: object, capsys: pytest.CaptureFixture[str]
    ) -> None:
        target = f"{tmp_path}/nested/latency.json"
        assert main(["--iterations", "5", "--warmup", "0", "--json", target]) == 0
        capsys.readouterr()
        with open(target, encoding="utf-8") as handle:
            payload = json.load(handle)
        assert payload["machine"]
        assert payload["rail_call_ms_assumed"] == RAIL_CALL_MS
        assert set(payload["layers"]) == {"envelope", "gate", "rail", "engine"}

    @pytest.mark.parametrize(
        "argv",
        [
            ["--iterations", "0"],
            ["--iterations", "-1"],
            ["--warmup", "-1"],
            ["--iterations", "-100", "--warmup", "-100"],
        ],
        ids=repr,
    )
    def test_a_nonsensical_run_is_refused_rather_than_reported(self, argv: list[str]) -> None:
        """Zero iterations would divide by nothing and print a table of zeros, which reads
        exactly like a very fast checkpoint."""
        with pytest.raises(SystemExit) as exit_info:
            main(argv)
        assert exit_info.value.code == 2
