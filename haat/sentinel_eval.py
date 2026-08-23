"""How good is the sentinel, really? Measured against hard negatives, reported separately.

Recall alone is a number anybody can hit. Flag every field and you catch 100% of attacks,
and the merchant finds out when honest sellers stop being able to list. So this reports
recall and the false-positive rate side by side, and the negatives it is scored against
are deliberately awkward: recipes full of imperative verbs, shoppers writing in capitals,
reviews quoting real bracketed error messages, Hindi and Tamil text, legitimate SKU codes
and currency comparisons. See ``haat/negatives/hard_negatives.yaml``.

Two things this deliberately does not do:

**It does not touch the sealed families.** ``obfuscated_payload`` and
``tool_call_smuggling`` are held out of development entirely, and a detector tuned against
them would be a lookup table wearing a classifier's clothes. They are scored only after
the freeze, and the gap between visible recall and sealed recall is the honest measure of
whether this generalises. ``--include-sealed`` exists for that day and says so loudly.

**It does not feed the gate.** Nothing in this file changes a money decision. PayNaka's
guarantee holds with the sentinel switched off entirely; these numbers describe a warning
light, not a lock.

Run: ``python -m haat.sentinel_eval``            visible families + hard negatives
     ``python -m haat.sentinel_eval --per-rule`` which rule caught what, and what it cost
"""

from __future__ import annotations

import argparse
import json
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from haat.schema import SEALED, AttackCase, load_corpus
from merchant.catalog import CATALOG
from paynaka.sentinel import THRESHOLD, Scan, scan
from paynaka.tty import BOLD, DIM, GREEN, OFF, RED, YELLOW, say

NEGATIVES = Path("haat/negatives")


@dataclass(frozen=True, slots=True)
class Negative:
    """One piece of genuine merchant text that a nervous detector would want to flag."""

    case_id: str
    vector: str
    text: str
    trap: str = ""
    source: str = "fixture"


def load_negatives(root: Path = NEGATIVES) -> list[Negative]:
    """The hard-negative fixtures, plus every text field of the real catalogue.

    The catalogue is included because it is the text the sentinel will actually see in a
    demo, and a detector that fires on the shop's own product descriptions is not usable
    however well it scores on a fixture file.
    """
    out: list[Negative] = []
    for path in sorted(root.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        for entry in raw:
            out.append(
                Negative(
                    case_id=str(entry["id"]),
                    vector=str(entry.get("vector", "review")),
                    text=str(entry["text"]),
                    trap=str(entry.get("trap", "")),
                )
            )

    for sku, product in sorted(CATALOG.items()):
        for attr in ("title", "description", "seller_note", "image_alt"):
            value = getattr(product, attr, None)
            if value:
                out.append(
                    Negative(
                        case_id=f"catalog.{sku}.{attr}",
                        vector=attr,
                        text=str(value),
                        source="catalog",
                    )
                )
        for index, review in enumerate(product.reviews):
            out.append(
                Negative(
                    case_id=f"catalog.{sku}.review.{index}",
                    vector="review",
                    text=review.body,
                    source="catalog",
                )
            )
    return out


@dataclass
class Results:
    caught: list[tuple[AttackCase, Scan]] = field(default_factory=list)
    missed: list[tuple[AttackCase, Scan]] = field(default_factory=list)
    false_positives: list[tuple[Negative, Scan]] = field(default_factory=list)
    true_negatives: int = 0
    #: Every benign field that scored above zero, worst first. A false-positive rate of
    #: zero says nothing about how nearly it happened, and a detector whose closest honest
    #: text sits one signal below the line is one rule change away from a bad afternoon.
    nearest_misses: list[tuple[Negative, Scan]] = field(default_factory=list)

    @property
    def margin(self) -> int:
        """How far the highest-scoring benign field sat below the threshold."""
        if not self.nearest_misses:
            return THRESHOLD
        return THRESHOLD - self.nearest_misses[0][1].score

    @property
    def attacks(self) -> int:
        return len(self.caught) + len(self.missed)

    @property
    def negatives(self) -> int:
        return len(self.false_positives) + self.true_negatives

    @property
    def recall(self) -> float:
        return len(self.caught) / self.attacks if self.attacks else 0.0

    @property
    def false_positive_rate(self) -> float:
        return len(self.false_positives) / self.negatives if self.negatives else 0.0

    @property
    def precision(self) -> float:
        flagged = len(self.caught) + len(self.false_positives)
        return len(self.caught) / flagged if flagged else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "threshold": THRESHOLD,
            "attacks": self.attacks,
            "negatives": self.negatives,
            "caught": len(self.caught),
            "missed": len(self.missed),
            "false_positives": len(self.false_positives),
            "recall": round(self.recall, 4),
            "precision": round(self.precision, 4),
            "false_positive_rate": round(self.false_positive_rate, 4),
            "f1": round(self.f1, 4),
            "missed_ids": [case.case_id for case, _ in self.missed],
            "false_positive_ids": [neg.case_id for neg, _ in self.false_positives],
        }


def evaluate(*, include_sealed: bool = False) -> Results:
    corpus = load_corpus()
    cases = list(corpus.visible)
    if include_sealed:
        cases += [c for c in corpus.attacks if c.family in SEALED]

    results = Results()
    for case in cases:
        result = scan(case.payload, field_name=str(case.vector))
        (results.caught if result.flagged else results.missed).append((case, result))

    for negative in load_negatives():
        result = scan(negative.text, field_name=negative.vector)
        if result.flagged:
            results.false_positives.append((negative, result))
        else:
            results.true_negatives += 1
            if result.score > 0:
                results.nearest_misses.append((negative, result))

    results.nearest_misses.sort(key=lambda pair: -pair[1].score)
    return results


# ====================================================================== reporting


