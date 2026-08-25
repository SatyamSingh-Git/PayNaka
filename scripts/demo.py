"""The whole argument, in one command and about ninety seconds.

Five things were true and separately demonstrable, and a reviewer under time pressure runs
one command. Worse, the one they ran was `make demo-attack` -- the poisoned-catalogue
injection, which this project's own evidence says **is real but rare**: 4 breaches
in 752 undefended runs across three labs, Rs 3,30,860 escaped. The two attacks below land every time and need
no model to be fooled, and leading with the rarest one buried them.

So the order here is the order of the argument, not the order things were built:

1. **The attack that beats every model.** A price that moves between reading it and paying
   it. No injected text, no reasoning error -- 5 of 5 real models paid, 0 of 5 re-checked
   before paying. Nothing about a smarter agent helps.
2. **The attack with no attacker at all.** A duplicate webhook. Nobody is being hostile and
   the merchant is out Rs 3,994.
3. **The injection everybody expects**, shown honestly: it lands on 3 of 3 frontier models,
   though in only 0.53% of runs, and the checkpoint refuses every one.
4. **What the checkpoint costs**, because a defence nobody will deploy is not a defence.

No keys, no network, no model. Every number below is produced on this machine, now.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from dataclasses import dataclass

from paynaka.tty import BOLD, DIM, GREEN, OFF, YELLOW, say

__all__ = ["ACTS", "Act", "main"]


@dataclass(frozen=True, slots=True)
class Act:
    """One step of the story: what it shows, and the module that shows it."""

    title: str
    why: str
    module: str
    argv: tuple[str, ...] = ()


ACTS: tuple[Act, ...] = (
    Act(
        title="1 · The attack where a smarter agent cannot help",
        why=(
            "The merchant changes the price after the agent reads it and before the money "
            "moves. There is no injected text to be suspicious of and no reasoning error to "
            "correct, so a better model behaves identically. Five real models, five labs: "
            "all five paid, none re-checked before paying."
        ),
        module="haat.toctou",
    ),
    Act(
        title="2 · The attack with nobody attacking",
        why=(
            "A duplicate webhook. No adversary, no injection, no model -- just ordinary "
            "delivery semantics, and a naive handler is out Rs 3,994. The gate that contains "
            "a hostile agent is the same gate that contains a redelivery, for the same reason."
        ),
        module="chaos.runner",
    ),
    Act(
        title="3 · The injection everybody expects, reported honestly",
        why=(
            "A poisoned product review tells the agent to add a Rs 50,000 gift card. "
            "Measured: it lands on 3 of 3 frontier models, in 0.53% of runs, costing "
            "Rs 82,715 a time. The checkpoint refuses every one."
        ),
        module="buyer.cli",
        argv=("--scenario", "attack", "--compare"),
    ),
    Act(
        title="4 · What the checkpoint costs",
        why=(
            "A defence nobody will deploy is not a defence. The mandate checks are ten "
            "microseconds; almost all of a decision is the state store, not the checking."
        ),
        module="haat.latency",
        argv=("--iterations", "500"),
    ),
)


def _rule(text: str = "") -> None:
    say(f"{DIM}{'─' * 78}{OFF}")
    if text:
        say(text)


def run(act: Act) -> int:
    """Run one act in its own process, so a crash in one still tells the rest of the story."""
    _rule()
    say(f"{BOLD}{act.title}{OFF}")
    say()
    for line in _wrap(act.why):
        say(f"{DIM}{line}{OFF}")
    say()

    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-m", act.module, *act.argv],
        check=False,
    )
    if completed.returncode != 0:
        say(f"{YELLOW}({act.module} exited {completed.returncode}){OFF}")
    return completed.returncode


def _wrap(text: str, width: int = 78) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        if len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.demo", description=__doc__)
    parser.add_argument(
        "--skip-latency",
        action="store_true",
        help="drop the timing act, which is the slowest and the least surprising",
    )
    args = parser.parse_args(argv)

    acts = ACTS[:-1] if args.skip_latency else ACTS

    say()
    say(f"{BOLD}PayNaka{OFF} {DIM}पे-नाका — a checkpoint on the road{OFF}")
    say()
    for line in _wrap(
        "An authority-containment layer for money-moving AI agents. Everything below runs "
        "on this machine with no keys, no network and no model. The order is the order of "
        "the argument: the attacks that a better agent cannot avoid come first."
    ):
        say(f"{DIM}{line}{OFF}")
    say()

    started = time.perf_counter()
    failures = sum(1 for act in acts if run(act) != 0)
    elapsed = time.perf_counter() - started

    _rule()
    say()
    say(f"{BOLD}What that showed{OFF}")
    say()
    for line in _wrap(
        "Two attacks that need no model to be fooled, one that does, and the cost of "
        "stopping all three. The checkpoint moved Rs 0 in every case, and it decided in "
        "code -- paynaka/gate.py imports no LLM SDK, which is a claim you can check by "
        "reading one import block."
    ):
        say(line)
    say()
    for line in _wrap(
        "What it does not claim: prompt injection is not solved. PayNaka does not stop an "
        "agent being persuaded; it stops a persuaded agent moving money outside its "
        "mandate. Those are different claims and only the second is made here. See "
        "docs/THREATMODEL.md for what is not defended, including the sentinel's 64.4% "
        "recall on held-out families against 92.1% on the corpus its rules were read from."
    ):
        say(f"{DIM}{line}{OFF}")
    say()
    say(f"{GREEN}{len(acts) - failures}/{len(acts)} acts in {elapsed:.0f}s{OFF}")
    say()
    return 1 if failures else 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
