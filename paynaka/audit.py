"""The audit trail: append-only, hash-chained, verifiable by someone who distrusts us.

Track 01's bar asks for an audit trail. A log file is not an audit trail -- anyone with
write access can edit one and nobody can tell. What makes this one different is that each
record commits to the one before it, so removing or editing a record breaks every hash
after it and ``verify()`` reports exactly where.

The chain proves *internal consistency*, not authenticity: someone who can rewrite the
whole table can also recompute the whole chain. That is an honest limitation and it is
stated here rather than glossed over. The defence against it is the same as for any
ledger -- ship the head hash somewhere you do not control. ``head()`` exists for that.

Run ``python -m paynaka.audit --verify`` to check a chain from the command line.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Self

from paynaka.clock import Clock

__all__ = ["GENESIS", "AuditChain", "AuditError", "AuditRecord", "ChainBreak"]

#: The hash a chain starts from. Any value would do; a constant makes an empty chain and
#: a truncated-to-empty chain indistinguishable only in the trivial case where seq is 0.
GENESIS: Final[str] = "0" * 64

_DOMAIN: Final[bytes] = b"paynaka.audit.v1"

_SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS audit (
    seq       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        INTEGER NOT NULL,
    prev_hash TEXT NOT NULL,
    hash      TEXT NOT NULL UNIQUE,
    payload   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_ts ON audit (ts);
"""


class AuditError(Exception):
    """The audit chain could not be written or read safely."""


@dataclass(frozen=True, slots=True)
class AuditRecord:
    seq: int
    ts: int
    prev_hash: str
    hash: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "prev_hash": self.prev_hash,
            "hash": self.hash,
            "payload": self.payload,
        }


@dataclass(frozen=True, slots=True)
class ChainBreak:
    """Where verification failed, and what was expected instead."""

    seq: int
    kind: str
    expected: str
    found: str

    def __str__(self) -> str:
        return f"chain break at seq {self.seq} ({self.kind}): expected {self.expected}, found {self.found}"


def _digest(prev_hash: str, ts: int, payload: dict[str, Any]) -> str:
    """Hash a record against its predecessor.

    Canonical JSON for the same reasons as the mandate: sorted keys, no insignificant
    whitespace, ASCII-escaped. If two renderings of one payload could hash differently,
    verification would fail on honest data; if two payloads could hash the same, the
    chain would not detect an edit.
    """
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    material = b"|".join(
        [_DOMAIN, prev_hash.encode("ascii"), str(ts).encode("ascii"), body.encode("ascii")]
    )
    return hashlib.sha256(material).hexdigest()


class AuditChain:
    """Append-only hash chain over SQLite. ``:memory:`` for tests."""

    def __init__(self, path: str | Path = ":memory:", *, clock: Clock | None = None) -> None:
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._clock = clock

        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_SCHEMA)

    # ---------------------------------------------------------------- lifecycle
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

    # ---------------------------------------------------------------- writing
    def append(self, payload: dict[str, Any], *, clock: Clock | None = None) -> AuditRecord:
        """Append a record. The only way to write to this table.

        Read-of-head and insert happen under one lock and one transaction, so two
        concurrent appends cannot both chain off the same predecessor and produce a fork.
        """
        chosen = clock or self._clock
        if chosen is None:
            raise AuditError("audit records must be time-stamped; supply a clock")

        try:
            json.dumps(payload, sort_keys=True, ensure_ascii=True, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise AuditError(f"audit payload is not JSON-serialisable: {exc}") from exc

        ts = chosen.epoch()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT hash FROM audit ORDER BY seq DESC LIMIT 1"
                ).fetchone()
                prev = str(row["hash"]) if row else GENESIS
                digest = _digest(prev, ts, payload)
                body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
                cursor = self._conn.execute(
                    "INSERT INTO audit (ts, prev_hash, hash, payload) VALUES (?, ?, ?, ?)",
                    (ts, prev, digest, body),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

        return AuditRecord(
            seq=int(cursor.lastrowid or 0), ts=ts, prev_hash=prev, hash=digest, payload=payload
        )

    # ---------------------------------------------------------------- reading
    def head(self) -> str:
        """The current tip hash. Publish this somewhere you do not control."""
        with self._lock:
            row = self._conn.execute("SELECT hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
        return str(row["hash"]) if row else GENESIS

    def __len__(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM audit").fetchone()
        return int(row[0])

    def records(self, *, limit: int | None = None, since_seq: int = 0) -> list[AuditRecord]:
        sql = "SELECT seq, ts, prev_hash, hash, payload FROM audit WHERE seq > ? ORDER BY seq"
        params: list[Any] = [since_seq]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            AuditRecord(
                seq=int(r["seq"]),
                ts=int(r["ts"]),
                prev_hash=str(r["prev_hash"]),
                hash=str(r["hash"]),
                payload=json.loads(r["payload"]),
            )
            for r in rows
        ]

    # ---------------------------------------------------------------- verifying
    def verify(self) -> ChainBreak | None:
        """Recompute the whole chain. Returns the first break, or ``None`` if intact.

        Three ways a chain can be wrong, all detected:
        an edited payload (the record's own hash no longer matches its contents),
        a removed record (the next record's ``prev_hash`` points at a hash that is gone),
        a reordered or forked chain (``prev_hash`` does not match the actual predecessor).
        """
        expected_prev = GENESIS
        expected_seq = 1

        for record in self.records():
            if record.seq != expected_seq:
                return ChainBreak(
                    seq=record.seq,
                    kind="missing record",
                    expected=f"seq {expected_seq}",
                    found=f"seq {record.seq}",
                )
            if record.prev_hash != expected_prev:
                return ChainBreak(
                    seq=record.seq,
                    kind="broken link",
                    expected=expected_prev[:16],
                    found=record.prev_hash[:16],
                )
            recomputed = _digest(record.prev_hash, record.ts, record.payload)
            if recomputed != record.hash:
                return ChainBreak(
                    seq=record.seq,
                    kind="payload tampered",
                    expected=recomputed[:16],
                    found=record.hash[:16],
                )
            expected_prev = record.hash
            expected_seq += 1

        return None


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m paynaka.audit", description="Verify a PayNaka audit chain."
    )
    parser.add_argument("--db", default="var/audit.db", help="path to the audit database")
    parser.add_argument("--verify", action="store_true", help="recompute the chain")
    parser.add_argument("--head", action="store_true", help="print the current tip hash")
    parser.add_argument("--tail", type=int, metavar="N", help="print the last N records")
    args = parser.parse_args(argv)

    if not Path(args.db).exists():
        print(f"no audit database at {args.db}", file=sys.stderr)
        return 2

    with AuditChain(args.db) as chain:
        if args.head:
            print(chain.head())

        if args.tail:
            for record in chain.records()[-args.tail :]:
                print(json.dumps(record.to_dict(), indent=2))

        if args.verify or not (args.head or args.tail):
            total = len(chain)
            break_at = chain.verify()
            if break_at is None:
                print(f"chain intact: {total} record(s), head {chain.head()[:16]}...")
                return 0
            print(f"CHAIN BROKEN: {break_at}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