def _bar(value: float, width: int = 24) -> str:
    filled = round(value * width)
    return "█" * filled + "·" * (width - filled)


def report(results: Results, *, per_rule: bool = False, sealed: bool = False) -> None:
    say()
    say(f"{BOLD}Sentinel{OFF}  {DIM}layer two. It notices; the gate is what stops things.{OFF}")
    if sealed:
        say(f"{YELLOW}Sealed families included. This consumes the held-out set -- say so.{OFF}")
    say()

    say(f"  {DIM}threshold{OFF}          {THRESHOLD}")
    say(f"  {DIM}attack payloads{OFF}    {results.attacks}")
    say(
        f"  {DIM}benign fields{OFF}      {results.negatives}  {DIM}(hard negatives + the real catalogue){OFF}"
    )
    say()

    recall = results.recall
    fpr = results.false_positive_rate
    say(
        f"  {BOLD}recall{OFF}             {_bar(recall)}  {recall:6.1%}   {DIM}{len(results.caught)}/{results.attacks} payloads flagged{OFF}"
    )
    colour = GREEN if fpr < 0.05 else (YELLOW if fpr < 0.15 else RED)
    say(
        f"  {BOLD}false positives{OFF}    {colour}{_bar(fpr)}{OFF}  {fpr:6.1%}   {DIM}{len(results.false_positives)}/{results.negatives} benign fields flagged{OFF}"
    )
    say(f"  {DIM}precision{OFF}          {results.precision:6.1%}")
    say(f"  {DIM}F1{OFF}                 {results.f1:6.1%}")
    say()

    if results.nearest_misses:
        worst, worst_scan = results.nearest_misses[0]
        colour = GREEN if results.margin >= 20 else YELLOW
        say(
            f"  {BOLD}margin{OFF}             {colour}{results.margin}{OFF} "
            f"{DIM}points. The closest honest text scored {worst_scan.score} of {THRESHOLD}:{OFF}"
        )
        say(f"    {DIM}{worst.case_id} -- {', '.join(worst_scan.rules)}{OFF}")
        say(
            f"    {DIM}A zero false-positive rate says nothing about how nearly it "
            f"happened. This is that number.{OFF}"
        )
        say()

    if results.missed:
        say(f"  {BOLD}missed{OFF} {DIM}-- payloads the sentinel did not flag{OFF}")
        by_family = Counter(str(case.family) for case, _ in results.missed)
        for family, count in by_family.most_common():
            say(f"    {family:24s} {count:>3}")
        quietest_case, quietest_scan = min(results.missed, key=lambda pair: pair[1].score)
        say(f"    {DIM}lowest scoring: {quietest_case.case_id} ({quietest_scan.score}){OFF}")
        say()

    if results.false_positives:
        say(f"  {BOLD}false positives{OFF} {DIM}-- honest text the sentinel flagged anyway{OFF}")
        for negative, result in results.false_positives:
            trap = f" (aimed at {negative.trap})" if negative.trap else ""
            say(
                f"    {RED}{negative.case_id}{OFF}  score {result.score}  {DIM}{', '.join(result.rules)}{trap}{OFF}"
            )
            say(f'      {DIM}"{result.text.strip()[:96]}"{OFF}')
        say()

    if per_rule:
        _per_rule(results)

    say(f"{DIM}These numbers are the sentinel's alone. They are not combined with the gate's,{OFF}")
    say(f"{DIM}and the gate's guarantee does not depend on any of them.{OFF}")
    say()


def _per_rule(results: Results) -> None:
    caught = Counter(rule for _, s in results.caught for rule in s.rules)
    cost = Counter(rule for _, s in results.false_positives for rule in s.rules)
    say(
        f"  {BOLD}per rule{OFF}  {DIM}how many attacks it appeared in, and how many benign fields{OFF}"
    )
    for rule in sorted(set(caught) | set(cost), key=lambda r: -caught[r]):
        flag = f"  {RED}<- costs{OFF}" if cost[rule] else ""
        say(f"    {rule:28s} attacks {caught[rule]:>4}   benign {cost[rule]:>3}{flag}")
    say()


def _frozen() -> bool:
    """Whether the freeze tag exists. Until it does, the sealed families are off limits."""
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "v1.0-freeze"],  # noqa: S607
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - no git, no freeze
        return False
    return completed.returncode == 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m haat.sentinel_eval")
    parser.add_argument(
        "--include-sealed",
        action="store_true",
        help="also score the held-out families. Doing this consumes them; say so in any "
        "number you publish afterwards.",
    )
    parser.add_argument("--per-rule", action="store_true", help="per-rule hit and cost table")
    parser.add_argument("--json", dest="json_path", help="write machine-readable results")
    args = parser.parse_args(argv)

    if args.include_sealed and not _frozen():
        # The same refusal ``make bench-sealed`` makes, for the same reason. The rules
        # below were written by reading the visible corpus; the sealed families are the
        # only evidence that any of it generalises, and that evidence is worth exactly
        # once. Spending it to make a number look better today is the whole trap.
        say(f"{RED}REFUSED{OFF}: tag v1.0-freeze does not exist. The sealed corpus stays sealed.")
        say(f"{DIM}  The sentinel was tuned by reading the visible families. Scoring it{OFF}")
        say(f"{DIM}  against the held-out ones is the only measurement that shows whether{OFF}")
        say(f"{DIM}  it generalises rather than remembers, and it can only be spent once.{OFF}")
        return 2

    results = evaluate(include_sealed=args.include_sealed)
    report(results, per_rule=args.per_rule, sealed=args.include_sealed)

    if args.json_path:
        Path(args.json_path).write_text(json.dumps(results.to_dict(), indent=2), encoding="utf-8")
        say(f"{DIM}wrote {args.json_path}{OFF}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
