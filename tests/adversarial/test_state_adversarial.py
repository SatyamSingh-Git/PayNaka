"""Adversarial tests for paynaka.state.

State is where replay defence actually lives. The mandate says *what* is allowed; state
says *whether it has already happened*. Two failure shapes matter most:

**Races.** A read-then-write nonce check has a window in which two concurrent requests
both observe "unused". That window is a double-spend, and it will not show up in a
single-threaded test -- so these tests use real threads and real contention.

**Ledger drift.** Refunded must never exceed captured. The gate is what enforces that,
but the ledger must also refuse to silently report a nonsensical position if it ever does.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from paynaka.clock import FrozenClock
from paynaka.state import SqliteState, StateError

pytestmark = pytest.mark.adversarial


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock.at_ist("2026-08-23 11:30")


@pytest.fixture
def state(clock: FrozenClock) -> SqliteState:
    with SqliteState(":memory:", clock=clock) as st:
        yield st


class TestNonceReplay:
    def test_second_use_is_refused(self, state: SqliteState) -> None:
        assert state.consume_nonce("n1", "mnd_1") is True
        assert state.consume_nonce("n1", "mnd_1") is False

    def test_replay_refused_even_under_a_different_mandate(self, state: SqliteState) -> None:
        """A nonce is global. Re-presenting it under a new mandate id must not refresh it."""
        assert state.consume_nonce("n1", "mnd_1") is True
        assert state.consume_nonce("n1", "mnd_attacker") is False

    @pytest.mark.parametrize("workers", [2, 8, 32])
    def test_exactly_one_winner_under_concurrency(self, workers: int, clock: FrozenClock) -> None:
        """The double-spend test. Only one of N racing threads may win the same nonce."""
        with SqliteState(":memory:", clock=clock) as state:
            barrier = threading.Barrier(workers)

            def attempt() -> bool:
                barrier.wait()  # maximise real contention rather than hoping for it
                return state.consume_nonce("contended", "mnd_1")

            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(lambda _: attempt(), range(workers)))

        assert sum(results) == 1, f"{sum(results)} threads won the same nonce"

    @pytest.mark.parametrize("nonce", ["", None, 42, b"bytes", []])
    def test_malformed_nonce_refused(self, state: SqliteState, nonce: object) -> None:
        with pytest.raises((StateError, TypeError, sqlite3.InterfaceError)):
            state.consume_nonce(nonce, "mnd_1")  # type: ignore[arg-type]

    def test_nonces_are_case_and_whitespace_sensitive(self, state: SqliteState) -> None:
        """Near-miss variants must be distinct, not silently folded onto the same row."""
        assert state.consume_nonce("Abc", "m") is True
        assert state.consume_nonce("abc", "m") is True
        assert state.consume_nonce("abc ", "m") is True
        assert state.consume_nonce("abc", "m") is False


class TestIdempotency:
    def test_first_claim_wins(self, state: SqliteState) -> None:
        assert state.claim_idempotency("k1", "h1", '{"ok":true}') is None

    def test_second_claim_returns_the_original(self, state: SqliteState) -> None:
        state.claim_idempotency("k1", "h1", '{"id":"pay_1"}')
        existing = state.claim_idempotency("k1", "h1", '{"id":"pay_2"}')
        assert existing is not None
        assert json.loads(existing.result_json)["id"] == "pay_1", "the later result overwrote"

    def test_same_key_different_request_is_detectable(self, state: SqliteState) -> None:
        """Key reuse with a different body is the substitution attack. Surface it."""
        state.claim_idempotency("k1", "hash_of_1999", "{}")
        existing = state.claim_idempotency("k1", "hash_of_52000", "{}")
        assert existing is not None
        assert existing.request_hash == "hash_of_1999"

    @pytest.mark.parametrize("workers", [4, 16])
    def test_exactly_one_winner_under_concurrency(self, workers: int, clock: FrozenClock) -> None:
        with SqliteState(":memory:", clock=clock) as state:
            barrier = threading.Barrier(workers)

            def attempt(i: int) -> bool:
                barrier.wait()
                return state.claim_idempotency("k", "h", f'{{"n":{i}}}') is None

            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(attempt, range(workers)))

        assert sum(results) == 1


class TestLedgerIntegrity:
    def test_captures_and_refunds_accumulate(self, state: SqliteState) -> None:
        state.record_capture("pay_1", 199900)
        state.record_refund("pay_1", 50000)
        state.record_refund("pay_1", 50000)
        assert state.captured_amount("pay_1") == 199900
        assert state.refunded_amount("pay_1") == 100000
        assert state.refundable_amount("pay_1") == 99900

    def test_ledgers_do_not_bleed_between_payments(self, state: SqliteState) -> None:
        state.record_capture("pay_1", 199900)
        state.record_capture("pay_2", 5_200_000)
        assert state.captured_amount("pay_1") == 199900
        assert state.refundable_amount("pay_2") == 5_200_000

    def test_unknown_payment_is_zero_not_an_error(self, state: SqliteState) -> None:
        assert state.captured_amount("pay_nope") == 0
        assert state.refundable_amount("pay_nope") == 0

    @pytest.mark.parametrize("amount", [0, -1, -199900])
    def test_non_positive_ledger_entries_refused(self, state: SqliteState, amount: int) -> None:
        with pytest.raises(StateError):
            state.record_refund("pay_1", amount)

    @pytest.mark.parametrize("amount", [1.5, "199900", True, None])
    def test_non_int_amounts_refused(self, state: SqliteState, amount: object) -> None:
        with pytest.raises(StateError):
            state.record_capture("pay_1", amount)  # type: ignore[arg-type]

    def test_absurd_amount_refused(self, state: SqliteState) -> None:
        with pytest.raises(StateError):
            state.record_capture("pay_1", 10**20)

    def test_over_refund_is_surfaced_not_hidden(self, state: SqliteState) -> None:
        """The gate prevents this. If it ever gets through, the ledger must shout."""
        state.record_capture("pay_1", 100000)
        state.record_refund("pay_1", 150000)  # only reachable by bypassing the gate
        with pytest.raises(StateError, match="invariant violated"):
            state.refundable_amount("pay_1")

    def test_empty_payment_id_refused(self, state: SqliteState) -> None:
        with pytest.raises(StateError, match="payment_id"):
            state.record_capture("", 1000)


class TestDailyBoundaries:
    def test_refund_cap_buckets_by_ist_day_not_utc(self, clock: FrozenClock) -> None:
        """23:00 IST and 01:00 IST next day are 2h apart but must be different days.

        In UTC they are 17:30 and 19:30 on the *same* date, so a UTC-bucketed cap would
        wrongly merge them and let a full extra day's refunds through.
        """
        with SqliteState(":memory:", clock=clock) as state:
            late = FrozenClock.at_ist("2026-08-23 23:00")
            early = FrozenClock.at_ist("2026-08-24 01:00")

            state.record_refund("pay_1", 100000, clock=late)
            state.record_refund("pay_2", 100000, clock=early)

            assert state.daily_refund_total(late.epoch()) == 100000
            assert state.daily_refund_total(early.epoch()) == 100000

    def test_retry_counter_resets_on_the_ist_day_boundary(self, clock: FrozenClock) -> None:
        with SqliteState(":memory:", clock=clock) as state:
            day1 = FrozenClock.at_ist("2026-08-23 23:59")
            day2 = FrozenClock.at_ist("2026-08-24 00:01")

            assert state.bump_retry("mnd_1", clock=day1) == 1
            assert state.bump_retry("mnd_1", clock=day1) == 2
            assert state.bump_retry("mnd_1", clock=day1) == 3
            assert state.retry_count("mnd_1", day1.epoch()) == 3

            assert state.bump_retry("mnd_1", clock=day2) == 1, "counter did not reset"

    @pytest.mark.parametrize("workers", [4, 16])
    def test_retry_counter_does_not_lose_increments_under_races(
        self, workers: int, clock: FrozenClock
    ) -> None:
        """A read-modify-write counter would undercount here, letting extra retries past."""
        with SqliteState(":memory:", clock=clock) as state:
            barrier = threading.Barrier(workers)

            def bump(_: int) -> int:
                barrier.wait()
                return state.bump_retry("mnd_1")

            with ThreadPoolExecutor(max_workers=workers) as pool:
                list(pool.map(bump, range(workers)))

            assert state.retry_count("mnd_1", clock.epoch()) == workers

    def test_retry_scopes_are_independent(self, state: SqliteState) -> None:
        state.bump_retry("mnd_1")
        state.bump_retry("mnd_1")
        state.bump_retry("mnd_2")
        assert state.retry_count("mnd_1", state._now(None)) == 2
        assert state.retry_count("mnd_2", state._now(None)) == 1


class TestRevocation:
    def test_revoking_a_mandate_does_not_revoke_others(self, state: SqliteState) -> None:
        state.revoke("mnd_1")
        assert state.is_revoked("mnd_1") is True
        assert state.is_revoked("mnd_2") is False

    def test_global_kill_switch_revokes_everything(self, state: SqliteState) -> None:
        state.revoke("*")
        assert state.is_revoked("mnd_anything") is True
        assert state.is_revoked("sess_anything") is True

    def test_any_matching_scope_revokes(self, state: SqliteState) -> None:
        """A request carries both a mandate id and a session id; either may be killed."""
        state.revoke("sess_compromised")
        assert state.is_revoked("mnd_innocent", "sess_compromised") is True

    def test_revocation_is_idempotent(self, state: SqliteState) -> None:
        state.revoke("mnd_1")
        state.revoke("mnd_1")
        assert state.is_revoked("mnd_1") is True

    def test_unrevoke_is_scoped(self, state: SqliteState) -> None:
        state.revoke("mnd_1")
        state.revoke("*")
        state.unrevoke("mnd_1")
        assert state.is_revoked("mnd_1") is True, "global revocation must still apply"
        state.unrevoke("*")
        assert state.is_revoked("mnd_1") is False


class TestReturns:
    def test_absent_by_default(self, state: SqliteState) -> None:
        assert state.has_return("pay_1") is False

    def test_recorded_return_is_visible(self, state: SqliteState) -> None:
        state.record_return("pay_1")
        assert state.has_return("pay_1") is True

    def test_recording_twice_is_harmless(self, state: SqliteState) -> None:
        state.record_return("pay_1")
        state.record_return("pay_1")
        assert state.has_return("pay_1") is True


class TestInjection:
    @pytest.mark.parametrize(
        "payload",
        [
            "'; DROP TABLE ledger; --",
            '" OR "1"="1',
            "pay_1'); DELETE FROM nonces; --",
            "\x00truncated",
            "pay_1\n; DROP TABLE returns;",
        ],
    )
    def test_sql_injection_through_identifiers_is_inert(
        self, state: SqliteState, payload: str
    ) -> None:
        """Every statement is parameterised. Hostile identifiers are just odd strings."""
        state.record_capture(payload, 1000)
        state.record_return(payload)
        state.revoke(payload)
        assert state.captured_amount(payload) == 1000
        assert state.has_return(payload) is True
        # the tables the payloads try to drop are all still there and still correct
        assert state.captured_amount("pay_untouched") == 0
        assert state.consume_nonce("still-works", "m") is True


class TestClockDiscipline:
    def test_operations_require_a_clock(self) -> None:
        """No implicit datetime.now() anywhere -- an untimed write is refused."""
        with SqliteState(":memory:") as state, pytest.raises(StateError, match="clock"):
            state.consume_nonce("n1", "m1")

    def test_explicit_clock_overrides_the_default(self, clock: FrozenClock) -> None:
        with SqliteState(":memory:", clock=clock) as state:
            other = FrozenClock.at_ist("2026-01-01 00:00")
            state.record_refund("pay_1", 1000, clock=other)
            assert state.daily_refund_total(other.epoch()) == 1000
            assert state.daily_refund_total(clock.epoch()) == 0
