"""What to put on a dashboard, and the one alarm that matters.

An audit chain nobody watches breaks quietly. That is the failure mode the witnessing tiers
in :mod:`paynaka.anchor` exist to make detectable -- and detectable is not detected. This
module is the other half: the numbers an operator can alert on.

**Derived, never accumulated.** Every figure here is computed from the audit records and the
state store on demand. The tempting alternative is a counter incremented next to each
decision, which is faster and introduces a second source of truth that can disagree with
the chain. When those two disagree the counter is wrong and the chain is right, so the
counter should not exist.

**Prometheus text format, by hand.** Four lines of string formatting against a stable, dull
specification, versus a dependency in the money-path process. The exposition format is not
where this project should be spending its trust.

The metric worth an alarm rather than a graph is ``paynaka_audit_chain_intact``. Zero means
the chain no longer verifies against itself, which is either corruption or somebody editing
history, and both are incidents. Every other series here answers "how is it going"; that one
answers "is the record still true".
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = ["Metrics", "collect", "render_prometheus"]


@dataclass(frozen=True, slots=True)
class Metrics:
    """A snapshot. Counts of things that happened, not gauges of things that are."""

    decisions: int = 0
    allowed: int = 0
    denied: int = 0
    stepped_up: int = 0
    replayed: int = 0
    executed: int = 0
    money_moved: int = 0
    by_check: dict[str, int] = field(default_factory=dict)
    breaker_trips: int = 0
    escalations_opened: int = 0
    escalations_approved: int = 0
    escalations_denied: int = 0
    observed_suppressions: int = 0
    rail_declined: int = 0
    rail_indeterminate: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "decisions": self.decisions,
            "allowed": self.allowed,
            "denied": self.denied,
            "stepped_up": self.stepped_up,
            "replayed": self.replayed,
            "executed": self.executed,
            "money_moved": self.money_moved,
            "by_check": dict(sorted(self.by_check.items())),
            "breaker_trips": self.breaker_trips,
            "escalations_opened": self.escalations_opened,
            "escalations_approved": self.escalations_approved,
            "escalations_denied": self.escalations_denied,
            "observed_suppressions": self.observed_suppressions,
            "rail_declined": self.rail_declined,
            "rail_indeterminate": self.rail_indeterminate,
        }


def collect(payloads: Iterable[Mapping[str, Any]]) -> Metrics:
    """Count the audit records into a snapshot.

    Tolerant of records it does not recognise and of fields with the wrong shape, for the
    same reason the shadow report is: this reads history that already happened. A metrics
    endpoint that raises on one malformed record tells the operator nothing about the
    thousands of sound ones, and it does so at exactly the moment they most need a number.
    """
    counts = {
        "decisions": 0,
        "allowed": 0,
        "denied": 0,
        "stepped_up": 0,
        "replayed": 0,
        "executed": 0,
        "breaker_trips": 0,
        "escalations_opened": 0,
        "escalations_approved": 0,
        "escalations_denied": 0,
        "observed_suppressions": 0,
        "rail_declined": 0,
        "rail_indeterminate": 0,
    }
    money = 0
    by_check: dict[str, int] = {}

    for payload in payloads:
        kind = payload.get("kind")

        if kind == "decision":
            counts["decisions"] += 1
            decision = payload.get("decision")
            decision = decision if isinstance(decision, Mapping) else {}
            verdict = str(decision.get("verdict", ""))
            if verdict == "ALLOW":
                counts["allowed"] += 1
            elif verdict == "DENY":
                counts["denied"] += 1
            elif verdict == "STEP_UP":
                counts["stepped_up"] += 1
            if decision.get("replayed") is True:
                counts["replayed"] += 1
            check = decision.get("check_id")
            if isinstance(check, str) and check:
                by_check[check] = by_check.get(check, 0) + 1

        elif kind == "executed":
            counts["executed"] += 1
            money += _amount_of(payload.get("result"))

        elif kind == "circuit.tripped":
            counts["breaker_trips"] += 1
        elif kind == "escalation.opened":
            counts["escalations_opened"] += 1
        elif kind == "escalation.decided":
            outcome = str(payload.get("outcome", ""))
            if outcome == "approved":
                counts["escalations_approved"] += 1
            elif outcome == "denied":
                counts["escalations_denied"] += 1
        elif kind == "observed":
            counts["observed_suppressions"] += 1
        elif kind == "rail.declined":
            counts["rail_declined"] += 1
        elif kind == "rail.indeterminate":
            counts["rail_indeterminate"] += 1

    return Metrics(money_moved=money, by_check=by_check, **counts)


def _amount_of(result: object) -> int:
    """Paise from an execution record, or zero.

    Booleans are excluded explicitly. ``True`` is an ``int`` in Python, and one paisa of
    "money moved" arriving from a boolean is a number somebody would spend an afternoon
    explaining.
    """
    if not isinstance(result, Mapping):
        return 0
    amount = result.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
        return 0
    return amount


def render_prometheus(
    metrics: Metrics, *, chain_records: int, chain_intact: bool, mode: str
) -> str:
    """Render the exposition format.

    ``chain_intact`` is passed rather than derived because verifying the chain is a full
    recompute, and whether a scrape should pay for that is the caller's decision, not this
    function's.
    """
    lines: list[str] = []

    def series(name: str, kind: str, help_text: str, value: float, labels: str = "") -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")
        lines.append(f"{name}{labels} {_number(value)}")

    series(
        "paynaka_audit_chain_intact",
        "gauge",
        "1 if the audit chain verifies against itself, 0 if it does not. Alarm on 0: "
        "that is corruption or somebody editing history, and both are incidents.",
        1 if chain_intact else 0,
    )
    series(
        "paynaka_audit_records",
        "gauge",
        "Records in the audit chain.",
        chain_records,
    )
    series(
        "paynaka_enforcing",
        "gauge",
        "1 when the checkpoint acts on its decisions, 0 when it is only observing. "
        "A 0 here means nothing is being stopped.",
        1 if mode == "enforce" else 0,
    )
    series("paynaka_decisions_total", "counter", "Money decisions made.", metrics.decisions)
    series("paynaka_allowed_total", "counter", "Decisions that allowed.", metrics.allowed)
    series("paynaka_denied_total", "counter", "Decisions that refused.", metrics.denied)
    series(
        "paynaka_step_up_total",
        "counter",
        "Decisions that asked a human.",
        metrics.stepped_up,
    )
    series(
        "paynaka_replayed_total",
        "counter",
        "Duplicate requests answered from the original result.",
        metrics.replayed,
    )
    series("paynaka_executed_total", "counter", "Requests that reached a rail.", metrics.executed)
    series(
        "paynaka_money_moved_paise",
        "counter",
        "Paise confirmed moved by a rail. Integer paise, never rupees.",
        metrics.money_moved,
    )
    series(
        "paynaka_circuit_trips_total",
        "counter",
        "Times a session or subject had its authority withdrawn.",
        metrics.breaker_trips,
    )
    series(
        "paynaka_escalations_opened_total",
        "counter",
        "Step-ups put in front of a human.",
        metrics.escalations_opened,
    )
    series(
        "paynaka_escalations_approved_total",
        "counter",
        "Step-ups a human approved.",
        metrics.escalations_approved,
    )
    series(
        "paynaka_escalations_denied_total",
        "counter",
        "Step-ups a human refused.",
        metrics.escalations_denied,
    )
    series(
        "paynaka_observed_suppressions_total",
        "counter",
        "Refusals computed and not acted on because the mode is observe.",
        metrics.observed_suppressions,
    )
    series(
        "paynaka_rail_declined_total",
        "counter",
        "Rail refusals: definitive, the money did not move.",
        metrics.rail_declined,
    )
    series(
        "paynaka_rail_indeterminate_total",
        "counter",
        "Rail timeouts: outcome unknown, awaiting reconciliation. Not a failure.",
        metrics.rail_indeterminate,
    )

    if metrics.by_check:
        lines.append("# HELP paynaka_check_total Decisions attributed to each check.")
        lines.append("# TYPE paynaka_check_total counter")
        for check, count in sorted(metrics.by_check.items()):
            lines.append(f'paynaka_check_total{{check_id="{_escape(check)}"}} {_number(count)}')

    return "\n".join(lines) + "\n"


def _number(value: float) -> str:
    """Render a value without reaching for scientific notation.

    ``format(value, "g")`` carries six significant digits, so Rs 52 lakh of money moved
    exposes itself as ``5.2e+11`` -- a scraper reading that as an amount of money is a bug
    with a currency symbol on it. Integer paise render as integers, which is what every
    series here actually holds.
    """
    if float(value).is_integer():
        return str(int(value))
    return repr(float(value))


def _escape(value: str) -> str:
    """Escape a label value per the exposition format.

    Check ids are ours and contain none of these, which is exactly why this is here: the
    day one contains a quote is the day an unescaped label silently corrupts every series
    after it in the scrape.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
