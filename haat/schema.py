"""HAAT's data model: what an attack case is, and what a run of one produced.

Cases are static YAML fixtures, not a generator. That distinction is deliberate and it is
the line between a benchmark and a weapon: a fixture describes one payload aimed at one
field of one bundled fictitious shop, is diffable in review, and cannot be pointed at
anything else. There is no target parameter because there is nowhere else to point it.

The outcome that matters is ``money_moved``, not the verdict. A gate that says DENY while
money leaves the account has not defended anything, and a benchmark scored on verdicts
would not notice.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

import yaml

__all__ = [
    "FAMILIES",
    "VECTORS",
    "AttackCase",
    "BenignCase",
    "CaseError",
    "Corpus",
    "Family",
    "RunResult",
    "Vector",
    "load_corpus",
]


class CaseError(Exception):
    """A fixture file is malformed. Fail loudly: a silently dropped case inflates a score."""


class Family(StrEnum):
    """The eight attack families. Two are sealed until after the freeze."""

    LINE_ITEM_APPEND = "line_item_append"
    QUANTITY_INFLATION = "quantity_inflation"
    DESTINATION_SWAP = "destination_swap"
    REFUND_WITHOUT_RETURN = "refund_without_return"
    CURRENCY_CONFUSION = "currency_confusion"
    REPLAY_DOUBLE_CHARGE = "replay_double_charge"
    OBFUSCATED_PAYLOAD = "obfuscated_payload"  # sealed
    TOOL_CALL_SMUGGLING = "tool_call_smuggling"  # sealed


#: Families held out of development entirely. First contact is after v1.0-freeze.
#:
#: The point is generalisation. If PayNaka only stops what it was built against it is a
#: lookup table; if the structural checks catch families they never saw -- because taking
#: money still requires violating the mandate -- that is a real result.
SEALED: Final[frozenset[Family]] = frozenset(
    {Family.OBFUSCATED_PAYLOAD, Family.TOOL_CALL_SMUGGLING}
)

FAMILIES: Final[tuple[Family, ...]] = tuple(Family)


class Vector(StrEnum):
    """Which attacker-controlled field the payload lands in."""

    REVIEW = "review"
    DESCRIPTION = "description"
    SELLER_NOTE = "seller_note"
    IMAGE_ALT = "image_alt"
    TITLE = "title"


VECTORS: Final[tuple[Vector, ...]] = tuple(Vector)


@dataclass(frozen=True, slots=True)
class AttackCase:
    """One payload, one field, one intended money outcome."""

    case_id: str
    family: Family
    vector: Vector
    target_sku: str
    payload: str
    intent: str
    expected_violation: str
    severity: str = "high"
    notes: str = ""

    @property
    def sealed(self) -> bool:
        return self.family in SEALED

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "family": str(self.family),
            "vector": str(self.vector),
            "target_sku": self.target_sku,
            "payload": self.payload,
            "intent": self.intent,
            "expected_violation": self.expected_violation,
            "severity": self.severity,
        }


@dataclass(frozen=True, slots=True)
class BenignCase:
    """A legitimate but unusual transaction.

    This half of the corpus is what makes the false-positive number mean anything. Without
    it, a gate that blocks everything scores a perfect 100% and nobody notices until a
    merchant's real customers cannot check out.
    """

    case_id: str
    instruction: str
    mandate: dict[str, Any]
    expect_allowed: bool = True
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "instruction": self.instruction,
            "expect_allowed": self.expect_allowed,
            "notes": self.notes,
        }


@dataclass(frozen=True, slots=True)
class RunResult:
    """What one case did under one defence."""

    case_id: str
    defence: str
    family: str
    money_moved: int
    authorised: int
    attack_succeeded: bool
    model: str = ""
    served_by: str | None = None
    blocked_by: str | None = None
    latency_ms: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    turns: int = 0
    refused: bool = False
    error: str | None = None

    @property
    def overspent(self) -> int:
        return max(0, self.money_moved - self.authorised)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "defence": self.defence,
            "family": self.family,
            "model": self.model,
            "served_by": self.served_by,
            "money_moved": self.money_moved,
            "authorised": self.authorised,
            "overspent": self.overspent,
            "attack_succeeded": self.attack_succeeded,
            "blocked_by": self.blocked_by,
            "latency_ms": round(self.latency_ms, 2),
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "turns": self.turns,
            "refused": self.refused,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class Corpus:
    attacks: tuple[AttackCase, ...] = ()
    benign: tuple[BenignCase, ...] = ()

    @property
    def visible(self) -> tuple[AttackCase, ...]:
        return tuple(c for c in self.attacks if not c.sealed)

    @property
    def sealed(self) -> tuple[AttackCase, ...]:
        return tuple(c for c in self.attacks if c.sealed)

    def by_family(self) -> dict[str, list[AttackCase]]:
        grouped: dict[str, list[AttackCase]] = {}
        for case in self.attacks:
            grouped.setdefault(str(case.family), []).append(case)
        return grouped

    def __len__(self) -> int:
        return len(self.attacks) + len(self.benign)


# ---------------------------------------------------------------- loading
_ATTACK_KEYS = {
    "case_id",
    "family",
    "vector",
    "target_sku",
    "payload",
    "intent",
    "expected_violation",
    "severity",
    "notes",
}
_BENIGN_KEYS = {"case_id", "instruction", "mandate", "expect_allowed", "notes"}


def load_corpus(root: str | Path = "haat") -> Corpus:
    """Load every fixture under ``root``. Duplicated ids are an error, not a warning."""
    base = Path(root)
    attacks = [
        _attack(raw, path)
        for path in sorted((base / "attacks").glob("*.yaml"))
        for raw in _documents(path)
    ]
    attacks += [
        _attack(raw, path)
        for path in sorted((base / "sealed").glob("*.yaml"))
        for raw in _documents(path)
    ]
    benign = [
        _benign(raw, path)
        for path in sorted((base / "benign").glob("*.yaml"))
        for raw in _documents(path)
    ]

    seen: set[str] = set()
    every: list[AttackCase | BenignCase] = [*attacks, *benign]
    for case in every:
        if case.case_id in seen:
            raise CaseError(f"duplicate case id: {case.case_id}")
        seen.add(case.case_id)

    return Corpus(attacks=tuple(attacks), benign=tuple(benign))


def _documents(path: Path) -> list[dict[str, Any]]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CaseError(f"{path.name} is not valid YAML: {exc}") from exc

    if loaded is None:
        return []
    if not isinstance(loaded, list):
        raise CaseError(f"{path.name} must contain a list of cases")
    return loaded


def _attack(raw: object, path: Path) -> AttackCase:
    if not isinstance(raw, dict):
        raise CaseError(f"{path.name}: each case must be a mapping")

    unknown = set(raw) - _ATTACK_KEYS
    if unknown:
        raise CaseError(f"{path.name}: unknown key(s) {sorted(unknown)}")

    missing = {"case_id", "family", "vector", "target_sku", "payload", "expected_violation"} - set(
        raw
    )
    if missing:
        raise CaseError(f"{path.name}: missing key(s) {sorted(missing)}")

    try:
        family = Family(raw["family"])
        vector = Vector(raw["vector"])
    except ValueError as exc:
        raise CaseError(f"{path.name}: {exc}") from exc

    return AttackCase(
        case_id=str(raw["case_id"]),
        family=family,
        vector=vector,
        target_sku=str(raw["target_sku"]),
        payload=str(raw["payload"]),
        intent=str(raw.get("intent", "")),
        expected_violation=str(raw["expected_violation"]),
        severity=str(raw.get("severity", "high")),
        notes=str(raw.get("notes", "")),
    )


def _benign(raw: object, path: Path) -> BenignCase:
    if not isinstance(raw, dict):
        raise CaseError(f"{path.name}: each case must be a mapping")

    unknown = set(raw) - _BENIGN_KEYS
    if unknown:
        raise CaseError(f"{path.name}: unknown key(s) {sorted(unknown)}")

    missing = {"case_id", "instruction", "mandate"} - set(raw)
    if missing:
        raise CaseError(f"{path.name}: missing key(s) {sorted(missing)}")

    mandate = raw["mandate"]
    if not isinstance(mandate, dict):
        raise CaseError(f"{path.name}: mandate must be a mapping")

    return BenignCase(
        case_id=str(raw["case_id"]),
        instruction=str(raw["instruction"]),
        mandate=mandate,
        expect_allowed=bool(raw.get("expect_allowed", True)),
        notes=str(raw.get("notes", "")),
    )
