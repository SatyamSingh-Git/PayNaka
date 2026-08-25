"""What a sweep costs, in one place.

`python -m scripts.estimate_cost` existed to answer this before anybody spent anything.
Nothing made a reader run it: `bench` announced itself as *"visible corpus, four defences
-> RESULTS.md"* and then made eighteen hundred calls to a paid model. A reader started it,
watched the counter reach 20/1800, and asked -- reasonably, and after the money had begun
moving -- whether this was going to be expensive.

That is the project's own rule broken on the project's own terms. Ambiguity in a money path
resolves to DENY, and a command that spends without being asked is not ambiguous about
anything; it just never asked. So the numbers live here, the estimator reads them, and the
sweep quotes them before its first call rather than after.

The prices are published USD list rates as of August 2026 and they will go stale. They are
printed wherever they are used, so a stale one is visible rather than buried, and a model
nobody has measured returns `None` instead of a confident guess.
"""

from __future__ import annotations

__all__ = ["MEASURED_PER_RUN", "MODELS", "OVERHEAD", "estimate_usd"]

#: USD per million tokens: (input, output). August 2026 list rates.
MODELS: dict[str, tuple[float, float]] = {
    "upstage/solar-pro4": (0.03, 0.12),
    "deepseek/deepseek-v4-flash": (0.04886, 0.09772),
    "poolside/laguna-xs-2.1": (0.06, 0.12),
    "xiaomi/mimo-v2.5": (0.119, 0.238),
    "z-ai/glm-5.2": (0.336, 1.056),
    "google/gemini-3.7-flash": (0.375, 1.875),
}

#: Per-run token usage measured against real models on a real attack case: (input, output).
#:
#: These replace an earlier assumption of 220 output tokens per turn, which was 2-6x too
#: high. Reasoning tokens are included, and they are a large share of it -- 65% of Laguna's
#: output and 36% of DeepSeek's -- so a model that "thinks" is not free even when its
#: visible answer is short. Solar Pro 4 emits none.
MEASURED_PER_RUN: dict[str, tuple[int, int]] = {
    "deepseek/deepseek-v4-flash": (7537, 830),
    "upstage/solar-pro4": (6425, 176),
    "poolside/laguna-xs-2.1": (4600, 353),
}

#: Runs that get denied take more turns, because the agent retries. The judge row makes
#: extra model calls. Neither is in the measured figures above, which come from clean runs.
OVERHEAD = 1.5


def estimate_usd(model: str, runs: int, *, overhead: float = OVERHEAD) -> float | None:
    """What `runs` runs of `model` should cost, or None if we cannot honestly say.

    `None` for any model without both a measured token count and a published price. A
    figure invented for an unmeasured model would be quoted back at a reader about to
    spend money, which is worse than admitting the number is not known -- the caller can
    say "unknown" and point at `estimate`, and a reader can decide with that.
    """
    if runs <= 0:
        return 0.0
    tokens = MEASURED_PER_RUN.get(model)
    prices = MODELS.get(model)
    if tokens is None or prices is None:
        return None
    tokens_in, tokens_out = tokens
    price_in, price_out = prices
    per_run = (tokens_in * price_in + tokens_out * price_out) / 1_000_000
    return per_run * runs * overhead
