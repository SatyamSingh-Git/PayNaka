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

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Self

from paynaka.clock import IST, Clock
from paynaka.money import MoneyError, to_paise

__all__ = ["Authority", "Escalation", "IdempotencyRecord", "SqliteState", "StateError"]

_SCHEMA: Final[str] = """
-- The authority graph: which mandate created which order, and which payment came from it.
--
-- Capture and refund name a payment_id and nothing else, so the gate could check that a
-- refund did not exceed the captured balance while having no idea whose payment it was.
-- An audit put it plainly: a fresh refund-capable mandate could operate on any payment
-- that happened to be in state. The balance arithmetic was right and the authority
-- question was never asked.
--
-- Two tables rather than one because the two facts are learned at different times and by
-- different parties. We create the order and know the mandate behind it; the payment id
-- arrives later, from the provider, after a human authenticated at Checkout -- which is
-- the step an agent cannot take, and the reason this graph has a gap an agent cannot
-- close on its own.
CREATE TABLE IF NOT EXISTS orders (
    order_id   TEXT PRIMARY KEY,
    mandate_id TEXT NOT NULL,
    subject    TEXT NOT NULL,
    session_id TEXT NOT NULL,
    at         INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS payments (
    payment_id TEXT PRIMARY KEY,
    order_id   TEXT NOT NULL,
    at         INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS payments_order ON payments (order_id);

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

-- A claim on part of a payment's refundable balance, taken before the rail is called.
--
-- Without this, checking the balance and writing the ledger entry are two statements with
-- a gap between them, and two concurrent refunds for the same payment both read the full
-- balance and both proceed. Measured: twenty concurrent refunds on one payment, every one
-- of them approved, sixteen of them stopped only because the gateway independently
-- refused. Relying on the gateway to enforce our own bound is not enforcement.
--
-- 'held' means claimed and unresolved -- deliberately including the case where the rail
-- timed out, because the money may have moved and releasing the claim would let a second
-- refund spend it again.
CREATE TABLE IF NOT EXISTS reservations (
    key        TEXT PRIMARY KEY,
    payment_id TEXT NOT NULL,
    amount     INTEGER NOT NULL CHECK (amount > 0),
    state      TEXT NOT NULL CHECK (state IN ('held', 'settled', 'released')),
    at         INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS reservations_held ON reservations (payment_id, state);

CREATE TABLE IF NOT EXISTS mandate_spend (
    mandate_id TEXT NOT NULL,
    key        TEXT NOT NULL,
    amount     INTEGER NOT NULL CHECK (amount > 0),
    at         INTEGER NOT NULL,
    PRIMARY KEY (mandate_id, key)
);
CREATE INDEX IF NOT EXISTS mandate_spend_by_mandate ON mandate_spend (mandate_id);

-- A short-lived, single-use ticket that lets an authenticated MCP client bind the mandate
-- it was just issued to its own session.
--
-- It exists because the proxy's `bind()` had no route into it: an external client could
-- list and read, and every write answered "no mandate". The session identity itself came
-- from a client-supplied header, so a caller could also simply claim somebody else's.
--
-- The token is stored as a hash, never in the clear. A grant is authority to spend a
-- mandate, so a leaked database must not hand anybody a working one -- the same reasoning
-- that applies to any other credential at rest.
--
-- Single-use is enforced by the state column rather than a flag, so two concurrent
-- redemptions cannot both succeed: 'issued' -> 'redeemed' is one guarded UPDATE.
CREATE TABLE IF NOT EXISTS grants (
    token_hash   TEXT PRIMARY KEY,
    mandate_json TEXT NOT NULL,
    subject      TEXT NOT NULL,
    issued_at    INTEGER NOT NULL,
    expires_at   INTEGER NOT NULL,
    state        TEXT NOT NULL CHECK (state IN ('issued', 'redeemed')),
    redeemed_by  TEXT,
    redeemed_at  INTEGER
);
CREATE INDEX IF NOT EXISTS grants_state ON grants (state, expires_at);

CREATE TABLE IF NOT EXISTS returns (
    payment_id TEXT PRIMARY KEY,
    at         INTEGER NOT NULL
);

-- Denials, counted per scope per IST day. The gate refusing a request costs PayNaka
-- microseconds and costs the *agent operator* a full model turn, so an attacker who can
-- keep an agent retrying against a wall burns somebody else's money without moving a
-- rupee. This counter is what turns that from unbounded into bounded.
CREATE TABLE IF NOT EXISTS denials (
    scope   TEXT NOT NULL,
    ist_day TEXT NOT NULL,
    count   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (scope, ist_day)
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

-- A money action waiting for a human, and the record of what the human said.
--
-- Bound to `request_hash`, not to the amount or the session: an approval names one exact
-- request. Otherwise "yes to Rs 3,000" becomes reusable authority, which is the shape of
-- every replay bug in this file.
--
-- Single-use, enforced by the state column rather than by a flag: the transition
-- 'approved' -> 'consumed' is one guarded UPDATE, so two concurrent retries cannot both
-- find an approval and both spend it.
--
-- `expires_at` is checked at every read that matters rather than swept by a background
-- job. A sweeper that has not run yet is an approval that outlives its window, and the
-- policy says an unanswered escalation resolves to DENY.
CREATE TABLE IF NOT EXISTS escalations (
    id           TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    mandate_id   TEXT NOT NULL,
    session_id   TEXT NOT NULL,
    subject      TEXT NOT NULL,
    action       TEXT NOT NULL,
    amount       INTEGER NOT NULL CHECK (amount > 0),
    summary_json TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    expires_at   INTEGER NOT NULL,
    state        TEXT NOT NULL
                 CHECK (state IN ('pending', 'approved', 'denied', 'consumed')),
    decided_at   INTEGER,
    decided_by   TEXT
);
CREATE INDEX IF NOT EXISTS escalations_hash ON escalations (request_hash, state);
CREATE INDEX IF NOT EXISTS escalations_state ON escalations (state, expires_at);
"""


