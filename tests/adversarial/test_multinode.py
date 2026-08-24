"""Two nodes, one database. Where the single-process assumption actually ends.

"It uses SQLite, so it is single-node" is the easy thing to say and it is not what the code
does. Every claim in ``paynaka/state.py`` is a *single* atomic statement -- an ``INSERT``
guarded by a ``UNIQUE`` constraint, an ``UPDATE`` guarded on the current state, an upsert.
The ``SELECT`` that follows only reads back what was already decided. SQLite in WAL mode
serialises writers across connections, so those guarantees do not depend on the process
boundary at all.

This file measures that instead of asserting it. Each test builds **two independent
``SqliteState`` objects over the same file** and hammers them concurrently. Two objects
means two connections, no shared Python state, and -- crucially -- two different
``threading.RLock`` instances, so the in-process lock cannot be what saves them. Whatever
holds here is being held by SQLite.

**What a separate connection does and does not model.** A second connection is what a
second *process* has: its own transaction state, its own cache, taking the same file locks
through the same code path. It is not a second *host*, and that is exactly the boundary
this file exists to draw. Two nodes sharing a filesystem are covered by what follows. Two
nodes that cannot see each other's storage share no state at all, and none of it holds --
which is a deployment fact, not a code fix, and `docs/ARCHITECTURE.md` says so.
"""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from paynaka.clock import FrozenClock
from paynaka.state import SqliteState

pytestmark = pytest.mark.adversarial

#: Enough concurrency to lose a race that exists, few enough that the suite stays quick.
RACERS = 24


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock.at_ist("2026-08-23 15:00")


@pytest.fixture
def shared(tmp_path: Path, clock: FrozenClock) -> Iterator[tuple[SqliteState, SqliteState]]:
    """Two nodes. Same file, separate connections, separate locks."""
    db = tmp_path / "shared.db"
    node_a = SqliteState(str(db), clock=clock)
    node_b = SqliteState(str(db), clock=clock)
    assert node_a._lock is not node_b._lock
    try:
        yield node_a, node_b
    finally:
        node_a.close()
        node_b.close()


def _race(work: list) -> list:  # type: ignore[type-arg]
    """Run every callable at once and return the results in order."""
    with ThreadPoolExecutor(max_workers=RACERS) as pool:
        return list(pool.map(lambda fn: fn(), work))


class TestNoncesAreSpentOnceAcrossNodes:
    def test_only_one_node_consumes_a_nonce(self, shared: tuple[SqliteState, SqliteState]) -> None:
        """The double-spend that a read-then-write would allow. Both nodes see an unspent
        nonce, both proceed, and the shopper's single-use authority is used twice."""
        a, b = shared
        winners = _race(
            [
                lambda i=i: (a if i % 2 else b).consume_nonce("nonce_1", "mnd_1")
                for i in range(RACERS)
            ]
        )
        assert sum(winners) == 1

    def test_the_loser_sees_it_spent(self, shared: tuple[SqliteState, SqliteState]) -> None:
        a, b = shared
        assert a.consume_nonce("nonce_2", "mnd_1") is True
        assert b.nonce_spent("nonce_2") is True
        assert b.consume_nonce("nonce_2", "mnd_1") is False


class TestIdempotencyHoldsAcrossNodes:
    def test_only_one_node_claims_a_key(self, shared: tuple[SqliteState, SqliteState]) -> None:
        """Two claims means two rail calls means one order charged twice."""
        a, b = shared
        results = _race(
            [
                lambda i=i: (a if i % 2 else b).claim_idempotency("idem_1", "hash_1", "{}")
                for i in range(RACERS)
            ]
        )
        assert sum(1 for r in results if r is None) == 1

    def test_the_losers_all_see_the_same_winning_record(
        self, shared: tuple[SqliteState, SqliteState]
    ) -> None:
        """A loser has to be able to replay the original result, so it must be able to read
        it -- not merely be told no."""
        a, b = shared
        _race(
            [
                lambda i=i: (a if i % 2 else b).claim_idempotency("idem_2", "hash_2", '{"ok":1}')
                for i in range(RACERS)
            ]
        )
        seen = {
            node.lookup_idempotency("idem_2").request_hash  # type: ignore[union-attr]
            for node in (a, b)
        }
        assert seen == {"hash_2"}


class TestTheRefundableBalanceCannotBeOverclaimedAcrossNodes:
    def test_concurrent_reservations_respect_one_balance(
        self, shared: tuple[SqliteState, SqliteState]
    ) -> None:
        """The defect the chaos harness found in-process, asked again across nodes: twenty
        refunds on one payment, and the gate approved all twenty because the balance was
        read and the ledger written separately."""
        a, b = shared
        a.record_capture("pay_1", 100_000)
        claimed = _race(
            [
                lambda i=i: (a if i % 2 else b).reserve_refund(f"key_{i}", "pay_1", 25_000)
                for i in range(RACERS)
            ]
        )
        assert sum(claimed) == 4  # 100,000 / 25,000, and not one more
        assert a.held_amount("pay_1") == 100_000
        assert b.refundable_amount("pay_1") == 0

    def test_a_claim_beyond_the_balance_is_refused_from_either_node(
        self, shared: tuple[SqliteState, SqliteState]
    ) -> None:
        a, b = shared
        a.record_capture("pay_2", 50_000)
        assert b.reserve_refund("k_ok", "pay_2", 50_000) is True
        assert a.reserve_refund("k_over", "pay_2", 1) is False


