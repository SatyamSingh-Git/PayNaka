"""Tests for the metrics snapshot and its Prometheus rendering.

An audit chain nobody watches breaks quietly, so these numbers are what makes the
witnessing in `anchor.py` detected rather than merely detectable. Two things get tested
harder than the rest:

* **The counts come from the records.** Every figure is derived on scrape rather than
  accumulated beside the decision, so the test that matters is that a chain and a snapshot
  cannot disagree.
* **A malformed record does not take the endpoint down.** This reads history that already
  happened. Raising on one bad row tells an operator nothing about the thousands of sound
  ones, at exactly the moment they most need a number.
"""

from __future__ import annotations

from typing import Any

import pytest

from paynaka.metrics import Metrics, collect, render_prometheus


def decision(
    verdict: str = "ALLOW", check: str | None = None, replayed: bool = False
) -> dict[str, Any]:
    return {
        "kind": "decision",
        "decision": {"verdict": verdict, "check_id": check, "replayed": replayed},
    }


def executed(amount: int = 199_900) -> dict[str, Any]:
    return {"kind": "executed", "action": "create_order", "result": {"amount": amount}}


class TestCounting:
    def test_an_empty_chain_is_a_zeroed_snapshot(self) -> None:
        assert collect([]) == Metrics()

    def test_verdicts_are_counted_apart(self) -> None:
        snapshot = collect(
            [decision("ALLOW"), decision("DENY"), decision("DENY"), decision("STEP_UP")]
        )
        assert (snapshot.decisions, snapshot.allowed, snapshot.denied, snapshot.stepped_up) == (
            4,
            1,
            2,
            1,
        )

    def test_money_is_summed_from_what_the_rail_confirmed(self) -> None:
        """Never from what was requested. Those differ on a partial capture."""
        snapshot = collect([executed(199_900), executed(50_000)])
        assert snapshot.money_moved == 249_900
        assert snapshot.executed == 2

    def test_a_replay_is_an_allow_and_also_a_replay(self) -> None:
        snapshot = collect([decision("ALLOW", "idempotency.replay", replayed=True)])
        assert snapshot.allowed == 1
        assert snapshot.replayed == 1

    def test_checks_are_attributed(self) -> None:
        snapshot = collect(
            [
                decision("DENY", "envelope.total_exceeded"),
                decision("DENY", "envelope.total_exceeded"),
                decision("DENY", "envelope.destination"),
            ]
        )
        assert snapshot.by_check == {
            "envelope.total_exceeded": 2,
            "envelope.destination": 1,
        }

    @pytest.mark.parametrize(
        ("kind", "attribute"),
        [
            ("circuit.tripped", "breaker_trips"),
            ("escalation.opened", "escalations_opened"),
            ("observed", "observed_suppressions"),
            ("rail.declined", "rail_declined"),
            ("rail.indeterminate", "rail_indeterminate"),
        ],
    )
    def test_each_event_kind_lands_on_its_own_counter(self, kind: str, attribute: str) -> None:
        snapshot = collect([{"kind": kind}] * 3)
        assert getattr(snapshot, attribute) == 3

    def test_escalation_outcomes_are_told_apart(self) -> None:
        snapshot = collect(
            [
                {"kind": "escalation.decided", "outcome": "approved"},
                {"kind": "escalation.decided", "outcome": "denied"},
                {"kind": "escalation.decided", "outcome": "denied"},
            ]
        )
        assert snapshot.escalations_approved == 1
        assert snapshot.escalations_denied == 2


