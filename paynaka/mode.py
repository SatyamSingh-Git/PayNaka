"""Enforce, or observe. How a checkpoint gets adopted rather than admired.

Nobody puts an enforcing gate in front of live payment traffic on the strength of a
README. The way payment infrastructure actually gets adopted is: run it beside production,
changing nothing, and read what it *would* have done for a week. Then decide.

``observe`` is that mode. Every check runs, every decision is computed and audited, and a
refusal does not stop anything. What the operator gets at the end of the week is not an
argument, it is a list: these are the calls that would have been blocked, this is how much
money was involved, and here is the check that would have caught each one.

Two things make this honest rather than a hole:

**The mode is on every record.** It is stamped on each decision the chain carries, so it
is never possible to read the audit log later and believe the gate was enforcing when it
was not. The failure mode this design fears most is an operator who thinks they are
protected and is not, so the mode is loud everywhere -- health endpoint, audit record,
console -- rather than a line in a config file nobody reads twice.

**Observe mode withholds authority judgments, not money-correctness.** A duplicate request
is resolved by idempotency before the mode is consulted at all, so an observing checkpoint
never issues a payment it has already made. Suppressing an authority check means declining
to stop what would have happened anyway; suppressing idempotency would mean *causing* a
double charge. The first is observation, the second is damage.

An unknown value is a startup failure. ``PAYNAKA_MODE=enfroce`` must not quietly become
the mode that enforces nothing.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

__all__ = ["MODE_ENV_VAR", "Mode", "ShadowReport", "shadow_report"]

MODE_ENV_VAR: Final[str] = "PAYNAKA_MODE"


class Mode(StrEnum):
    """What the checkpoint does with a refusal."""

    #: Decide and act on the decision. The default, because a checkpoint that does not
    #: enforce unless told to is a checkpoint somebody forgot to switch on.
    ENFORCE = "enforce"

    #: Decide, record, and let the request through regardless. For running beside live
    #: traffic to find out what enforcement would cost before enforcing anything.
    OBSERVE = "observe"

    @property
    def enforcing(self) -> bool:
        return self is Mode.ENFORCE

    @classmethod
    def from_env(cls, value: str | None = None) -> Mode:
        """Read the mode from the environment, defaulting to enforcement.

        A typo raises rather than falling back. Falling back to ``enforce`` would be the
        safe direction and still wrong: the operator asked for something, got something
        else, and nothing said so. Falling back to ``observe`` would be catastrophic.
        """
        raw = (value if value is not None else os.environ.get(MODE_ENV_VAR, "")).strip().lower()
        if not raw:
            return cls.ENFORCE
        try:
            return cls(raw)
        except ValueError:
            allowed = ", ".join(sorted(m.value for m in cls))
            raise ValueError(
                f"{MODE_ENV_VAR}={raw!r} is not a mode. Expected one of: {allowed}. "
                f"Refusing to guess which one was meant -- one of them enforces nothing."
            ) from None


@dataclass(frozen=True, slots=True)
class ShadowReport:
    """What a week of observing found. The deliverable that ends the adoption argument.

    Counted from the audit chain rather than accumulated in memory, so the report is a
    consequence of the records rather than a second bookkeeping path that can disagree
    with them. If the chain verifies, the report is what the chain says.
    """

    #: Decisions the checkpoint made, in either verdict.
    decisions: int = 0
    #: Refusals that were computed and not acted on.
    observed: int = 0
    #: Paise that the observed refusals would have stopped. Not "money saved" -- money
    #: that would not have moved, which is a different and more defensible sentence: some
    #: of it belongs to purchases the shopper would have been happy with.
    money_at_risk: int = 0
    #: How many times each check would have fired, and for how much.
    by_check: dict[str, int] = field(default_factory=dict)
    by_check_amount: dict[str, int] = field(default_factory=dict)

    @property
    def rate(self) -> float:
        """Share of decisions that would have been stopped. Zero when nothing ran."""
        return self.observed / self.decisions if self.decisions else 0.0

    @property
    def top_check(self) -> str | None:
        """The check that would have fired most, or ``None``.

        Ties resolve on the check id so the report is deterministic -- an operator
        comparing two runs should not see the order change for no reason.
        """
        if not self.by_check:
            return None
        return max(sorted(self.by_check), key=lambda check: self.by_check[check])

    def to_dict(self) -> dict[str, Any]:
        return {
            "decisions": self.decisions,
            "observed": self.observed,
            "money_at_risk": self.money_at_risk,
            "rate": self.rate,
            "top_check": self.top_check,
            "by_check": dict(sorted(self.by_check.items())),
            "by_check_amount": dict(sorted(self.by_check_amount.items())),
        }


def shadow_report(payloads: Iterable[Mapping[str, Any]]) -> ShadowReport:
    """Summarise audit payloads into what enforcement would have changed.

    Takes payloads rather than an :class:`~paynaka.audit.AuditChain` so this stays a pure
    function over records: it is testable without a database, and it cannot accidentally
    become a second source of truth about what happened.

    Only ``observed`` records count toward the suppression totals. A decision record
    denied in enforce mode is a refusal that *did* stop something, and folding it in here
    would inflate the number that the whole feature exists to report honestly.
    """
    decisions = 0
    observed = 0
    at_risk = 0
    by_check: dict[str, int] = {}
    by_amount: dict[str, int] = {}

    for payload in payloads:
        kind = payload.get("kind")
        if kind == "decision":
            decisions += 1
            continue
        if kind != "observed":
            continue

        check = payload.get("check_id")
        check = check if isinstance(check, str) and check else "unknown"
        # An absent or non-integer amount contributes nothing rather than raising: a
        # report is a read over history, and history it cannot parse is not a reason to
        # refuse to tell the operator about the records it can.
        raw = payload.get("amount")
        amount = raw if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0 else 0

        observed += 1
        at_risk += amount
        by_check[check] = by_check.get(check, 0) + 1
        by_amount[check] = by_amount.get(check, 0) + amount

    return ShadowReport(
        decisions=decisions,
        observed=observed,
        money_at_risk=at_risk,
        by_check=by_check,
        by_check_amount=by_amount,
    )
