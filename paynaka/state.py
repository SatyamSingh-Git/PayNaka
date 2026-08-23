"""Durable state the gate consults: nonces, idempotency, the money ledger, counters.

Everything here exists to answer questions the mandate alone cannot: *has this nonce been
spent?*, *did we already run this exact request?*, *how much of this payment has already
been refunded?*, *how many times have we retried this mandate today?*

Two design commitments:

**Atomicity, not read-then-write.** Nonce consumption and idempotency claims go through a
single ``INSERT`` guarded by a ``UNIQUE`` constraint. A ``SELECT`` followed by an
``INSERT`` has a window between them, and two concurrent requests carrying the same nonce
would both find it unused and both proceed. That window is a double-spend.

**Inspectable.** SQLite, one file, plain columns. A reviewer with ``sqlite3`` should be
able to audit the ledger by hand, without running our code and taking its word for it.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Self

from paynaka.clock import IST, Clock
from paynaka.money import MoneyError, to_paise

__all__ = ["IdempotencyRecord", "SqliteState", "StateError"]

_SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS nonces (
    nonce      TEXT PRIMARY KEY,
    mandate_id TEXT NOT NULL,
    spent_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency (
    key          TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    result_json  TEXT NOT NULL,
    created_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    payment_id TEXT NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('capture', 'refund')),
    amount     INTEGER NOT NULL CHECK (amount > 0),
    at         INTEGER NOT NULL,
    ist_day    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ledger_payment ON ledger (payment_id, kind);
CREATE INDEX IF NOT EXISTS ledger_day ON ledger (ist_day, kind);

CREATE TABLE IF NOT EXISTS returns (
    payment_id TEXT PRIMARY KEY,
    at         INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS retries (
    scope   TEXT NOT NULL,
    ist_day TEXT NOT NULL,
    count   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (scope, ist_day)
);

CREATE TABLE IF NOT EXISTS revocations (
    scope      TEXT PRIMARY KEY,
    revoked_at INTEGER NOT NULL
);
"""


class StateError(Exception):
    """A state operation could not be completed safely."""


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    """A previously-completed request and what it returned."""

    key: str
    request_hash: str
    result_json: str
    created_at: int


