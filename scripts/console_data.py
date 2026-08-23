"""Write everything the console can show into ``console/public/``.

The Benchmark screen used to fetch ``/RESULTS.json``, a file nothing produced, from a
directory that did not exist. It rendered its empty state on every run and had done since
it was written -- a screen that has never displayed data is a mock with a fetch call in it.

Three files come out of here, and which of them exist is itself the honest answer to what
this project has actually measured:

``chaos.json``     always. Six webhook-delivery scenarios, no model, no keys, no network.
                   Reproducible to the paise on any machine that can run Python.

``sentinel.json``  always. Recall and false positives for the layer-two detector, with the
                   margin, over the visible families and the hard negatives.

``bench.json``     only if ``make bench`` has been run and produced ``RESULTS.json``. It
                   needs a model key. As of this writing the honest result from that run
                   is that plain-text catalog injection does not reliably land on 2026
                   tool-calling models, and the console says so rather than showing four
                   rows of zeroes that read like a triumph.

Run: ``python -m scripts.console_data``  (or ``make console-data``)
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

from chaos.runner import SCENARIOS, run_scenario
from haat.sentinel_eval import evaluate
from haat.toctou import LISTED, MOMENTS, MUTATIONS, run_case
from paynaka.sentinel import THRESHOLD
from paynaka.tty import DIM, GREEN, OFF, YELLOW, say

PUBLIC = Path("console/public")
RESULTS = Path("RESULTS.json")


def chaos_payload() -> dict[str, Any]:
    results = [run_scenario(scenario) for scenario in SCENARIOS]
    return {
        "scenarios": [r.to_dict() for r in results],
        "totals": {
            "naive_overspent": sum(r.naive.overspent for r in results),
            "paynaka_overspent": sum(r.naka.overspent for r in results),
            "naive_underpaid": sum(r.naive.underpaid for r in results),
        },
    }


def toctou_payload() -> dict[str, Any]:
    runs = [
        run_case(defence, moment, mutation)
        for defence in ("none", "prompt", "naka")
        for moment, _ in MOMENTS
        for mutation in MUTATIONS
    ]
    return {
        "listed": LISTED,
        "authorised": LISTED,
        "mutations": [
            {"key": m.key, "label": m.label, "why": m.why, "charged": m.applied_to(LISTED)}
            for m in MUTATIONS
        ],
        "runs": [r.to_dict() for r in runs],
        "totals": {
            defence: sum(r.overspent for r in runs if r.defence == defence)
            for defence in ("none", "prompt", "naka")
        },
    }


def sentinel_payload() -> dict[str, Any]:
    results = evaluate()
    payload = results.to_dict()
    payload["margin"] = results.margin
    if results.nearest_misses:
        negative, scan = results.nearest_misses[0]
        payload["nearest_miss"] = {
            "case_id": negative.case_id,
            "score": scan.score,
            "threshold": THRESHOLD,
            "rules": list(scan.rules),
            "text": scan.text.strip()[:200],
        }
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.console_data")
    parser.add_argument("--out", default=str(PUBLIC), help="where the console serves from")
    args = parser.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    written: list[str] = []
    for name, payload in (
        ("chaos.json", chaos_payload()),
        ("sentinel.json", sentinel_payload()),
        ("toctou.json", toctou_payload()),
    ):
        (out / name).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        written.append(name)

    if RESULTS.exists():
        shutil.copyfile(RESULTS, out / "bench.json")
        written.append("bench.json")
        say(f"{GREEN}copied{OFF} {RESULTS} -> {out / 'bench.json'}")
    else:
        say(
            f"{YELLOW}skipped{OFF} bench.json: {RESULTS} does not exist. "
            f"{DIM}It needs a model key; run `make bench`.{OFF}"
        )

    say(f"{GREEN}wrote{OFF} {', '.join(written)} into {out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
