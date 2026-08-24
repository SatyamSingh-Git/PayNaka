"""Tests for the shadow-mode report -- the thing an operator reads after a week.

Both halves live here because the function is small and pure: the forward cases establish
that it counts correctly, and the hostile cases establish that a malformed history is
summarised rather than refused. A report is a read over records that already happened, so
raising on a record it cannot parse would mean the operator is told nothing about the
thousands it *can*.
"""

from __future__ import annotations

from typing import Any

import pytest

from paynaka.mode import ShadowReport, shadow_report


def decision() -> dict[str, Any]:
    return {"kind": "decision", "mode": "observe"}


def observed(check: str = "envelope.total_exceeded", amount: int = 100_000) -> dict[str, Any]:
    return {"kind": "observed", "check_id": check, "amount": amount, "verdict": "DENY"}


class TestCounting:
    def test_an_empty_history_is_a_zero_report_not_a_failure(self) -> None:
        report = shadow_report([])
        assert report == ShadowReport()
        assert report.rate == 0.0
        assert report.top_check is None

    def test_decisions_and_suppressions_are_counted_separately(self) -> None:
        report = shadow_report([decision(), observed(), decision(), decision()])
        assert report.decisions == 3
        assert report.observed == 1

    def test_money_at_risk_sums_the_suppressed_amounts(self) -> None:
        report = shadow_report([observed(amount=199_900), observed(amount=5_200_000)])
        assert report.money_at_risk == 5_399_900

    def test_the_breakdown_counts_and_totals_per_check(self) -> None:
        report = shadow_report(
            [
                observed("envelope.price_moved", 100_000),
                observed("envelope.price_moved", 250_000),
                observed("policy.max_amount", 700_000),
            ]
        )
        assert report.by_check == {"envelope.price_moved": 2, "policy.max_amount": 1}
        assert report.by_check_amount == {
            "envelope.price_moved": 350_000,
            "policy.max_amount": 700_000,
        }

    def test_the_rate_is_suppressions_over_decisions(self) -> None:
        report = shadow_report([decision(), decision(), decision(), decision(), observed()])
        assert report.rate == pytest.approx(0.25)

    def test_the_top_check_is_the_one_that_would_have_fired_most(self) -> None:
        report = shadow_report([observed("a"), observed("b"), observed("b")])
        assert report.top_check == "b"

    def test_a_tie_resolves_deterministically(self) -> None:
        """An operator comparing two runs must not see the order change for no reason."""
        first = shadow_report([observed("zebra"), observed("alpha")])
        second = shadow_report([observed("alpha"), observed("zebra")])
        assert first.top_check == second.top_check

    def test_the_dict_is_sorted_so_two_runs_diff_cleanly(self) -> None:
        report = shadow_report([observed("zebra"), observed("alpha")])
        assert list(report.to_dict()["by_check"]) == ["alpha", "zebra"]


class TestOnlySuppressionsCount:
    def test_an_enforced_denial_is_not_a_suppression(self) -> None:
        """A refusal that *did* stop something must not inflate the number this feature
        exists to report honestly. Enforce mode writes no ``observed`` record at all."""
        report = shadow_report([{"kind": "decision", "mode": "enforce"}] * 10)
        assert report.observed == 0
        assert report.money_at_risk == 0

    @pytest.mark.parametrize(
        "kind",
        ["executed", "rail.declined", "rail.indeterminate", "circuit.tripped", "anchor", "unknown"],
    )
    def test_no_other_record_kind_is_mistaken_for_a_suppression(self, kind: str) -> None:
        report = shadow_report([{"kind": kind, "check_id": "x", "amount": 999}])
        assert report.observed == 0
        assert report.decisions == 0


class TestAMalformedHistoryIsSummarisedNotRefused:
    @pytest.mark.parametrize(
        "payload",
        [
            {"kind": "observed"},  # no check, no amount
            {"kind": "observed", "check_id": None, "amount": None},
            {"kind": "observed", "check_id": "", "amount": 0},
            {"kind": "observed", "check_id": 42, "amount": "1000"},
            {"kind": "observed", "check_id": ["a"], "amount": {"b": 1}},
        ],
        ids=["bare", "nulls", "empty", "wrong-types", "container-types"],
    )
    def test_an_unparseable_suppression_still_counts_as_one(self, payload: dict[str, Any]) -> None:
        """It happened. Losing the count because a field is the wrong shape would
        understate exactly the number that matters."""
        report = shadow_report([payload])
        assert report.observed == 1
        assert report.money_at_risk == 0
        assert report.by_check == {"unknown": 1}

    @pytest.mark.parametrize("amount", [-1, -5_000_000, 0])
    def test_a_negative_or_zero_amount_contributes_nothing(self, amount: int) -> None:
        """Money at risk must never go down because a record was malformed."""
        report = shadow_report([observed(amount=amount)])
        assert report.observed == 1
        assert report.money_at_risk == 0

    def test_a_boolean_is_not_an_amount(self) -> None:
        """``True`` is an ``int`` in Python, and 1 paisa of "money at risk" from a boolean
        is a number that would take somebody an afternoon to explain."""
        report = shadow_report([{"kind": "observed", "check_id": "c", "amount": True}])
        assert report.money_at_risk == 0

    def test_a_record_with_no_kind_at_all_is_skipped(self) -> None:
        report = shadow_report([{}, {"check_id": "c", "amount": 500}])
        assert report == ShadowReport()

    def test_a_huge_history_does_not_lose_precision(self) -> None:
        """Integer paise, so 100,000 records of ₹52,000 is exact rather than nearly."""
        report = shadow_report([observed(amount=5_200_000) for _ in range(100_000)])
        assert report.money_at_risk == 520_000_000_000
