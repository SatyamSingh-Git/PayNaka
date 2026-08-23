"""Anchoring the audit chain to something the writer does not control.

The chain in ``paynaka/audit.py`` proves *internal consistency*. Anyone who can rewrite the
whole table can recompute the whole chain, and a chain recomputed from scratch verifies
perfectly. Truncation is the same problem wearing a smaller hat: lop records off the end
and what remains is shorter, internally consistent, and silent about what is missing.

Both have the same fix and it is not cryptographic cleverness. It is **witnesses**.

An anchor is one signed sentence: *at time T, this chain had N records and its tip was H.*
Once that sentence exists somewhere the attacker cannot reach, the two attacks above stop
working, because a rewritten chain produces a different H at every N and a truncated one
cannot reach N at all.

Everything then turns on *where the sentence lives* and *who signed it*, and this module is
honest about there being three tiers rather than pretending one solution:

**Tier 1 — a separate append-only log, separate file.** Stops a careless attacker and an
accidental corruption. Stops nobody with filesystem access. Included because it is the
floor, not because it is a defence.

**Tier 2 — signed by a notary key the gate does not hold.** Now rewriting the chain is not
enough; the attacker needs a key that lives in a different process, and in a real
deployment a different machine with different credentials. This is the tier most systems
that claim "tamper-evident logs" actually reach.

**Tier 3 — witnessed by the payment rail.** The head hash rides along in the ``notes`` of
the very API calls PayNaka is already making. Razorpay stores them. An attacker who owns
the merchant's database, the gate process, and the notary key still does not own
Razorpay's records, and reconciliation against them catches a rewrite that beats
everything else here. It costs nothing extra, because the calls were happening anyway.

Tier 3 is the one worth arguing about, and its limitation is stated plainly: it witnesses
the chain only at the moments money moved. A denial-only stretch of the chain is anchored
by tiers 1 and 2 alone. That is a real gap and it is narrower than the one it replaces.

What none of this defends: an attacker who controls the gate, the notary and the merchant's
Razorpay account at once has already won, and no arrangement of hashes changes that.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Self

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from paynaka.audit import GENESIS, AuditChain
from paynaka.clock import Clock

__all__ = [
    "Anchor",
    "AnchorBreak",
    "AnchorError",
    "AnchorLog",
    "Notary",
    "NotaryVerifier",
    "rail_note",
    "verify_against_anchors",
    "verify_against_rail",
    "witnesses_from_rail",
]

#: Domain separation, for the same reason the mandate has it: an anchor signature must
#: never verify as a mandate signature, whatever an attacker manages to get signed.
DOMAIN: Final[bytes] = b"paynaka.anchor.v1"

_SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS anchors (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    at        INTEGER NOT NULL,
    length    INTEGER NOT NULL,
    head      TEXT NOT NULL,
    notary    TEXT NOT NULL,
    signature TEXT NOT NULL,
    UNIQUE (length, head, notary)
);
CREATE INDEX IF NOT EXISTS anchors_length ON anchors (length);
"""


class AnchorError(Exception):
    """An anchor could not be created, stored or read safely."""


@dataclass(frozen=True, slots=True)
class Anchor:
    """One witnessed statement about the chain, signed by somebody who is not the writer."""

    at: int
    length: int
    head: str
    notary: str  #: fingerprint of the notary's public key, so a reader knows who signed
    signature: str  #: hex

    def __post_init__(self) -> None:
        if isinstance(self.length, bool) or not isinstance(self.length, int):
            raise AnchorError("length must be an int")
        if self.length < 0:
            raise AnchorError("length must not be negative")
        if not self.head or len(self.head) != 64:
            raise AnchorError("head must be a 64-character hex digest")

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "length": self.length,
            "head": self.head,
            "notary": self.notary,
            "signature": self.signature,
        }