class TestAnApprovalIsSpentOnceAcrossNodes:
    def test_two_nodes_cannot_both_release_one_step_up(
        self, shared: tuple[SqliteState, SqliteState]
    ) -> None:
        """An approval is a capability to move money. Spent twice, it is money nobody
        agreed to twice -- and the agent retrying against two nodes is the ordinary way
        that would happen, not an attack."""
        a, b = shared
        a.open_escalation(
            escalation_id="esc_1",
            request_hash="rh_1",
            mandate_id="m",
            session_id="s",
            subject="c",
            action="create_order",
            amount=350_000,
            summary={},
            timeout_seconds=300,
        )
        assert b.decide_escalation("esc_1", approve=True, by="ops") == "approved"

        spent = _race(
            [lambda i=i: (a if i % 2 else b).consume_approval("rh_1") for i in range(RACERS)]
        )
        assert sum(1 for s in spent if s == "esc_1") == 1
        assert sum(1 for s in spent if s is None) == RACERS - 1

    def test_only_one_answer_lands_when_two_approvers_race(
        self, shared: tuple[SqliteState, SqliteState]
    ) -> None:
        """Two operators clicking at once on two nodes. The first answer is the answer."""
        a, b = shared
        a.open_escalation(
            escalation_id="esc_2",
            request_hash="rh_2",
            mandate_id="m",
            session_id="s",
            subject="c",
            action="create_order",
            amount=350_000,
            summary={},
            timeout_seconds=300,
        )
        answers = _race(
            [
                lambda i=i: (a if i % 2 else b).decide_escalation(
                    "esc_2", approve=i % 2 == 0, by=f"ops_{i}"
                )
                for i in range(RACERS)
            ]
        )
        assert sum(1 for answer in answers if answer is not None) == 1


class TestTheBreakerCountsAcrossNodes:
    def test_denials_on_two_nodes_add_up_to_one_total(
        self, shared: tuple[SqliteState, SqliteState]
    ) -> None:
        """A breaker that counts per node needs N times the denials to trip, which is the
        same as not having a breaker on a fleet of N."""
        a, b = shared
        _race([lambda i=i: (a if i % 2 else b).bump_denial("session:s1") for i in range(RACERS)])
        assert a.denial_count("session:s1", a._now(None)) == RACERS

    def test_the_returned_running_total_never_undercounts(
        self, shared: tuple[SqliteState, SqliteState]
    ) -> None:
        """The upsert is atomic; the read-back after it can see a *higher* value if another
        node incremented in between. That is the safe direction -- a breaker that overshoots
        trips early, and one that undercounts fails to trip when it matters most."""
        a, b = shared
        totals = _race(
            [lambda i=i: (a if i % 2 else b).bump_denial("session:s2") for i in range(RACERS)]
        )
        assert max(totals) == RACERS
        assert min(totals) >= 1

    def test_revocation_on_one_node_is_seen_by_the_other(
        self, shared: tuple[SqliteState, SqliteState]
    ) -> None:
        """Withdrawing authority that only one node honours is not withdrawing authority."""
        a, b = shared
        assert b.is_revoked("sess_x") is False
        a.revoke("sess_x")
        assert b.is_revoked("sess_x") is True
        b.unrevoke("sess_x")
        assert a.is_revoked("sess_x") is False

    def test_the_global_kill_switch_reaches_every_node(
        self, shared: tuple[SqliteState, SqliteState]
    ) -> None:
        a, b = shared
        a.revoke("*")
        assert b.is_revoked("anything-at-all") is True


class TestWhatThisDoesNotCover:
    def test_two_nodes_with_separate_files_share_nothing(
        self, tmp_path: Path, clock: FrozenClock
    ) -> None:
        """The boundary, asserted so it is not mistaken for a bug later.

        Nodes that cannot see each other's storage share no state, and every guarantee
        above evaporates: the same nonce is spendable on both. This is not something the
        code can fix -- it is the deployment telling you it has two checkpoints, not one.
        """
        first = SqliteState(str(tmp_path / "a.db"), clock=clock)
        second = SqliteState(str(tmp_path / "b.db"), clock=clock)
        try:
            assert first.consume_nonce("n", "m") is True
            assert second.consume_nonce("n", "m") is True  # spent twice, by design of the setup
        finally:
            first.close()
            second.close()

    def test_in_memory_state_is_private_to_its_own_instance(self, clock: FrozenClock) -> None:
        """``:memory:`` is per-connection, so two in-memory nodes are always two
        checkpoints. The service uses it for the demo; a deployment must not."""
        first = SqliteState(":memory:", clock=clock)
        second = SqliteState(":memory:", clock=clock)
        try:
            assert first.consume_nonce("n", "m") is True
            assert second.consume_nonce("n", "m") is True
        finally:
            first.close()
            second.close()
