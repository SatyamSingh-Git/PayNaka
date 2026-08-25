"""Measure what a HAAT sweep actually costs, by instrumenting the real payloads.

Not an estimate from first principles. This runs the scripted agent through real corpus
cases, captures every request that *would* have gone to a model -- system prompt, tool
schemas, and the full conversation history at each turn -- and counts them.

The number that surprises people is input tokens. An agent loop resends the entire history
on every turn, so a run costing N tokens on turn one costs roughly N + M on turn two and
N + M + P on turn three. Input grows quadratically in turns, and it dominates everything.

Run: ``python -m scripts.estimate_cost``
"""

from __future__ import annotations

import argparse
import itertools
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from buyer.agent import BuyerAgent, load_prompt
from buyer.brains import ScriptedBrain, Step, Turn, _to_openai
from buyer.tools import TOOL_SCHEMAS, ToolBox
from haat.cost import MEASURED_PER_RUN, MODELS, OVERHEAD
from haat.runner import DEFAULT_INTENT, _fresh_stack, _poison
from haat.schema import load_corpus
from merchant.app import reset_catalog
from paynaka.env import load_env
from paynaka.mandate import IntentMandate

# The prices and the measured token counts live in `haat.cost`, because `bench` quotes
# them before it spends anything and two copies of a price is two answers to "what will
# this cost". This script is the long form: it measures real payloads and projects across
# turn counts. The constants are the same ones the sweep's own confirmation uses.
ASSUMED_OUTPUT_PER_TURN = 220


@dataclass
class Recorder:
    """A brain that records what it was asked and then defers to a scripted plan."""

    inner: ScriptedBrain
    name: str = "recorder"
    model: str = "recorder"
    served_by: str | None = None
    requests: list[int] = field(default_factory=list)

    def next_step(
        self, system: str, history: Sequence[Turn], tools: Sequence[dict[str, Any]]
    ) -> Step:
        # Serialise exactly what the OpenRouter brain would put on the wire, so the count
        # includes tool-call scaffolding and JSON punctuation rather than just prose.
        payload = json.dumps(
            {
                "messages": [{"role": "system", "content": system}, *_to_openai(history)],
                "tools": list(tools),
            },
            ensure_ascii=False,
        )
        self.requests.append(len(payload))
        return self.inner.next_step(system, history, tools)


def _measure(case_kind: str, sample: int, encoder: Any) -> tuple[list[float], float]:
    """Per-turn payload sizes (characters) averaged across a sample, and the turn count.

    Returned per turn rather than summed, because the growth between turns is the whole
    story: turn one carries the system prompt and tools, and every turn after it carries
    all of that plus the entire conversation so far.
    """
    corpus = load_corpus()
    plan = [
        [("search_catalog", {"query": "atta"})],
        [("get_product", {"sku": "ATTA-5KG"})],
        [("add_to_cart", {"sku": "ATTA-5KG", "qty": 1})],
        [("create_order", {"destination": "addr_home"})],
    ]

    per_turn: list[list[int]] = []

    cases = corpus.visible[:sample] if case_kind == "attack" else corpus.benign[:sample]
    for case in cases:
        reset_catalog()
        if case_kind == "attack":
            _poison(case)  # type: ignore[arg-type]

        naka, signer, _rail, clock = _fresh_stack(f"estimate:{case.case_id}")
        mandate = IntentMandate.create(
            clock=clock,
            subject="cust_kirana_001",
            session_id=f"sess_{case.case_id}",
            max_total=199_900,
            allowed_skus=("ATTA-5KG",),
            allowed_destinations=("addr_home",),
            max_qty_per_sku=3,
            allowed_actions=("create_order", "capture_payment", "create_refund"),
        )
        recorder = Recorder(inner=ScriptedBrain(plan=[list(step) for step in plan]))
        toolbox = ToolBox(naka=naka, signed_mandate=signer.sign(mandate), mandate=mandate)
        agent = BuyerAgent(
            brain=recorder,  # type: ignore[arg-type]
            tools=toolbox,
            system_prompt=load_prompt("naive"),
        )
        agent.shop(DEFAULT_INTENT if case_kind == "attack" else case.instruction)  # type: ignore[union-attr]

        per_turn.append(list(recorder.requests))

    reset_catalog()
    width = max(len(row) for row in per_turn)
    averages = [
        sum(row[i] for row in per_turn if i < len(row))
        / max(1, sum(1 for row in per_turn if i < len(row)))
        for i in range(width)
    ]
    return averages, float(width)


def _project(per_turn: list[float], turns: int) -> float:
    """Input characters for a run of ``turns`` turns.

    Measured turns are used directly. Beyond the measured window the last observed
    turn-over-turn delta is extended, which is the right shape: an agent that keeps going
    keeps re-sending a history that keeps growing, so input is quadratic in turns and
    linear projections badly understate long runs.
    """
    if turns <= len(per_turn):
        return sum(per_turn[:turns])

    deltas = [b - a for a, b in itertools.pairwise(per_turn)]
    delta = sum(deltas) / len(deltas) if deltas else 0.0
    total = sum(per_turn)
    last = per_turn[-1]
    for _ in range(turns - len(per_turn)):
        last += delta
        total += last
    return total


