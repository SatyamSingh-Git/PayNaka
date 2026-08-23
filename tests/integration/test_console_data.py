"""The committed evidence must match what the code currently produces.

``console/public/chaos.json`` and ``sentinel.json`` are checked in so that a reviewer who
clones the repo sees real numbers without running anything. That convenience has an
obvious failure mode: the code changes, the JSON does not, and the console goes on showing
a result that stopped being true three commits ago. Committed evidence that nothing checks
is worse than no evidence, because it looks authoritative.

Both generators are deterministic and need no keys and no network, so "regenerate it and
compare" is a test rather than an aspiration. If this fails, run ``make console-data``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts.console_data import chaos_payload, sentinel_payload, toctou_payload

pytestmark = pytest.mark.integration

PUBLIC = Path("console/public")


def _committed(name: str) -> dict:  # type: ignore[type-arg]
    path = PUBLIC / name
    if not path.exists():
        pytest.fail(f"{path} is missing. Run `make console-data`.")
    return json.loads(path.read_text(encoding="utf-8"))


class TestTheCommittedNumbersAreCurrent:
    def test_chaos_json_matches_a_fresh_run(self) -> None:
        assert _committed("chaos.json") == chaos_payload(), (
            "console/public/chaos.json is stale. Run `make console-data`."
        )

    def test_sentinel_json_matches_a_fresh_run(self) -> None:
        assert _committed("sentinel.json") == sentinel_payload(), (
            "console/public/sentinel.json is stale. Run `make console-data`."
        )

    def test_toctou_json_matches_a_fresh_run(self) -> None:
        assert _committed("toctou.json") == toctou_payload(), (
            "console/public/toctou.json is stale. Run `make console-data`."
        )


class TestTheShapeTheScreenExpects:
    """The console reads these keys by name. A rename here is a blank panel there."""

    def test_chaos_carries_every_field_the_screen_reads(self) -> None:
        payload = _committed("chaos.json")
        assert set(payload) >= {"scenarios", "totals"}
        assert set(payload["totals"]) >= {"naive_overspent", "paynaka_overspent"}
        for scenario in payload["scenarios"]:
            assert set(scenario) >= {
                "key",
                "title",
                "hazard",
                "why",
                "naive",
                "paynaka",
                "prevented",
            }
            for side in ("naive", "paynaka"):
                assert set(scenario[side]) >= {
                    "left_the_gateway",
                    "ledger_says",
                    "overspent",
                    "books_disagree",
                }

    def test_sentinel_carries_every_field_the_screen_reads(self) -> None:
        payload = _committed("sentinel.json")
        assert set(payload) >= {
            "threshold",
            "attacks",
            "negatives",
            "caught",
            "recall",
            "precision",
            "false_positive_rate",
            "false_positives",
            "margin",
        }

    def test_every_money_field_is_integer_paise(self) -> None:
        """The screen formats to rupees once, at the edge. Anything else is a float bug."""
        payload = _committed("chaos.json")
        for scenario in payload["scenarios"]:
            for side in ("naive", "paynaka"):
                for key in ("left_the_gateway", "ledger_says", "entitled", "overspent"):
                    value = scenario[side][key]
                    assert isinstance(value, int) and not isinstance(value, bool), (
                        f"{scenario['key']}.{side}.{key} is {type(value).__name__}"
                    )


class TestTheShapeOfTocTou:
    def test_it_carries_every_field_the_screen_reads(self) -> None:
        payload = _committed("toctou.json")
        assert set(payload) >= {"listed", "authorised", "mutations", "runs", "totals"}
        assert set(payload["totals"]) >= {"none", "prompt", "naka"}
        for run in payload["runs"]:
            assert set(run) >= {"defence", "mutation", "overspent", "overpaid_vs_listed"}

    def test_prompt_hardening_loses_exactly_what_no_defence_loses(self) -> None:
        """The finding, asserted. A gap between these two rows would be news."""
        totals = _committed("toctou.json")["totals"]
        assert totals["prompt"] == totals["none"] > 0

    def test_paynaka_loses_nothing(self) -> None:
        assert _committed("toctou.json")["totals"]["naka"] == 0


class TestTheClaimsItMakes:
    def test_paynaka_overspends_nothing_in_the_published_numbers(self) -> None:
        """The headline on the screen. If it ever stops being true, fail here first."""
        assert _committed("chaos.json")["totals"]["paynaka_overspent"] == 0

    def test_the_sentinel_margin_is_published_alongside_the_rate(self) -> None:
        """A zero false-positive rate without its margin is the misleading half."""
        payload = _committed("sentinel.json")
        assert "margin" in payload
        if payload["false_positive_rate"] == 0:
            assert payload["margin"] > 0

    def test_bench_results_are_absent_rather_than_faked(self) -> None:
        """HAAT needs a model key. An empty section is the honest state, not a zeroed one."""
        bench = PUBLIC / "bench.json"
        if bench.exists():
            payload = json.loads(bench.read_text(encoding="utf-8"))
            assert payload.get("runs", 0) > 0, "a bench.json with no runs should not exist"
