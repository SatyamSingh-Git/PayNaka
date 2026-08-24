"""Hostile input to the metrics endpoint, and to the exposition it renders.

Two distinct concerns, and the second is the one that surprises people:

**A malformed record must not take the endpoint down.** Metrics are read during incidents.
An endpoint that raises on one bad row is an endpoint that goes dark precisely when an
operator is trying to find out what is happening.

**A value must not break the exposition format.** A scrape is parsed by a machine that will
not tell anybody it misread something. An unescaped label or a number in scientific
notation corrupts a series silently, and the corrupted one here would be money.
"""

from __future__ import annotations

from typing import Any

import pytest

from paynaka.metrics import Metrics, collect, render_prometheus

pytestmark = pytest.mark.adversarial


class TestAMalformedRecordDoesNotTakeTheEndpointDown:
    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"kind": None},
            {"kind": 7},
            {"kind": "decision"},
            {"kind": "decision", "decision": None},
            {"kind": "decision", "decision": "ALLOW"},
            {"kind": "decision", "decision": []},
            {"kind": "decision", "decision": {"verdict": 42}},
            {"kind": "decision", "decision": {"verdict": None}},
            {"kind": "decision", "decision": {"check_id": ["a"]}},
            {"kind": "decision", "decision": {"replayed": "yes"}},
            {"kind": "executed"},
            {"kind": "executed", "result": None},
            {"kind": "executed", "result": []},
            {"kind": "executed", "result": {"amount": "1999"}},
            {"kind": "escalation.decided"},
            {"kind": "escalation.decided", "outcome": None},
            {"kind": "unknown-to-this-version"},
        ],
        ids=repr,
    )
    def test_it_is_counted_or_skipped_but_never_raises(self, payload: dict[str, Any]) -> None:
        collect([payload])  # the assertion is that this returns at all

    @pytest.mark.parametrize("amount", [-1, -199_900, True, False, "500", None, 1.5, [1], {"a": 1}])
    def test_nothing_but_a_positive_int_adds_to_money_moved(self, amount: object) -> None:
        """``True`` is an ``int``. One paisa of "money moved" arriving from a boolean is a
        number somebody would spend an afternoon explaining."""
        snapshot = collect([{"kind": "executed", "result": {"amount": amount}}])
        assert snapshot.money_moved == 0
        assert snapshot.executed == 1

    def test_a_negative_amount_cannot_cancel_a_real_one(self) -> None:
        """Money moved must never go down because a record was malformed."""
        snapshot = collect(
            [
                {"kind": "executed", "result": {"amount": -500_000}},
                {"kind": "executed", "result": {"amount": 199_900}},
            ]
        )
        assert snapshot.money_moved == 199_900

    def test_a_decision_with_no_verdict_still_counts_as_a_decision(self) -> None:
        """Losing the denominator because a field is missing is worse than losing the
        breakdown: every rate computed from it would be wrong in the flattering direction."""
        snapshot = collect([{"kind": "decision", "decision": {}}])
        assert snapshot.decisions == 1
        assert snapshot.allowed == snapshot.denied == snapshot.stepped_up == 0

    def test_an_unknown_verdict_is_counted_in_no_bucket_but_still_a_decision(self) -> None:
        snapshot = collect([{"kind": "decision", "decision": {"verdict": "MAYBE"}}])
        assert snapshot.decisions == 1
        assert snapshot.allowed + snapshot.denied + snapshot.stepped_up == 0


class TestTheExpositionCannotBeCorrupted:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ('has"quote', 'has\\"quote'),
            ("has\\backslash", "has\\\\backslash"),
            ("has\nnewline", "has\\nnewline"),
            ('", evil="1', '\\", evil=\\"1'),
            ('"} 999\nevil_metric{a="b', '\\"} 999\\nevil_metric{a=\\"b'),
        ],
        ids=["quote", "backslash", "newline", "label-injection", "series-injection"],
    )
    def test_a_label_value_cannot_break_out_of_its_quotes(self, raw: str, expected: str) -> None:
        """Check ids are ours and contain none of these, which is exactly why this is here.
        The day one does, an unescaped label injects a series and silently corrupts every
        line after it in the scrape."""
        text = render_prometheus(
            Metrics(by_check={raw: 1}), chain_records=1, chain_intact=True, mode="enforce"
        )
        assert f'check_id="{expected}"' in text
        # The payload may appear *inside* the quoted label; that is harmless. What must not
        # happen is a new line, because a line is what defines a series. Passed through
        # unescaped, "evil_metric{a="b"} 999" would become a metric of its own.
        assert not any(line.startswith("evil_metric") for line in text.splitlines())
        assert all(line.startswith(("#", "paynaka_")) for line in text.splitlines() if line.strip())

    @pytest.mark.parametrize(
        "amount",
        [0, 1, 199_900, 520_000_000_000, 10**15, 2**53, 2**53 + 1],
    )
    def test_money_never_renders_in_scientific_notation(self, amount: int) -> None:
        """Regression. Rendering with ``g`` carries six significant digits, so ₹52 lakh of
        money moved exposed itself as ``5.2e+11`` -- a scraper reading that as an amount of
        money is a bug with a currency symbol on it."""
        text = render_prometheus(
            Metrics(money_moved=amount), chain_records=1, chain_intact=True, mode="enforce"
        )
        assert f"paynaka_money_moved_paise {amount}" in text
        assert "e+" not in text

    def test_a_broken_chain_is_reported_as_broken(self) -> None:
        """The one series worth an alarm. If this ever renders 1 while the chain is broken,
        the alarm is a green tile on a wall in front of an incident."""
        text = render_prometheus(Metrics(), chain_records=9, chain_intact=False, mode="enforce")
        assert "paynaka_audit_chain_intact 0" in text

    @pytest.mark.parametrize("mode", ["observe", "OBSERVE", "", "nonsense"])
    def test_anything_that_is_not_enforce_reads_as_not_enforcing(self, mode: str) -> None:
        """Fail closed in the reporting direction too: a mode string this renderer does not
        recognise must not display as "enforcing"."""
        text = render_prometheus(Metrics(), chain_records=0, chain_intact=True, mode=mode)
        assert "paynaka_enforcing 0" in text

    def test_an_empty_snapshot_still_renders_a_valid_document(self) -> None:
        text = render_prometheus(Metrics(), chain_records=0, chain_intact=True, mode="enforce")
        assert text.endswith("\n")
        assert not text.endswith("\n\n")
        assert "# TYPE" in text