def main(argv: list[str] | None = None) -> int:
    load_env()
    parser = argparse.ArgumentParser(prog="python -m scripts.estimate_cost")
    parser.add_argument("--sample", type=int, default=12, help="cases to measure per kind")
    args = parser.parse_args(argv)

    try:
        import tiktoken

        encoder = tiktoken.get_encoding("o200k_base")
    except Exception:
        encoder = None

    attack_turns, _ = _measure("attack", args.sample, encoder)
    benign_turns, _ = _measure("benign", args.sample, encoder)

    if encoder is not None:
        corpus_probe = load_corpus()
        probe = json.dumps(
            {
                "tools": TOOL_SCHEMAS,
                "prompt": load_prompt("naive"),
                "case": corpus_probe.visible[0].payload,
            },
            ensure_ascii=False,
        )
        ratio = len(probe) / max(1, len(encoder.encode(probe)))
    else:
        ratio = 3.6

    corpus = load_corpus()
    n_visible, n_sealed, n_benign = len(corpus.visible), len(corpus.sealed), len(corpus.benign)
    defences = 4

    print(
        f"chars per token: {ratio:.2f}"
        f"{'  [tiktoken o200k_base]' if encoder else '  [no tiktoken; assumed]'}"
    )
    print()
    print("Measured per-turn input, real payloads (tokens):")
    for i, chars in enumerate(attack_turns, start=1):
        print(f"  turn {i}   {chars / ratio:8,.0f}")
    print(
        "  -> the growth per turn is the history being resent; it is why long runs cost "
        "much more than short ones"
    )
    print()
    print("Sweep shape:")
    print(
        f"  visible   ({n_visible} attacks + {n_benign} benign) x {defences} defences "
        f"= {(n_visible + n_benign) * defences:,} runs"
    )
    print(
        f"  sealed    ({n_sealed} attacks, no benign)     x {defences} defences "
        f"= {n_sealed * defences:,} runs"
    )
    print(
        f"  both                                                = "
        f"{(n_visible + n_benign + n_sealed) * defences:,} runs"
    )
    print()

    scenarios = [
        (5, "measured floor -- agent goes straight to checkout"),
        (8, "realistic -- some exploration, one retry after a denial"),
        (12, "pessimistic -- hits the max_turns bound"),
    ]

    print("Both sweeps, by how many turns a real agent actually takes:")
    print()
    header = f"  {'turns':>5}  {'input':>9}  {'output':>8}   " + "  ".join(
        f"{m.split('/')[-1]:>18}" for m in MODELS
    )
    print(header)
    print("  " + "-" * (len(header) - 2))

    for turns, label in scenarios:
        attack_in = _project(attack_turns, turns) / ratio
        benign_in = _project(benign_turns, turns) / ratio
        out_per_run = turns * ASSUMED_OUTPUT_PER_TURN

        attack_runs = (n_visible + n_sealed) * defences
        benign_runs = n_benign * defences

        total_in = attack_runs * attack_in + benign_runs * benign_in
        total_out = (attack_runs + benign_runs) * out_per_run

        costs = []
        for price_in, price_out in MODELS.values():
            costs.append(f"${total_in / 1e6 * price_in + total_out / 1e6 * price_out:17,.2f}")

        print(
            f"  {turns:>5}  {total_in / 1e6:>8.1f}M  {total_out / 1e6:>7.2f}M   " + "  ".join(costs)
        )
        print(f"         {label}")

    print()
    print("Against measured per-run usage (real models, real case), all four defences:")
    print()
    total_runs = (n_visible + n_sealed + n_benign) * defences
    for slug, (per_in, per_out) in MEASURED_PER_RUN.items():
        price_in, price_out = MODELS[slug]
        tin = total_runs * per_in / 1e6
        tout = total_runs * per_out / 1e6
        base = tin * price_in + tout * price_out
        print(
            f"  {slug:30s} {tin:6.1f}M in {tout:5.2f}M out   "
            f"${base:5.2f}  ->  ${base * OVERHEAD:5.2f} with denial/judge overhead"
        )
    measured_total = sum(
        (total_runs * i / 1e6) * MODELS[s][0] + (total_runs * o / 1e6) * MODELS[s][1]
        for s, (i, o) in MEASURED_PER_RUN.items()
    )
    print()
    print(f"  all three, worst case: ${measured_total * OVERHEAD:.2f}")
    print()
    print("Notes")
    print("  - The judge row makes one extra model call per money action, and a second")
    print("    when the cheap tier hedges. Add roughly 25% if you run all four defences.")
    print("  - Input dominates: an agent loop resends the entire history every turn, so")
    print("    input grows quadratically in turns while output grows linearly.")
    print("  - Nothing here assumes prompt caching. OpenRouter support varies by host;")
    print("    where it works, input cost drops substantially.")
    print("  - Prices are Aug 2026 list rates, printed above so a stale one is obvious.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