class StateError(Exception):
    """A state operation could not be completed safely."""


@dataclass(frozen=True, slots=True)
class Escalation:
    """A money action waiting for a human, or the record of what they decided."""

    id: str
    request_hash: str
    mandate_id: str
    session_id: str
    subject: str
    action: str
    amount: int
    summary: dict[str, Any]
    created_at: int
    expires_at: int
    state: str
    decided_at: int | None = None
    decided_by: str | None = None

    def is_expired(self, now: int) -> bool:
        """Expiry is a property of the clock, not of the stored state.

        A row still reading 'pending' past its window has expired whether or not anything
        has got round to updating it, and every caller here asks this question rather than
        trusting the column.
        """
        return now >= self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "request_hash": self.request_hash,
            "mandate_id": self.mandate_id,
            "session_id": self.session_id,
            "subject": self.subject,
            "action": self.action,
            "amount": self.amount,
            "summary": self.summary,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "state": self.state,
            "decided_at": self.decided_at,
            "decided_by": self.decided_by,
        }


@dataclass(frozen=True, slots=True)
class Authority:
    """Who a payment traces back to: the order, the mandate, the shopper, the session.

    Returned by :meth:`SqliteState.authority_for`. The gate compares ``subject`` and
    nothing else, deliberately -- see the check for why binding to ``mandate_id`` would be
    stronger on paper and wrong in practice. The other three are carried because evidence
    should show the whole chain even where only one link is enforced.
    """

    order_id: str
    mandate_id: str
    subject: str
    session_id: str


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

    def complete_idempotency(self, key: str, result_json: str) -> bool:
        """Store what a claimed key actually produced. ``True`` if a row was updated.

        The claim is taken *before* the rail is called -- it has to be, or two concurrent
        copies of one request both find the key free -- so at claim time there is no result
        to record and a placeholder goes in. Without this second half the placeholder was
        all there ever was, and a retry after a timeout got back "nothing happened" about a
        payment that had in fact been made.
        """
        if not key:
            raise StateError("completing an idempotency record needs a key")
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE idempotency SET result_json = ? WHERE key = ?", (result_json, key)
            )
        return cursor.rowcount == 1

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

    def held_amount(self, payment_id: str) -> int:
        """Refundable balance claimed by a refund that has not resolved yet."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM reservations "
                "WHERE payment_id = ? AND state = 'held'",
                (payment_id,),
            ).fetchone()
        return int(row[0])

    def refundable_amount(self, payment_id: str) -> int:
        """Captured, minus already refunded, minus claimed and unresolved.

        Held amounts are subtracted because a claim that has not resolved might yet
        become a refund. Counting it as available is how the same rupee gets refunded
        twice, and the whole reason :meth:`reserve_refund` exists.
        """
        remaining = (
            self.captured_amount(payment_id)
            - self.refunded_amount(payment_id)
            - self.held_amount(payment_id)
        )
        if remaining < 0:  # pragma: no cover - the gate exists to make this unreachable
            raise StateError(
                f"ledger invariant violated: {payment_id} refunded beyond capture by "
                f"{-remaining} paise"
            )
        return remaining

    # ---------------------------------------------------------------- reservations
    # ---------------------------------------------------------------- grants
    def issue_grant(
        self,
        token_hash: str,
        mandate_json: str,
        subject: str,
        ttl_seconds: int,
        *,
        clock: Clock | None = None,
    ) -> int:
        """Record a single-use ticket for binding a mandate. Returns its expiry.

        Only the hash is stored. A grant is authority to spend a mandate, so a database
        somebody walks off with must not contain working ones.
        """
        if not token_hash or not mandate_json or not subject:
            raise StateError("a grant needs a token, a mandate and a subject")
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
            raise StateError(f"a grant ttl must be a positive int, got {ttl_seconds!r}")

        now = self._now(clock)
        expires_at = now + ttl_seconds
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO grants "
                "(token_hash, mandate_json, subject, issued_at, expires_at, state) "
                "VALUES (?, ?, ?, ?, ?, 'issued')",
                (token_hash, mandate_json, subject, now, expires_at),
            )
        return expires_at

    def redeem_grant(self, token_hash: str, by: str, *, clock: Clock | None = None) -> str | None:
        """Spend a grant. Returns the mandate it carried, or ``None``.

        ``None`` for every reason -- unknown, already spent, expired -- because telling a
        caller *which* tells them whether a token they guessed ever existed.

        One guarded UPDATE, so two concurrent redemptions cannot both win. Expiry is part
        of the guard rather than checked before it: a check-then-update has a window, and
        a grant is a capability.
        """
        if not token_hash or not by:
            raise StateError("redeeming a grant needs a token and a caller")

        now = self._now(clock)
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE grants SET state = 'redeemed', redeemed_by = ?, redeemed_at = ? "
                "WHERE token_hash = ? AND state = 'issued' AND expires_at > ?",
                (by, now, token_hash, now),
            )
            if cursor.rowcount != 1:
                return None
            row = self._conn.execute(
                "SELECT mandate_json FROM grants WHERE token_hash = ?", (token_hash,)
            ).fetchone()
        return str(row[0]) if row is not None else None

    def grant_state(self, token_hash: str) -> str | None:
        """What became of a grant. For the console and for tests, never for a decision."""
        with self._lock:
            row = self._conn.execute(
                "SELECT state FROM grants WHERE token_hash = ?", (token_hash,)
            ).fetchone()
        return str(row[0]) if row is not None else None

    def reserve_mandate_spend(
        self, mandate_id: str, key: str, amount: int, ceiling: int, *, clock: Clock | None = None
    ) -> bool:
        """Atomically claim ``amount`` of a mandate's *remaining* authority. ``True`` if claimed.

        This is what makes ``max_total`` a budget rather than a per-request ceiling, and
        its absence was a hole straight through the project's central claim. ``check_total``
        asks "does this request fit the budget?" and every request answered yes, so one
        signed mandate authorising Rs 1,999 moved Rs 5,997 across three requests with three
        fresh idempotency keys. Idempotency stops the *same* request repeating; it has
        never stopped a caller spending the same authority again under a new key.

        One statement, for the same reason as :meth:`reserve_refund`: the remaining balance
        is computed inside the ``INSERT``, so there is no instant at which two callers both
        read the same remainder and both decide they fit inside it.

        Keyed on ``(mandate_id, key)`` so a replay of one request is a no-op that returns
        ``True`` -- it already holds its reservation, and charging it twice for one purchase
        would be the mirror of the bug this fixes.
        """
        if isinstance(amount, bool) or not isinstance(amount, int):
            raise StateError(f"a mandate claim must be int paise, got {type(amount).__name__}")
        if isinstance(ceiling, bool) or not isinstance(ceiling, int):
            raise StateError(f"a ceiling must be int paise, got {type(ceiling).__name__}")
        if amount <= 0:
            raise StateError("a mandate claim must be for a positive amount")
        if not mandate_id or not key:
            raise StateError("a mandate claim needs a mandate and a key")

        now = self._now(clock)
        with self._lock:
            existing = self._conn.execute(
                "SELECT amount FROM mandate_spend WHERE mandate_id = ? AND key = ?",
                (mandate_id, key),
            ).fetchone()
            if existing is not None:
                # Already claimed by this exact request. A retry is not a second purchase.
                return True
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO mandate_spend (mandate_id, key, amount, at)
                SELECT ?, ?, ?, ?
                WHERE ? <= ? - (
                    SELECT COALESCE(SUM(amount), 0) FROM mandate_spend WHERE mandate_id = ?
                )
                """,
                (mandate_id, key, amount, now, amount, ceiling, mandate_id),
            )
            return cursor.rowcount == 1

    def mandate_spent(self, mandate_id: str) -> int:
        """What this mandate has already committed. Read off the same rows the claim uses."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM mandate_spend WHERE mandate_id = ?",
                (mandate_id,),
            ).fetchone()
        return int(row[0])

    def mandate_remaining(self, mandate_id: str, ceiling: int) -> int:
        """Authority left. Never negative, so a caller cannot read a breach as headroom."""
        return max(0, ceiling - self.mandate_spent(mandate_id))

    def reserve_refund(
        self, key: str, payment_id: str, amount: int, *, clock: Clock | None = None
    ) -> bool:
        """Atomically claim ``amount`` of what remains refundable. ``True`` if claimed.

        One statement, deliberately. The balance is computed *inside* the ``INSERT``, so
        there is no instant at which two callers can both read the same remaining balance
        and both decide they fit into it -- the same trick, and for the same reason, as
        :meth:`consume_nonce`.

        ``INSERT OR IGNORE`` rather than a plain insert so a repeated key is a refusal
        rather than an exception: the caller's question is "may I proceed", and the two
        reasons for no do not need different plumbing.
        """
        if isinstance(amount, bool) or not isinstance(amount, int):
            raise StateError(f"reservation must be int paise, got {type(amount).__name__}")
        if amount <= 0:
            raise StateError("a reservation must be for a positive amount")
        if not key or not payment_id:
            raise StateError("a reservation needs a key and a payment")

        now = self._now(clock)
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO reservations (key, payment_id, amount, state, at)
                SELECT ?, ?, ?, 'held', ?
                WHERE ? <= (
                    (SELECT COALESCE(SUM(amount), 0) FROM ledger
                       WHERE payment_id = ? AND kind = 'capture')
                  - (SELECT COALESCE(SUM(amount), 0) FROM ledger
                       WHERE payment_id = ? AND kind = 'refund')
                  - (SELECT COALESCE(SUM(amount), 0) FROM reservations
                       WHERE payment_id = ? AND state = 'held')
                )
                """,
                (key, payment_id, amount, now, amount, payment_id, payment_id, payment_id),
            )
            return cursor.rowcount == 1

    def settle_reservation(self, key: str, confirmed: int, *, clock: Clock | None = None) -> None:
        """Turn a held claim into a ledger entry for what the rail actually moved.

        ``confirmed`` comes from the rail, never from the request. They differ on a
        partial refund, and a ledger that records the ask rather than the outcome drifts
        away from the money by exactly the difference.
        """
        if isinstance(confirmed, bool) or not isinstance(confirmed, int):
            raise StateError(f"settlement must be int paise, got {type(confirmed).__name__}")

        now = self._now(clock)
        with self._lock:
            row = self._conn.execute(
                "SELECT payment_id, amount, state FROM reservations WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                raise StateError(f"no reservation to settle under key {key!r}")
            payment_id, held, state = row[0], int(row[1]), row[2]
            if state != "held":
                raise StateError(f"reservation {key!r} is already {state}")
            if confirmed > held:
                # Settling above the claim would spend balance nobody reserved, which is
                # the exact hole the reservation was taken to close.
                raise StateError(
                    f"reservation {key!r} held {held} paise; cannot settle {confirmed}"
                )

            self._conn.execute("UPDATE reservations SET state = 'settled' WHERE key = ?", (key,))
            if confirmed > 0:
                self._conn.execute(
                    "INSERT INTO ledger (payment_id, kind, amount, at, ist_day) "
                    "VALUES (?, 'refund', ?, ?, ?)",
                    (payment_id, confirmed, now, self._ist_day(now)),
                )

    def release_reservation(self, key: str) -> bool:
        """Give the claim back. Only ever called on a *definitive* refusal.

        A timeout must not release: the rail may have moved the money, and handing the
        balance back would let a second request spend it a second time. That claim stays
        held until reconciliation says otherwise, which is the conservative direction.
        """
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE reservations SET state = 'released' WHERE key = ? AND state = 'held'",
                (key,),
            )
            return cursor.rowcount == 1

    def reservation_state(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT state FROM reservations WHERE key = ?", (key,)
            ).fetchone()
        return str(row[0]) if row else None

    def unresolved_reservations(self) -> list[tuple[str, str, int]]:
        """Every claim still held: ``(key, payment_id, amount)``.

        The reconciliation queue. A row here means PayNaka asked a rail to move money and
        never learned whether it did, which is the only honest thing to say about it.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, payment_id, amount FROM reservations WHERE state = 'held' ORDER BY at"
            ).fetchall()
        return [(str(r[0]), str(r[1]), int(r[2])) for r in rows]

    # ---------------------------------------------------------------- authority graph
    def record_order(
        self,
        order_id: str,
        *,
        mandate_id: str,
        subject: str,
        session_id: str,
        clock: Clock | None = None,
    ) -> None:
        """Remember who an order was created for. Written when the rail confirms one.

        ``INSERT OR IGNORE``: an order id is provider-assigned and unique, so a second
        write for the same id is a replay of the same fact. Overwriting would let a later
        request rewrite an earlier order's authority, which is the whole thing this table
        exists to prevent.
        """
        if not order_id or not mandate_id or not subject:
            raise StateError("an order's authority needs an order, a mandate and a subject")
        now = self._now(clock)
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO orders (order_id, mandate_id, subject, session_id, at) "
                "VALUES (?, ?, ?, ?, ?)",
                (order_id, mandate_id, subject, session_id, now),
            )

    def link_payment(self, payment_id: str, order_id: str, *, clock: Clock | None = None) -> None:
        """Attach a provider payment to the order it settled.

        The link arrives from the provider -- in a webhook, or on a fetch -- because it is
        created when a human authenticates at Checkout. An agent cannot manufacture it,
        which is exactly why the graph is worth checking.
        """
        if not payment_id or not order_id:
            raise StateError("linking a payment needs both a payment and an order")
        now = self._now(clock)
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO payments (payment_id, order_id, at) VALUES (?, ?, ?)",
                (payment_id, order_id, now),
            )

    def authority_for(self, payment_id: str) -> Authority | None:
        """Who a payment belongs to, or ``None`` if this system never saw it created.

        ``None`` is the answer that matters. A payment with no recorded origin is not a
        payment this service has any business capturing or refunding, and the gate reads
        the absence as a refusal rather than as permission.
        """
        if not payment_id:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT o.order_id, o.mandate_id, o.subject, o.session_id "
                "FROM payments p JOIN orders o ON o.order_id = p.order_id "
                "WHERE p.payment_id = ?",
                (payment_id,),
            ).fetchone()
        if row is None:
            return None
        return Authority(
            order_id=str(row[0]),
            mandate_id=str(row[1]),
            subject=str(row[2]),
            session_id=str(row[3]),
        )

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

    # ---------------------------------------------------------------- denials
    def bump_denial(self, scope: str, *, clock: Clock | None = None) -> int:
        """Count one refusal against ``scope`` and return the running total for today.

        An upsert rather than read-then-write, for the same reason as :meth:`bump_retry`:
        a loop hammering the gate is exactly the situation in which two increments race,
        and a breaker that undercounts under load is a breaker that does not trip when it
        matters most.
        """
        if not scope:
            raise StateError("a denial must be counted against a scope")
        now = self._now(clock)
        day = self._ist_day(now)
        with self._lock:
            self._conn.execute(
                "INSERT INTO denials (scope, ist_day, count) VALUES (?, ?, 1) "
                "ON CONFLICT (scope, ist_day) DO UPDATE SET count = count + 1",
                (scope, day),
            )
            row = self._conn.execute(
                "SELECT count FROM denials WHERE scope = ? AND ist_day = ?", (scope, day)
            ).fetchone()
        return int(row[0])

    def denial_count(self, scope: str, epoch: int) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT count FROM denials WHERE scope = ? AND ist_day = ?",
                (scope, self._ist_day(epoch)),
            ).fetchone()
        return int(row[0]) if row else 0

    def clear_denials(self, scope: str) -> None:
        """Forget a scope's refusals. The other half of ``unrevoke``, for an operator."""
        with self._lock:
            self._conn.execute("DELETE FROM denials WHERE scope = ?", (scope,))

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

    # ---------------------------------------------------------------- escalations
    def open_escalation(
        self,
        *,
        escalation_id: str,
        request_hash: str,
        mandate_id: str,
        session_id: str,
        subject: str,
        action: str,
        amount: int,
        summary: dict[str, Any],
        timeout_seconds: int,
        clock: Clock | None = None,
    ) -> Escalation:
        """Open an escalation for this request, or return the one already open for it.

        Idempotent on ``request_hash``, which matters more than it looks. A duplicate
        webhook delivery for the same above-threshold action must not put a second
        approval in somebody's queue: two rows for one request means a human can approve
        it twice, and the second approval is authority nobody granted twice.

        A previously-decided or expired escalation does not block a new one. The old
        answer was about a window that has closed; asking again is correct, and the audit
        chain still carries the earlier record.
        """
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            raise StateError(f"an escalation needs positive int paise, got {amount!r}")
        if not escalation_id or not request_hash:
            raise StateError("an escalation needs an id and a request hash")
        if timeout_seconds <= 0:
            raise StateError("an escalation timeout must be positive")

        now = self._now(clock)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM escalations
                 WHERE request_hash = ? AND state = 'pending' AND expires_at > ?
                 ORDER BY created_at DESC LIMIT 1
                """,
                (request_hash, now),
            ).fetchone()
            if row is not None:
                return _as_escalation(row)

            self._conn.execute(
                """
                INSERT INTO escalations (
                    id, request_hash, mandate_id, session_id, subject, action, amount,
                    summary_json, created_at, expires_at, state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                """,
                (
                    escalation_id,
                    request_hash,
                    mandate_id,
                    session_id,
                    subject,
                    action,
                    amount,
                    json.dumps(summary, sort_keys=True, default=str),
                    now,
                    now + timeout_seconds,
                ),
            )
            fresh = self._conn.execute(
                "SELECT * FROM escalations WHERE id = ?", (escalation_id,)
            ).fetchone()
        return _as_escalation(fresh)

    def decide_escalation(
        self, escalation_id: str, *, approve: bool, by: str, clock: Clock | None = None
    ) -> str | None:
        """Record a human's answer. Returns the new state, or ``None`` if it did not apply.

        One guarded ``UPDATE``, so the first answer wins and a second changes nothing:
        approve-then-deny and two simultaneous approvals both resolve to exactly one
        decision. ``None`` covers every reason for no -- unknown id, already decided,
        expired -- because the caller's question is "did my answer land", and an approver
        does not need those distinguished.

        The window is checked here as well as at consumption. An approval granted after
        the window closed would otherwise sit in the table looking valid.
        """
        if not escalation_id or not by:
            raise StateError("a decision needs an escalation id and an approver")

        now = self._now(clock)
        target = "approved" if approve else "denied"
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE escalations
                   SET state = ?, decided_at = ?, decided_by = ?
                 WHERE id = ? AND state = 'pending' AND expires_at > ?
                """,
                (target, now, by, escalation_id, now),
            )
        return target if cursor.rowcount == 1 else None

    def consume_approval(self, request_hash: str, *, clock: Clock | None = None) -> str | None:
        """Spend a human approval for exactly this request. Returns its id, or ``None``.

        The most security-relevant statement in this file after :meth:`consume_nonce`.
        Three properties, all in the ``WHERE`` clause rather than in a caller's discipline:

        * **Bound to the request.** ``request_hash`` covers the whole body, so an approval
          for one order cannot release a different order of the same amount.
        * **Single-use.** The transition is guarded on the current state, so two concurrent
          retries cannot both spend one approval.
        * **Still inside its window.** ``on_timeout`` is DENY and not configurable, so an
          approval whose window closed is not an approval.
        """
        if not request_hash:
            return None

        now = self._now(clock)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT id FROM escalations
                 WHERE request_hash = ? AND state = 'approved' AND expires_at > ?
                 ORDER BY decided_at ASC LIMIT 1
                """,
                (request_hash, now),
            ).fetchone()
            if row is None:
                return None
            claimed = str(row["id"])
            cursor = self._conn.execute(
                """
                UPDATE escalations SET state = 'consumed'
                 WHERE id = ? AND state = 'approved' AND expires_at > ?
                """,
                (claimed, now),
            )
            if cursor.rowcount != 1:
                return None
        return claimed

    def escalation(self, escalation_id: str) -> Escalation | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM escalations WHERE id = ?", (escalation_id,)
            ).fetchone()
        return _as_escalation(row) if row is not None else None

    def pending_escalations(self, *, clock: Clock | None = None) -> list[Escalation]:
        """Everything still awaiting a human and still inside its window.

        Expired rows are excluded by the query rather than by the reader, so an operator
        console cannot show an approve button for something that already resolved to DENY.
        """
        now = self._now(clock)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM escalations
                 WHERE state = 'pending' AND expires_at > ?
                 ORDER BY created_at ASC
                """,
                (now,),
            ).fetchall()
        return [_as_escalation(row) for row in rows]

    def expired_escalations(self, *, clock: Clock | None = None) -> list[Escalation]:
        """Escalations whose window closed with nobody answering.

        These are DENY by policy. They are surfaced rather than deleted because "nobody
        approved this in time" is exactly the sort of thing an operator should be able to
        count, and a silently-dropped row cannot be counted.
        """
        now = self._now(clock)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM escalations
                 WHERE state = 'pending' AND expires_at <= ?
                 ORDER BY expires_at ASC
                """,
                (now,),
            ).fetchall()
        return [_as_escalation(row) for row in rows]


def _as_escalation(row: sqlite3.Row) -> Escalation:
    """Map a row, tolerating a summary that will not parse.

    A malformed summary is display data, not authority. Refusing to return the escalation
    because its human-readable blurb is broken would turn a cosmetic defect into an
    approval nobody can act on.
    """
    try:
        summary = json.loads(row["summary_json"])
        if not isinstance(summary, dict):
            summary = {"raw": summary}
    except (TypeError, ValueError):
        summary = {}
    return Escalation(
        id=str(row["id"]),
        request_hash=str(row["request_hash"]),
        mandate_id=str(row["mandate_id"]),
        session_id=str(row["session_id"]),
        subject=str(row["subject"]),
        action=str(row["action"]),
        amount=int(row["amount"]),
        summary=summary,
        created_at=int(row["created_at"]),
        expires_at=int(row["expires_at"]),
        state=str(row["state"]),
        decided_at=None if row["decided_at"] is None else int(row["decided_at"]),
        decided_by=None if row["decided_by"] is None else str(row["decided_by"]),
    )
