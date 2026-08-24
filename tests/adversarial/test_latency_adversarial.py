"""Degenerate input to the latency measurement.

A benchmark is a claim, and the hostile cases for a claim are the ones that make it report
something arithmetically impossible. A p99 below a p50 in a README is worse than no
benchmark at all, because somebody will quote it and nobody will check it.

The other case here is a run that produces a table of zeros. Zero iterations printing
zeros looks exactly like a very fast checkpoint, which is the most flattering possible way
for a measurement to be broken.
"""

from __future__ import annotations

import pytest

from haat.latency import RAIL_CALL_MS, _percentiles, main

pytestmark = pytest.mark.adversarial


class TestNoInputProducesAnImpossibleNumber:
    @pytest.mark.parametrize("count", [1, 2, 3, 4, 5, 9, 10, 11, 19, 20, 99, 100, 101, 999, 1_000])
    def test_the_percentiles_are_always_ordered(self, count: int) -> None:
        """At every size where an off-by-one in the rank calculation would break it. The
        invariant is what makes the table quotable."""
        t = _percentiles("x", [n * 17 for n in range(count)])
        assert t.p50 <= t.p95 <= t.p99 <= t.worst

    @pytest.mark.parametrize("count", [1, 2, 7, 250, 1_000])
    def test_identical_samples_give_identical_percentiles(self, count: int) -> None:
        t = _percentiles("x", [4_000] * count)
        assert (t.p50, t.p95, t.p99, t.worst) == (4.0, 4.0, 4.0, 4.0)

    def test_a_single_sample_is_every_percentile(self) -> None:
        t = _percentiles("x", [9_000])
        assert (t.p50, t.p95, t.p99, t.worst) == (9.0, 9.0, 9.0, 9.0)

    def test_reversed_input_gives_the_same_answer_as_sorted(self) -> None:
        rising = [n * 1_000 for n in range(1, 501)]
        assert _percentiles("x", rising) == _percentiles("x", list(reversed(rising)))

    def test_one_enormous_outlier_does_not_drag_the_median(self) -> None:
        """A mean would be destroyed by this, which is why the table reports neither a mean
        nor a standard deviation."""
        t = _percentiles("x", [1_000] * 999 + [60_000_000_000])
        assert t.p50 == 1.0
        assert t.worst == 60_000_000.0

    def test_every_sample_zero_is_data_not_an_error(self) -> None:
        """A clock too coarse to resolve a fast call reports zero, and that is a finding
        about the clock rather than a reason to refuse to report."""
        t = _percentiles("x", [0] * 100)
        assert (t.p50, t.p99, t.worst) == (0.0, 0.0, 0.0)

    def test_the_worst_is_always_the_largest_sample(self) -> None:
        samples = [5_000, 1_000, 99_000, 3_000]
        assert _percentiles("x", samples).worst == 99.0

    @pytest.mark.parametrize("n", [1, 100, 1_000])
    def test_no_percentile_exceeds_the_largest_observation(self, n: int) -> None:
        """Nearest-rank, so every reported figure is a duration some call actually took.
        An interpolated percentile would report a number no call took."""
        samples = [i * 3 for i in range(1, n + 1)]
        t = _percentiles("x", samples)
        assert t.p99 <= max(samples) / 1000.0


class TestANonsensicalRunIsRefusedRatherThanReported:
    @pytest.mark.parametrize(
        "argv",
        [
            ["--iterations", "0"],
            ["--iterations", "-1"],
            ["--iterations", "-100000"],
            ["--warmup", "-1"],
            ["--iterations", "-1", "--warmup", "-1"],
        ],
        ids=repr,
    )
    def test_it_exits_rather_than_printing_zeros(self, argv: list[str]) -> None:
        """Zero iterations would print a table of zeros, which reads exactly like a very
        fast checkpoint -- the most flattering possible way to be broken."""
        with pytest.raises(SystemExit) as exit_info:
            main(argv)
        assert exit_info.value.code == 2

    @pytest.mark.parametrize("argv", [["--iterations", "abc"], ["--warmup", "1.5"]])
    def test_a_non_integer_count_is_refused(self, argv: list[str]) -> None:
        with pytest.raises(SystemExit):
            main(argv)

    def test_the_reference_rail_figure_is_not_flattering(self) -> None:
        """A checkpoint that looks cheap only next to a slow network is not a result, so the
        comparison uses the optimistic end of a hosted API's range. If somebody quietly
        raises this, the percentage in the README improves for no reason."""
        assert RAIL_CALL_MS <= 150.0