class TestAMalformedRecordDoesNotTakeTheEndpointDown:
    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"kind": None},
            {"kind": "decision"},
            {"kind": "decision", "decision": None},
            {"kind": "decision", "decision": "ALLOW"},
            {"kind": "decision", "decision": {"verdict": 42}},
            {"kind": "decision", "decision": {"check_id": ["a"]}},
            {"kind": "executed"},
            {"kind": "executed", "result": None},
            {"kind": "executed", "result": {"amount": "1999"}},
            {"kind": "escalation.decided"},
            {"kind": "unknown-to-this-version"},
        ],
        ids=repr,
    )
    def test_it_is_counted_or_skipped_but_never_raises(self, payload: dict[str, Any]) -> None:
        collect([payload])  # the assertion is that this returns

    @pytest.mark.parametrize("amount", [-1, -199_900, True, False, "500", None, 1.5])
    def test_nothing_but_a_positive_int_adds_to_money_moved(self, amount: object) -> None:
        """``True`` is an ``int`` in Python. One paisa of "money moved" arriving from a
        boolean is a number somebody would spend an afternoon explaining."""
        snapshot = collect([{"kind": "executed", "result": {"amount": amount}}])
        assert snapshot.money_moved == 0
        assert snapshot.executed == 1

    def test_a_decision_with_no_verdict_still_counts_as_a_decision(self) -> None:
        """It happened. Losing the denominator because a field is missing is worse than
        losing the breakdown."""
        snapshot = collect([{"kind": "decision", "decision": {}}])
        assert snapshot.decisions == 1
        assert snapshot.allowed == snapshot.denied == 0


class TestTheExposition:
    def test_the_alarm_metric_is_present_and_correct_both_ways(self) -> None:
        intact = render_prometheus(Metrics(), chain_records=3, chain_intact=True, mode="enforce")
        broken = render_prometheus(Metrics(), chain_records=3, chain_intact=False, mode="enforce")
        assert "paynaka_audit_chain_intact 1" in intact
        assert "paynaka_audit_chain_intact 0" in broken

    def test_observing_is_visible_as_a_series(self) -> None:
        """A dashboard must be able to show that nothing is being stopped."""
        observing = render_prometheus(Metrics(), chain_records=0, chain_intact=True, mode="observe")
        assert "paynaka_enforcing 0" in observing

    def test_every_series_is_helped_and_typed(self) -> None:
        text = render_prometheus(
            Metrics(by_check={"envelope.destination": 1}),
            chain_records=1,
            chain_intact=True,
            mode="enforce",
        )
        names = {line.split()[2] for line in text.splitlines() if line.startswith("# HELP")}
        typed = {line.split()[2] for line in text.splitlines() if line.startswith("# TYPE")}
        assert names == typed
        assert names

    def test_money_is_exposed_as_integer_paise(self) -> None:
        """Never rupees, and never scientific notation -- a scraper reading 1.9999e+05 as
        an amount of money is a bug with a currency symbol on it."""
        text = render_prometheus(
            Metrics(money_moved=520_000_000_000),
            chain_records=1,
            chain_intact=True,
            mode="enforce",
        )
        assert "paynaka_money_moved_paise 520000000000" in text

    def test_check_ids_become_labels(self) -> None:
        text = render_prometheus(
            Metrics(by_check={"envelope.price_moved": 7}),
            chain_records=1,
            chain_intact=True,
            mode="enforce",
        )
        assert 'paynaka_check_total{check_id="envelope.price_moved"} 7' in text

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ('has"quote', 'has\\"quote'),
            ("has\\backslash", "has\\\\backslash"),
            ("has\nnewline", "has\\nnewline"),
        ],
        ids=["quote", "backslash", "newline"],
    )
    def test_a_label_value_cannot_break_out_of_its_quotes(self, raw: str, expected: str) -> None:
        """Check ids are ours and contain none of these, which is why this is here: the day
        one does, an unescaped label silently corrupts every series after it."""
        text = render_prometheus(
            Metrics(by_check={raw: 1}), chain_records=1, chain_intact=True, mode="enforce"
        )
        assert f'check_id="{expected}"' in text

    def test_no_check_labels_when_there_are_no_checks(self) -> None:
        text = render_prometheus(Metrics(), chain_records=0, chain_intact=True, mode="enforce")
        assert "paynaka_check_total" not in text

    def test_the_body_ends_with_a_newline(self) -> None:
        """The exposition format requires it, and a scraper that is strict about it will
        reject the whole payload rather than the last line."""
        text = render_prometheus(Metrics(), chain_records=0, chain_intact=True, mode="enforce")
        assert text.endswith("\n")
        assert not text.endswith("\n\n")
