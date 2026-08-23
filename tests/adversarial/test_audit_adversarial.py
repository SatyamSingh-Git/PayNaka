"""Adversarial tests for paynaka.audit.

An audit trail nobody can tamper with undetectably is the difference between evidence and
a log file. These tests take the role of someone with direct write access to the database
-- the strongest realistic insider -- and check that every edit they can make is visible
afterwards, and that verification names the right record.

The one thing the chain deliberately does *not* claim: it proves internal consistency,
not authenticity. An attacker who rewrites every row can also recompute every hash. The
test for that expectation is here too, stated as what it is rather than hidden.
"""

from __future__ import annotations

import itertools
import json
import sqlite3
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest

from paynaka.audit import GENESIS, AuditChain, AuditError
from paynaka.clock import FrozenClock

pytestmark = pytest.mark.adversarial


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock.at_ist("2026-08-23 11:30")


@pytest.fixture
def chain(tmp_path, clock: FrozenClock) -> Iterator[AuditChain]:
    with AuditChain(tmp_path / "audit.db", clock=clock) as c:
        yield c


def seed(chain: AuditChain, n: int = 5) -> None:
    for i in range(n):
        chain.append({"verdict": "ALLOW" if i % 2 else "DENY", "amount": 1000 * (i + 1)})


class TestTampering:
    def test_editing_a_payload_is_detected(self, chain: AuditChain) -> None:
        """The insider raises a recorded amount. The chain must notice."""
        seed(chain)
        with sqlite3.connect(chain._path) as conn:
            conn.execute(
                "UPDATE audit SET payload = ? WHERE seq = 3",
                (json.dumps({"verdict": "ALLOW", "amount": 5_200_000}),),
            )

        break_at = chain.verify()
        assert break_at is not None
        assert break_at.seq == 3
        assert break_at.kind == "payload tampered"

    def test_editing_the_verdict_is_detected(self, chain: AuditChain) -> None:
        """Turning a recorded DENY into an ALLOW after the fact."""
        seed(chain)
        with sqlite3.connect(chain._path) as conn:
            conn.execute(
                "UPDATE audit SET payload = ? WHERE seq = 1",
                (json.dumps({"verdict": "ALLOW", "amount": 1000}),),
            )
        assert chain.verify() is not None

    def test_deleting_a_record_is_detected(self, chain: AuditChain) -> None:
        """Removing the record of a blocked action."""
        seed(chain)
        with sqlite3.connect(chain._path) as conn:
            conn.execute("DELETE FROM audit WHERE seq = 3")

        break_at = chain.verify()
        assert break_at is not None
        assert break_at.seq == 4
        assert break_at.kind == "missing record"

    def test_deleting_the_tail_is_detected_only_by_the_published_head(
        self, chain: AuditChain
    ) -> None:
        """Truncation is the chain's blind spot, and the reason head() exists.

        Lopping records off the end leaves a shorter but internally consistent chain. An
        externally published head hash is what catches it, so this test asserts both
        halves: verify() is content, and the head has moved.
        """
        seed(chain)
        published_head = chain.head()

        with sqlite3.connect(chain._path) as conn:
            conn.execute("DELETE FROM audit WHERE seq >= 4")

        assert chain.verify() is None, "a truncated chain is internally consistent"
        assert chain.head() != published_head, "but the head no longer matches what we published"

    def test_reordering_records_is_detected(self, chain: AuditChain) -> None:
        seed(chain)
        with sqlite3.connect(chain._path) as conn:
            rows = conn.execute("SELECT seq, payload FROM audit ORDER BY seq").fetchall()
            conn.execute("UPDATE audit SET payload = ? WHERE seq = 2", (rows[2][1],))
            conn.execute("UPDATE audit SET payload = ? WHERE seq = 3", (rows[1][1],))
        assert chain.verify() is not None

    def test_rewriting_a_prev_hash_to_reconnect_a_gap_is_detected(self, chain: AuditChain) -> None:
        """A cleverer attacker: delete a record *and* patch the next one's prev_hash."""
        seed(chain)
        with sqlite3.connect(chain._path) as conn:
            target = conn.execute("SELECT hash FROM audit WHERE seq = 2").fetchone()[0]
            conn.execute("DELETE FROM audit WHERE seq = 3")
            conn.execute("UPDATE audit SET prev_hash = ? WHERE seq = 4", (target,))

        break_at = chain.verify()
        assert break_at is not None, "the gap in seq numbering is still visible"

    def test_a_naive_full_rewrite_is_caught_by_the_sequence_gap(self, chain: AuditChain) -> None:
        """DELETE-then-re-append does not reset AUTOINCREMENT, so the seq jumps.

        An attacker who wipes the table and replays plausible-looking records produces a
        chain whose first record is seq 6, not seq 1. verify() checks sequence continuity
        from 1, so the wipe is visible even though every hash links correctly.
        """
        seed(chain)
        with sqlite3.connect(chain._path) as conn:
            conn.execute("DELETE FROM audit")
        for i in range(5):
            chain.append({"verdict": "ALLOW", "amount": 1000 * (i + 1)})

        break_at = chain.verify()
        assert break_at is not None
        assert break_at.kind == "missing record"
        assert break_at.expected == "seq 1"

    def test_a_thorough_rewrite_is_honestly_not_detected(self, chain: AuditChain) -> None:
        """The stated limitation, pinned so nobody later claims more than the chain offers.

        An attacker who also resets sqlite_sequence recomputes a wholly valid chain from
        seq 1. Nothing internal to the database can reveal that -- only an externally
        published head hash can. This is said plainly in the module docstring and in
        THREATMODEL.md, and this test exists to keep that claim honest rather than to
        assert a defence we do not have.
        """
        seed(chain)
        original_head = chain.head()

        with sqlite3.connect(chain._path) as conn:
            conn.execute("DELETE FROM audit")
            conn.execute("DELETE FROM sqlite_sequence WHERE name = 'audit'")
        for i in range(5):
            chain.append({"verdict": "ALLOW", "amount": 1000 * (i + 1)})

        assert chain.verify() is None, "a thoroughly rewritten chain verifies -- by design"
        assert chain.head() != original_head, "which is exactly why the head must be published"