class SqliteState:
    """SQLite-backed state. Pass ``":memory:"`` for tests.

    Not an ORM and deliberately not clever. Every method is one statement whose semantics
    a reviewer can check against the schema above.
    """

    def __init__(self, path: str | Path = ":memory:", *, clock: Clock | None = None) -> None:
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)

        # check_same_thread=False plus an explicit lock: the FastAPI app touches state
        # from a threadpool, and SQLite's own guard would reject those threads outright.
        self._conn = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._clock = clock

        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
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

    def _now(self, clock: Clock | None) -> int:
        chosen = clock or self._clock
        if chosen is None:
            raise StateError("no clock supplied; state operations must be time-stamped")
        return chosen.epoch()

    @staticmethod
    def _ist_day(epoch: int) -> str:
        """The IST calendar day an instant falls in.

        Daily caps are a business rule expressed in local days, so a refund at 23:00 IST
        and one at 01:00 IST the next morning must land in different buckets even though
        they are three hours apart.
        """
        return datetime.fromtimestamp(epoch, IST).strftime("%Y-%m-%d")

    # ---------------------------------------------------------------- nonces
    def consume_nonce(self, nonce: str, mandate_id: str, *, clock: Clock | None = None) -> bool:
        """Atomically spend ``nonce``. Returns ``True`` if it was fresh, ``False`` if spent.

        The whole replay defence is this one statement. ``INSERT`` against a PRIMARY KEY
        either wins or does nothing; there is no window in which two callers both believe
        they are first.
        """
        if not nonce or not isinstance(nonce, str):
            raise StateError("nonce must be a non-empty string")
        now = self._now(clock)
        with self._lock:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO nonces (nonce, mandate_id, spent_at) VALUES (?, ?, ?)",
                (nonce, mandate_id, now),
            )
            return cursor.rowcount == 1

    def nonce_spent(self, nonce: str) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM nonces WHERE nonce = ?", (nonce,)).fetchone()
        return bool(row is not None)

    # ---------------------------------------------------------------- idempotency
    def claim_idempotency(
        self, key: str, request_hash: str, result_json: str, *, clock: Clock | None = None
    ) -> IdempotencyRecord | None:
        """Record ``key`` as done. Returns ``None`` on success, or the existing record.

        A returned record means this key has been seen. The caller must then compare
        ``request_hash``: same hash is a genuine retry and should replay the stored
        result; a different hash is the same key reused for a different request, which is
        a client bug at best and a substitution attack at worst.
        """
        now = self._now(clock)
        with self._lock:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO idempotency (key, request_hash, result_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (key, request_hash, result_json, now),
            )
            if cursor.rowcount == 1:
                return None
            row = self._conn.execute(
                "SELECT key, request_hash, result_json, created_at FROM idempotency WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:  # pragma: no cover - only reachable if a row vanished mid-call
            raise StateError(f"idempotency key {key!r} neither inserted nor found")
        return IdempotencyRecord(*row)

    def lookup_idempotency(self, key: str) -> IdempotencyRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT key, request_hash, result_json, created_at FROM idempotency WHERE key = ?",
                (key,),
            ).fetchone()
        return IdempotencyRecord(*row) if row else None

    # ---------------------------------------------------------------- ledger
    def record_capture(self, payment_id: str, amount: int, *, clock: Clock | None = None) -> None:
        self._append_ledger(payment_id, "capture", amount, clock)

    def record_refund(self, payment_id: str, amount: int, *, clock: Clock | None = None) -> None:
        self._append_ledger(payment_id, "refund", amount, clock)

    def _append_ledger(self, payment_id: str, kind: str, amount: int, clock: Clock | None) -> None:
        if not payment_id:
            raise StateError("payment_id must be non-empty")

        # Strict int, checked *before* anything else touches the value. Coercing here
        # would be actively dangerous: to_paise("199900") reads a string as rupees and
        # returns 19990000, so a caller that passed a rupee string would silently write a
        # ledger entry 100x too large. The ledger's contract is int paise, full stop.
        if isinstance(amount, bool) or not isinstance(amount, int):
            raise StateError(
                f"ledger amount must be int paise, got {type(amount).__name__}: {amount!r}"
            )
        if amount <= 0:
            raise StateError("ledger entries must be positive; a reversal is its own entry")
        try:
            to_paise(amount)
        except MoneyError as exc:
            raise StateError(f"ledger amount rejected: {exc}") from exc

        now = self._now(clock)
        with self._lock:
            self._conn.execute(
                "INSERT INTO ledger (payment_id, kind, amount, at, ist_day) VALUES (?, ?, ?, ?, ?)",
                (payment_id, kind, amount, now, self._ist_day(now)),
            )

    def captured_amount(self, payment_id: str) -> int:
        return self._sum_ledger("payment_id = ? AND kind = 'capture'", (payment_id,))

    def refunded_amount(self, payment_id: str) -> int:
        return self._sum_ledger("payment_id = ? AND kind = 'refund'", (payment_id,))

    def refundable_amount(self, payment_id: str) -> int:
        """Captured minus already refunded. Never negative -- that would be a ledger bug."""
        remaining = self.captured_amount(payment_id) - self.refunded_amount(payment_id)
        if remaining < 0:  # pragma: no cover - the gate exists to make this unreachable
            raise StateError(
                f"ledger invariant violated: {payment_id} refunded beyond capture by "
                f"{-remaining} paise"
            )
        return remaining

    def daily_refund_total(self, epoch: int) -> int:
        return self._sum_ledger("ist_day = ? AND kind = 'refund'", (self._ist_day(epoch),))

    def _sum_ledger(self, where: str, params: tuple[Any, ...]) -> int:
        with self._lock:
            row = self._conn.execute(
                f"SELECT COALESCE(SUM(amount), 0) FROM ledger WHERE {where}",  # noqa: S608
                params,
            ).fetchone()
        return int(row[0])

    # ---------------------------------------------------------------- returns
    def record_return(self, payment_id: str, *, clock: Clock | None = None) -> None:
        now = self._now(clock)
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO returns (payment_id, at) VALUES (?, ?)", (payment_id, now)
            )

    def has_return(self, payment_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM returns WHERE payment_id = ?", (payment_id,)
            ).fetchone()
        return bool(row is not None)

    # ---------------------------------------------------------------- retries
    def bump_retry(self, scope: str, *, clock: Clock | None = None) -> int:
        """Increment today's retry counter for ``scope`` and return the new value.

        NPCI permits three retries per mandate per cycle. The counter is per IST day, and
        the increment is an upsert so two concurrent retries cannot both read 2 and both
        write 3.
        """
        now = self._now(clock)
        day = self._ist_day(now)
        with self._lock:
            self._conn.execute(
                "INSERT INTO retries (scope, ist_day, count) VALUES (?, ?, 1) "
                "ON CONFLICT (scope, ist_day) DO UPDATE SET count = count + 1",
                (scope, day),
            )
            row = self._conn.execute(
                "SELECT count FROM retries WHERE scope = ? AND ist_day = ?", (scope, day)
            ).fetchone()
        return int(row[0])

    def retry_count(self, scope: str, epoch: int) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT count FROM retries WHERE scope = ? AND ist_day = ?",
                (scope, self._ist_day(epoch)),
            ).fetchone()
        return int(row[0]) if row else 0

    # ---------------------------------------------------------------- revocation
    def revoke(self, scope: str, *, clock: Clock | None = None) -> None:
        """Kill switch. ``scope`` is a mandate id, a session id, or ``"*"`` for everything."""
        now = self._now(clock)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO revocations (scope, revoked_at) VALUES (?, ?)",
                (scope, now),
            )

    def is_revoked(self, *scopes: str) -> bool:
        """True if any supplied scope, or the global ``"*"``, has been revoked."""
        candidates = [*scopes, "*"]
        placeholders = ",".join("?" * len(candidates))
        with self._lock:
            row = self._conn.execute(
                f"SELECT 1 FROM revocations WHERE scope IN ({placeholders}) LIMIT 1",  # noqa: S608
                candidates,
            ).fetchone()
        return bool(row is not None)

    def unrevoke(self, scope: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM revocations WHERE scope = ?", (scope,))
