"""Hostile input to the shadow-mode report.

A report is a read over records that already happened, so the failure mode to design
against is not a wrong number, it is *no number*: raising on one malformed row would tell
an operator nothing about the thousands of sound ones, at exactly the moment they are
deciding whether to switch enforcement on.

The second failure mode is a number that flatters. Every case below checks that a broken
record cannot make ``observed`` or ``money_at_risk`` smaller than the truth, because those
are the figures the whole feature exists to report honestly.
"""

from __future__ import annotations

from typing import Any

import pytest

from paynaka.mode import ShadowReport, shadow_report

pytestmark = pytest.mark.adversarial


def observed(check: str = "envelope.total_exceeded", amount: int = 100_000) -> dict[str, Any]:
    return {"kind": "observed", "check_id": check, "amount": amount, "verdict": "DENY"}


class TestAMalformedHistoryIsSummarisedNotRefused:
    @pytest.mark.parametrize(
        "payload",
        [
            {"kind": "observed"},
            {"kind": "observed", "check_id": None, "amount": None},
            {"kind": "observed", "check_id": "", "amount": 0},
            {"kind": "observed", "check_id": 42, "amount": "1000"},
            {"kind": "observed", "check_id": ["a"], "amount": {"b": 1}},
            {"kind": "observed", "check_id": {"a": 1}, "amount": [5]},
        ],
        ids=["bare", "nulls", "empty", "wrong-types", "container-types", "swapped-containers"],
    )
    def test_an_unparseable_suppression_still_counts_as_one(self, payload: dict[str, Any]) -> None:
        """It happened. Losing the count because a field is the wrong shape understates
        exactly the number that matters."""
        report = shadow_report([payload])
        assert report.observed == 1
        assert report.money_at_risk == 0
        assert report.by_check == {"unknown": 1}

    @pytest.mark.parametrize("amount", [-1, -5_000_000, 0, -(2**63)])
    def test_a_negative_or_zero_amount_contributes_nothing(self, amount: int) -> None:
        """Money at risk must never go *down* because a record was malformed. A negative
        amount summing into the total would let one bad row cancel real ones out."""
        report = shadow_report([observed(amount=amount), observed(amount=50_000)])
        assert report.observed == 2
        assert report.money_at_risk == 50_000

    def test_a_boolean_is_not_an_amount(self) -> None:
        """``True`` is an ``int`` in Python, and one paisa of "money at risk" arriving from
        a boolean is a number somebody would spend an afternoon explaining."""
        assert (
            shadow_report([{"kind": "observed", "check_id": "c", "amount": True}]).money_at_risk
            == 0
        )

    @pytest.mark.parametrize("payload", [{}, {"kind": None}, {"kind": ""}, {"kind": 7}])
    def test_a_record_with_no_usable_kind_is_skipped(self, payload: dict[str, Any]) -> None:
        assert shadow_report([payload]) == ShadowReport()

    def test_an_enforced_denial_cannot_inflate_the_suppression_count(self) -> None:
        """The number this feature exists to report honestly. A refusal that *did* stop
        something is not a refusal that was let through."""
        report = shadow_report([{"kind": "decision", "mode": "enforce"}] * 50)
        assert report.observed == 0
        assert report.money_at_risk == 0

    @pytest.mark.parametrize(
        "kind",
        ["executed", "rail.declined", "rail.indeterminate", "circuit.tripped", "escalation.opened"],
    )
    def test_no_other_record_kind_is_mistaken_for_a_suppression(self, kind: str) -> None:
        report = shadow_report([{"kind": kind, "check_id": "x", "amount": 999_999}])
        assert report.observed == 0
        assert report.money_at_risk == 0

    def test_a_huge_history_stays_exact(self) -> None:
        """Integer paise, so 100,000 records of ₹52,000 is exact rather than nearly. A
        float would have lost the paise somewhere around the fourth crore."""
        report = shadow_report([observed(amount=5_200_000) for _ in range(100_000)])
        assert report.money_at_risk == 520_000_000_000

    def test_the_rate_cannot_divide_by_zero(self) -> None:
        """Suppressions with no decisions recorded is a nonsensical history, and it must
        still render rather than raising inside a report."""
        report = shadow_report([observed()])
        assert report.decisions == 0
        assert report.rate == 0.0
