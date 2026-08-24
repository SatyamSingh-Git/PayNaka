"""Forward tests for the escalation store: does the approval machinery work at all?

The hostile half -- reuse, expiry, racing, self-approval -- lives in
``tests/adversarial/test_escalation.py`` and is much longer, which is the right ratio for a
mechanism whose whole job is to hand out a capability to move money. This file exists so
that those refusal tests are not all passing against a store that refuses everything.
"""

from __future__ import annotations

import pytest

from paynaka.clock import FrozenClock
from paynaka.state import Escalation, SqliteState, StateError


def open_one(
    state: SqliteState, *, request_hash: str = "hash_a", amount: int = 350_000
) -> Escalation:
    return state.open_escalation(
        escalation_id=f"esc_{request_hash}",
        request_hash=request_hash,
        mandate_id="mnd_1",
        session_id="sess_1",
        subject="cust_1",
        action="create_order",
        amount=amount,
        summary={"action": "create_order", "amount": amount},
        timeout_seconds=300,
    )


class TestOpening:
    def test_a_new_escalation_starts_pending(self, state: SqliteState) -> None:
        escalation = open_one(state)
        assert escalation.state == "pending"
        assert escalation.amount == 350_000
        assert escalation.decided_at is None
        assert escalation.decided_by is None

    def test_it_appears_in_the_pending_queue(self, state: SqliteState) -> None:
        opened = open_one(state)
        assert [e.id for e in state.pending_escalations()] == [opened.id]

    def test_the_summary_survives_the_round_trip(self, state: SqliteState) -> None:
        """It is what a human reads before deciding, so it has to arrive intact."""
        assert open_one(state).summary["action"] == "create_order"

    def test_two_different_requests_get_two_escalations(self, state: SqliteState) -> None:
        open_one(state, request_hash="hash_a")
        open_one(state, request_hash="hash_b")
        assert len(state.pending_escalations()) == 2

    def test_the_same_request_gets_the_same_escalation(self, state: SqliteState) -> None:
        """Idempotent on the request hash: a duplicate delivery must not queue a second
        approval for one request."""
        first = open_one(state, request_hash="hash_a")
        second = open_one(state, request_hash="hash_a")
        assert first.id == second.id
        assert len(state.pending_escalations()) == 1

    def test_it_is_retrievable_by_id(self, state: SqliteState) -> None:
        opened = open_one(state)
        fetched = state.escalation(opened.id)
        assert fetched is not None and fetched.id == opened.id

    def test_an_unknown_id_is_none_rather_than_an_error(self, state: SqliteState) -> None:
        assert state.escalation("esc_nope") is None

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"amount": 0}, "positive int paise"),
            ({"amount": -1}, "positive int paise"),
            ({"timeout_seconds": 0}, "timeout must be positive"),
        ],
    )
    def test_a_nonsensical_escalation_is_refused_at_the_door(
        self, state: SqliteState, kwargs: dict[str, int], message: str
    ) -> None:
        with pytest.raises(StateError, match=message):
            state.open_escalation(
                escalation_id="esc_bad",
                request_hash="hash_bad",
                mandate_id="m",
                session_id="s",
                subject="c",
                action="create_order",
                amount=kwargs.get("amount", 100),
                summary={},
                timeout_seconds=kwargs.get("timeout_seconds", 300),
            )


class TestDeciding:
    def test_approving_moves_it_to_approved_and_names_the_approver(
        self, state: SqliteState
    ) -> None:
        opened = open_one(state)
        assert state.decide_escalation(opened.id, approve=True, by="ops-anita") == "approved"
        record = state.escalation(opened.id)
        assert record is not None
        assert record.state == "approved"
        assert record.decided_by == "ops-anita"
        assert record.decided_at is not None

    def test_denying_moves_it_to_denied(self, state: SqliteState) -> None:
        opened = open_one(state)
        assert state.decide_escalation(opened.id, approve=False, by="ops-anita") == "denied"

    def test_a_decided_escalation_leaves_the_pending_queue(self, state: SqliteState) -> None:
        opened = open_one(state)
        state.decide_escalation(opened.id, approve=True, by="ops")
        assert state.pending_escalations() == []


class TestConsuming:
    def test_an_approval_is_spendable_by_its_own_request(self, state: SqliteState) -> None:
        opened = open_one(state, request_hash="hash_a")
        state.decide_escalation(opened.id, approve=True, by="ops")
        assert state.consume_approval("hash_a") == opened.id

    def test_spending_marks_it_consumed(self, state: SqliteState) -> None:
        opened = open_one(state, request_hash="hash_a")
        state.decide_escalation(opened.id, approve=True, by="ops")
        state.consume_approval("hash_a")
        record = state.escalation(opened.id)
        assert record is not None and record.state == "consumed"

    def test_nothing_is_spendable_before_anyone_approves(self, state: SqliteState) -> None:
        open_one(state, request_hash="hash_a")
        assert state.consume_approval("hash_a") is None

    def test_a_denied_escalation_is_not_spendable(self, state: SqliteState) -> None:
        opened = open_one(state, request_hash="hash_a")
        state.decide_escalation(opened.id, approve=False, by="ops")
        assert state.consume_approval("hash_a") is None

    def test_an_unknown_hash_spends_nothing(self, state: SqliteState) -> None:
        assert state.consume_approval("never-seen") is None


class TestExpiry:
    def test_an_unanswered_escalation_leaves_pending_and_appears_as_expired(
        self, state: SqliteState, clock: FrozenClock
    ) -> None:
        open_one(state)
        clock.advance(seconds=301)
        assert state.pending_escalations(clock=clock) == []
        assert len(state.expired_escalations(clock=clock)) == 1

    def test_expired_rows_are_surfaced_rather_than_deleted(
        self, state: SqliteState, clock: FrozenClock
    ) -> None:
        """ "Nobody answered in time" is a number an operator should watch going up, and a
        silently dropped row cannot be counted."""
        opened = open_one(state)
        clock.advance(seconds=301)
        assert [e.id for e in state.expired_escalations(clock=clock)] == [opened.id]
        assert state.escalation(opened.id) is not None

    def test_expiry_is_a_property_of_the_clock_not_of_the_column(
        self, state: SqliteState, clock: FrozenClock
    ) -> None:
        """The row still reads 'pending'. It has expired anyway, because nothing has to run
        for a deadline to pass."""
        opened = open_one(state)
        clock.advance(seconds=301)
        record = state.escalation(opened.id)
        assert record is not None
        assert record.state == "pending"
        assert record.is_expired(clock.epoch())