class TestChainIntegrity:
    def test_an_untouched_chain_verifies(self, chain: AuditChain) -> None:
        seed(chain, 50)
        assert chain.verify() is None

    def test_an_empty_chain_verifies(self, chain: AuditChain) -> None:
        assert chain.verify() is None
        assert chain.head() == GENESIS

    def test_first_record_chains_off_genesis(self, chain: AuditChain) -> None:
        record = chain.append({"first": True})
        assert record.prev_hash == GENESIS
        assert record.seq == 1

    def test_each_record_chains_off_its_predecessor(self, chain: AuditChain) -> None:
        seed(chain, 10)
        records = chain.records()
        for previous, current in itertools.pairwise(records):
            assert current.prev_hash == previous.hash

    def test_head_tracks_the_tip(self, chain: AuditChain) -> None:
        seed(chain, 3)
        assert chain.head() == chain.records()[-1].hash

    def test_identical_payloads_get_different_hashes(self, chain: AuditChain) -> None:
        """Position matters. Two identical records must not be interchangeable."""
        a = chain.append({"same": "payload"})
        b = chain.append({"same": "payload"})
        assert a.hash != b.hash

    def test_hash_covers_the_timestamp(self, tmp_path) -> None:
        """Backdating a record must break its hash."""
        early = FrozenClock.at_ist("2026-08-23 11:30")
        late = FrozenClock.at_ist("2026-08-23 23:30")
        with AuditChain(tmp_path / "a.db", clock=early) as chain:
            first = chain.append({"x": 1})
        with AuditChain(tmp_path / "b.db", clock=late) as chain:
            second = chain.append({"x": 1})
        assert first.hash != second.hash


class TestConcurrency:
    @pytest.mark.parametrize("workers", [4, 16])
    def test_concurrent_appends_do_not_fork_the_chain(self, workers: int, tmp_path, clock) -> None:
        """Two appends chaining off the same predecessor would silently fork the ledger."""
        with AuditChain(tmp_path / "audit.db", clock=clock) as chain:
            barrier = threading.Barrier(workers)

            def append(i: int) -> int:
                barrier.wait()
                return chain.append({"worker": i}).seq

            with ThreadPoolExecutor(max_workers=workers) as pool:
                seqs = list(pool.map(append, range(workers)))

            assert sorted(seqs) == list(range(1, workers + 1)), "sequence numbers collided"
            assert chain.verify() is None, "the chain forked under concurrency"
            assert len(chain) == workers


class TestWriteDiscipline:
    def test_appends_require_a_clock(self, tmp_path) -> None:
        with AuditChain(tmp_path / "a.db") as chain, pytest.raises(AuditError, match="clock"):
            chain.append({"x": 1})

    @pytest.mark.parametrize(
        "payload",
        [{"f": float("nan")}, {"f": float("inf")}, {"bad": {1, 2}}, {"obj": object()}],
    )
    def test_unserialisable_payloads_are_refused(self, chain: AuditChain, payload: dict) -> None:
        """Better to refuse the write than to store a record that cannot be re-hashed."""
        with pytest.raises(AuditError, match="JSON-serialisable"):
            chain.append(payload)

    def test_a_refused_write_does_not_advance_the_chain(self, chain: AuditChain) -> None:
        seed(chain, 3)
        head_before, len_before = chain.head(), len(chain)
        with pytest.raises(AuditError):
            chain.append({"f": float("nan")})
        assert chain.head() == head_before
        assert len(chain) == len_before

    def test_unicode_payloads_round_trip(self, chain: AuditChain) -> None:
        record = chain.append({"reason": "अस्वीकृत — ₹52,000 माँगा गया"})
        assert chain.records()[-1].payload == record.payload
        assert chain.verify() is None


class TestReading:
    def test_since_seq_pages_forward(self, chain: AuditChain) -> None:
        seed(chain, 10)
        assert [r.seq for r in chain.records(since_seq=7)] == [8, 9, 10]

    def test_limit_bounds_the_read(self, chain: AuditChain) -> None:
        seed(chain, 10)
        assert len(chain.records(limit=3)) == 3

    def test_records_come_back_in_order(self, chain: AuditChain) -> None:
        seed(chain, 20)
        seqs = [r.seq for r in chain.records()]
        assert seqs == sorted(seqs)