def _statement(at: int, length: int, head: str, notary: str) -> bytes:
    """The exact bytes a notary signs. Canonical, so one statement has one rendering."""
    body = json.dumps(
        {"at": at, "length": length, "head": head, "notary": notary},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return DOMAIN + b"|" + body.encode("ascii")


def _fingerprint(public_key: Ed25519PublicKey) -> str:
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    raw = public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()[:16]


class Notary:
    """Holds a signing key the gate process does not.

    In this repository the notary is constructed alongside the engine, because a demo runs
    in one process. That is a deployment detail and it is the *only* thing separating tier
    2 from tier 1 -- so it is worth saying out loud: an anchor is exactly as external as
    the key that signed it. Move this to another machine and the tier moves with it.
    """

    __slots__ = ("_key",)

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self._key = private_key

    @classmethod
    def generate(cls) -> Notary:
        return cls(Ed25519PrivateKey.generate())

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self._key.public_key())

    def verifier(self) -> NotaryVerifier:
        return NotaryVerifier(self._key.public_key())

    def witness(self, chain: AuditChain, *, clock: Clock) -> Anchor:
        """Sign a statement about the chain as it stands right now."""
        length = len(chain)
        head = chain.head()
        at = clock.epoch()
        fingerprint = self.fingerprint
        signature = self._key.sign(_statement(at, length, head, fingerprint))
        return Anchor(
            at=at,
            length=length,
            head=head,
            notary=fingerprint,
            signature=signature.hex(),
        )


class NotaryVerifier:
    """Holds only the public key, so a compromised gate cannot forge a witness."""

    __slots__ = ("_key",)

    def __init__(self, public_key: Ed25519PublicKey) -> None:
        self._key = public_key

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self._key)

    def check(self, anchor: Anchor) -> bool:
        if anchor.notary != self.fingerprint:
            return False
        try:
            self._key.verify(
                bytes.fromhex(anchor.signature),
                _statement(anchor.at, anchor.length, anchor.head, anchor.notary),
            )
        except (InvalidSignature, ValueError):
            return False
        return True


