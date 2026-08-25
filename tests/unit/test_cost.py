"""What a sweep costs, before anybody spends it.

`haat.cost` is quoted to a reader who is deciding whether to start a run, so the two
failure modes that matter are a wrong number and a confident number for a model nobody has
measured. Both are worse than "unknown".
"""

from __future__ import annotations

import pytest

from haat.cost import MEASURED_PER_RUN, MODELS, OVERHEAD, estimate_usd

DEEPSEEK = "deepseek/deepseek-v4-flash"


class TestTheArithmetic:
    def test_one_run_is_tokens_times_price(self) -> None:
        """Worked by hand from the constants, so a change to either shows up here rather
        than being absorbed silently."""
        tokens_in, tokens_out = MEASURED_PER_RUN[DEEPSEEK]
        price_in, price_out = MODELS[DEEPSEEK]
        expected = (tokens_in * price_in + tokens_out * price_out) / 1_000_000 * OVERHEAD
        assert estimate_usd(DEEPSEEK, 1) == pytest.approx(expected)

    def test_it_scales_with_the_number_of_runs(self) -> None:
        one = estimate_usd(DEEPSEEK, 1)
        assert one is not None
        assert estimate_usd(DEEPSEEK, 1800) == pytest.approx(one * 1800)

    def test_the_full_visible_sweep_is_about_a_dollar(self) -> None:
        """The number printed in the README and the Makefile help. If the constants move
        far enough that this fails, those two need editing as well -- which is the point."""
        total = estimate_usd(DEEPSEEK, 1800)
        assert total is not None
        assert 0.5 < total < 2.5, total

    def test_the_overhead_can_be_overridden(self) -> None:
        bare = estimate_usd(DEEPSEEK, 10, overhead=1.0)
        loaded = estimate_usd(DEEPSEEK, 10, overhead=2.0)
        assert bare is not None and loaded is not None
        assert loaded == pytest.approx(bare * 2)


class TestWhenItCannotSayHonestly:
    def test_an_unmeasured_model_returns_none(self) -> None:
        """Priced but never measured. A plausible guess here would be quoted to somebody
        about to spend money on it."""
        assert "z-ai/glm-5.2" in MODELS
        assert "z-ai/glm-5.2" not in MEASURED_PER_RUN
        assert estimate_usd("z-ai/glm-5.2", 100) is None

    @pytest.mark.parametrize("model", ["", "not/a-model", "gpt-9", DEEPSEEK.upper()])
    def test_an_unknown_model_returns_none(self, model: str) -> None:
        assert estimate_usd(model, 100) is None

    @pytest.mark.parametrize("runs", [0, -1, -1800])
    def test_nothing_to_run_costs_nothing(self, runs: int) -> None:
        """A fully-resumed sweep has no calls left, and must not quote a price for them."""
        assert estimate_usd(DEEPSEEK, runs) == 0.0


class TestTheConstantsStayCoherent:
    def test_every_measured_model_has_a_price(self) -> None:
        assert set(MEASURED_PER_RUN) <= set(MODELS)

    def test_prices_are_per_million_tokens_and_plausible(self) -> None:
        """A rate entered per-token rather than per-million would be a millionfold error in
        a number a reader trusts."""
        for model, (price_in, price_out) in MODELS.items():
            assert 0.0 < price_in < 100.0, model
            assert 0.0 < price_out < 100.0, model
            assert price_out >= price_in, f"{model}: output is not cheaper than input"

    def test_measured_usage_is_a_whole_number_of_tokens(self) -> None:
        for model, (tokens_in, tokens_out) in MEASURED_PER_RUN.items():
            assert isinstance(tokens_in, int) and isinstance(tokens_out, int), model
            assert tokens_in > tokens_out, f"{model}: an agent loop resends its history"

    def test_the_estimator_script_reads_these_and_keeps_no_copy(self) -> None:
        """Two copies of a price is two answers to 'what will this cost', and the one a
        reader sees is whichever command they happened to run."""
        from pathlib import Path

        source = Path("scripts/estimate_cost.py").read_text(encoding="utf-8")
        assert "from haat.cost import" in source
        assert "MODELS: dict[str, tuple[float, float]] = {" not in source