class AnchorLog:
    """Append-only store for anchors. Deliberately a different file from the audit chain.

    Same file, same blast radius: an attacker rewriting one table would rewrite the other
    in the same transaction. Separating them is the cheapest thing that makes the two
    attacks require two capabilities instead of one.
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def append(self, anchor: Anchor) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO anchors (at, length, head, notary, signature) "
                "VALUES (?, ?, ?, ?, ?)",
                (anchor.at, anchor.length, anchor.head, anchor.notary, anchor.signature),
            )

    def all(self) -> list[Anchor]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT at, length, head, notary, signature FROM anchors ORDER BY length, id"
            ).fetchall()
        return [
            Anchor(
                at=int(r["at"]),
                length=int(r["length"]),
                head=str(r["head"]),
                notary=str(r["notary"]),
                signature=str(r["signature"]),
            )
            for r in rows
        ]

    def latest(self) -> Anchor | None:
        anchors = self.all()
        return anchors[-1] if anchors else None

    def __len__(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM anchors").fetchone()
        return int(row[0])


@dataclass(frozen=True, slots=True)
class AnchorBreak:
    """Which witnessed statement the chain now contradicts."""

    kind: str
    anchor: Anchor
    expected: str
    found: str

    def __str__(self) -> str:
        return (
            f"anchor break ({self.kind}) against the witness at length {self.anchor.length}: "
            f"expected {self.expected}, found {self.found}"
        )


def verify_against_anchors(
    chain: AuditChain, log: AnchorLog, verifier: NotaryVerifier
) -> AnchorBreak | None:
    """Check the chain against every witness. Returns the first contradiction.

    Three things are caught here that ``AuditChain.verify()`` cannot catch on its own,
    because it is checking the chain against itself:

    **A wholesale rewrite.** Recomputing the table from scratch produces a chain that
    verifies internally and has a different tip at every length. Any witness at any past
    length contradicts it.

    **Trailing truncation.** A witness at length 40 cannot be satisfied by a chain of
    length 32, whatever that chain says about itself.

    **A forged witness.** An attacker who rewrites the chain *and* writes matching anchors
    still has to sign them, and the signing key is not in the chain's blast radius.
    """
    for anchor in log.all():
        if not verifier.check(anchor):
            return AnchorBreak(
                kind="forged witness",
                anchor=anchor,
                expected=f"a signature from notary {verifier.fingerprint}",
                found=f"one that does not verify (claims {anchor.notary})",
            )

        if len(chain) < anchor.length:
            return AnchorBreak(
                kind="truncated",
                anchor=anchor,
                expected=f"at least {anchor.length} records",
                found=f"{len(chain)}",
            )

        witnessed = head_at(chain, anchor.length)
        if witnessed != anchor.head:
            return AnchorBreak(
                kind="rewritten",
                anchor=anchor,
                expected=anchor.head[:16],
                found=witnessed[:16],
            )

    return None


def witnesses_from_rail(records: list[dict[str, Any]]) -> list[tuple[int, str]]:
    """Pull ``(length, head_prefix)`` pairs out of whatever the gateway handed back.

    ``records`` is a list of payment/order/refund objects as a gateway returns them --
    each carrying the ``notes`` PayNaka sent. Anything without both fields is skipped
    rather than treated as a failure: notes are metadata, other systems write them too,
    and a note somebody else added is not evidence of tampering.
    """
    out: list[tuple[int, str]] = []
    for record in records:
        notes = record.get("notes") or record.get("raw", {}).get("notes") or {}
        head = notes.get("paynaka_audit_head")
        length = notes.get("paynaka_audit_len")
        if not head or not length:
            continue
        try:
            out.append((int(length), str(head)))
        except (TypeError, ValueError):
            continue
    return sorted(set(out))


def verify_against_rail(chain: AuditChain, records: list[dict[str, Any]]) -> AnchorBreak | None:
    """Check the chain against what the *payment gateway* remembers about it.

    This is the tier that survives an attacker who owns everything local. The head prefix
    travelled out in the ``notes`` of a real money call; Razorpay stored it; and the
    merchant cannot go back and edit Razorpay's record of a payment they made.

    Two honest limitations. It witnesses only at the moments money moved, so a stretch of
    the chain containing nothing but denials is not covered here. And it compares sixteen
    hex characters rather than sixty-four, which is a size trade explained at
    :func:`rail_note`.
    """
    for length, head_prefix in witnesses_from_rail(records):
        if length <= 0:
            continue
        anchor = Anchor(
            at=0,
            length=length,
            head=head_prefix.ljust(64, "0"),
            notary="rail",
            signature="",
        )
        if len(chain) < length:
            return AnchorBreak(
                kind="truncated",
                anchor=anchor,
                expected=f"at least {length} records",
                found=f"{len(chain)}",
            )
        actual = head_at(chain, length)
        if not actual.startswith(head_prefix):
            return AnchorBreak(
                kind="rewritten",
                anchor=anchor,
                expected=head_prefix,
                found=actual[: len(head_prefix)],
            )
    return None


def head_at(chain: AuditChain, length: int) -> str:
    """The chain's tip hash when it had exactly ``length`` records."""
    if length <= 0:
        return GENESIS
    records = chain.records(limit=length)
    if len(records) < length:
        raise AnchorError(f"chain has {len(records)} records, cannot reach length {length}")
    return records[-1].hash


def rail_note(head: str) -> str:
    """The value PayNaka puts in a rail call's ``notes``, so the gateway witnesses it.

    Truncated to sixteen hex characters, which is 64 bits: an attacker who wants a
    rewritten chain to reproduce a witnessed prefix has to find a collision, and the
    ledger is not worth 2^32 hashes. The full digest would be better and gateway note
    fields are small, so this is a size trade made deliberately and written down.
    """
    if not head or len(head) != 64:
        raise AnchorError("head must be a 64-character hex digest")
    return head[:16]
