"""Hostile pressure on the refundable-balance claim, and on what the engine does with it.

Two properties are load-bearing and neither is obvious from reading the happy path:

**The claim is atomic.** Not "usually atomic" or "atomic under the GIL" -- a single
statement whose ``WHERE`` reads the balance the same instant the row is written. Thirty-two
threads asking at once must sum to the balance and not a paise more.

**A timeout does not release.** A rail that stops answering may still have moved the money,
so the claim stays held. Handing it back would let the next request spend a rupee that is
already gone, and the conservative direction on a money path is the only defensible one.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from paynaka.audit import AuditChain
from paynaka.clock import FrozenClock
from paynaka.engine import PayNaka
from paynaka.gate import MoneyRequest, Verdict, reservation_key
from paynaka.mandate import IntentMandate, MandateSigner, generate_keypair
from paynaka.policy import Policy
from paynaka.rails.base import RailDeclined, RailError, RefundResult
from paynaka.rails.sim import SimRail
from paynaka.state import SqliteState

pytestmark = pytest.mark.adversarial

NOW = "2026-08-23 15:00"
CAPTURED = 100_000  # Rs 1,000


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock.at_ist(NOW)


@pytest.fixture
def state(clock: FrozenClock) -> SqliteState:
    s = SqliteState(":memory:", clock=clock)
    s.record_capture("pay_1", CAPTURED)
    s.record_return("pay_1")
    return s


# ====================================================================== atomicity


class TestConcurrentClaims:
    @pytest.mark.parametrize(
        ("slice_paise", "expected_winners"),
        [
            (CAPTURED, 1),  # all-or-nothing: exactly one may win
            (CAPTURED // 2, 2),
            (10_000, 10),
            (1, 32),  # everybody fits, and nobody should be spuriously refused
            (CAPTURED + 1, 0),  # nobody fits
        ],
    )
    def test_concurrent_claims_sum_to_the_balance_and_no_further(
        self, state: SqliteState, slice_paise: int, expected_winners: int
    ) -> None:
        threads = 32
        start = threading.Barrier(threads, timeout=10)
        won: list[bool] = []
        lock = threading.Lock()

        def claim(n: int) -> None:
            start.wait()
            ok = state.reserve_refund(f"k{n}", "pay_1", slice_paise)
            with lock:
                won.append(ok)

        with ThreadPoolExecutor(max_workers=threads) as pool:
            list(pool.map(claim, range(threads)))

        assert sum(won) == expected_winners
        assert state.held_amount("pay_1") == expected_winners * slice_paise
        assert state.held_amount("pay_1") <= CAPTURED

    def test_the_same_key_claimed_by_many_threads_wins_once(self, state: SqliteState) -> None:
        threads = 16
        start = threading.Barrier(threads, timeout=10)
        won: list[bool] = []
        lock = threading.Lock()

        def claim(_: int) -> None:
            start.wait()
            ok = state.reserve_refund("same", "pay_1", 1_000)
            with lock:
                won.append(ok)

        with ThreadPoolExecutor(max_workers=threads) as pool:
            list(pool.map(claim, range(threads)))

        assert sum(won) == 1
        assert state.held_amount("pay_1") == 1_000

    def test_claiming_and_settling_at_the_same_time_never_over_refunds(
        self, state: SqliteState
    ) -> None:
        """Settlement writes the ledger; claims read it. They must not overlap badly."""
        threads = 24
        start = threading.Barrier(threads, timeout=10)

        def churn(n: int) -> None:
            start.wait()
            key = f"k{n}"
            if state.reserve_refund(key, "pay_1", 5_000):
                state.settle_reservation(key, 5_000)

        with ThreadPoolExecutor(max_workers=threads) as pool:
            list(pool.map(churn, range(threads)))

        assert state.refunded_amount("pay_1") == CAPTURED
        assert state.refundable_amount("pay_1") == 0


# ====================================================================== the engine


def _naka(
    state: SqliteState, rail: object, clock: FrozenClock, *, max_total: int = CAPTURED
) -> tuple[PayNaka, object]:
    signer = MandateSigner(generate_keypair()[0])
    mandate = IntentMandate.create(
        clock=clock,
        subject="cust",
        session_id="sess",
        max_total=max_total,
        allowed_actions=("capture_payment", "create_refund"),
    )
    naka = PayNaka(
        rail=rail,  # type: ignore[arg-type]
        policy=Policy.from_yaml("policy.yaml"),
        state=state,
        audit=AuditChain(":memory:", clock=clock),
        verifier=signer.verifier(),
        clock=clock,
    )
    # The payment these tests refund has to have somewhere to have come from: the gate
    # walks payment -> order -> mandate before it looks at any balance. Recorded here
    # rather than in the `state` fixture because the mandate is built here, and the
    # subject on it is what the check compares against.
    state.record_order(
        "order_for_pay_1",
        mandate_id=mandate.mandate_id,
        subject=mandate.subject,
        session_id=mandate.session_id,
        clock=clock,
    )
    state.link_payment("pay_1", "order_for_pay_1", clock=clock)
    return naka, signer.sign(mandate)


def _refund(key: str, amount: int = 50_000) -> MoneyRequest:
    return MoneyRequest(
        action="create_refund",
        request_id=f"r_{key}",
        idempotency_key=key,
        amount=amount,
        payment_id="pay_1",
    )


class _Declining(SimRail):
    def create_refund(self, **kwargs: object) -> RefundResult:
        raise RailDeclined("the bank said no")


class _Timeout(SimRail):
    def create_refund(self, **kwargs: object) -> RefundResult:
        raise RailError("gateway timed out; outcome unknown")


class TestEngineResolvesTheClaim:
    def test_a_decline_gives_the_balance_back(self, state: SqliteState, clock: FrozenClock) -> None:
        naka, signed = _naka(state, _Declining(seed="d"), clock)
        request = _refund("k1")

        result = naka.execute(request, signed)  # type: ignore[arg-type]
        assert not result.executed
        assert state.reservation_state(reservation_key(request)) == "released"
        assert state.refundable_amount("pay_1") == CAPTURED

    def test_a_timeout_keeps_the_balance_held(self, state: SqliteState, clock: FrozenClock) -> None:
        """The money may have moved. Releasing would let it move a second time."""
        naka, signed = _naka(state, _Timeout(seed="t"), clock)
        request = _refund("k1")

        result = naka.execute(request, signed)  # type: ignore[arg-type]
        assert not result.executed
        assert "outcome unknown" in (result.error or "")
        assert state.reservation_state(reservation_key(request)) == "held"
        assert state.refundable_amount("pay_1") == CAPTURED - 50_000

    def test_an_unresolved_claim_reaches_the_reconciliation_queue(
        self, state: SqliteState, clock: FrozenClock
    ) -> None:
        naka, signed = _naka(state, _Timeout(seed="t"), clock)
        naka.execute(_refund("k1"), signed)  # type: ignore[arg-type]
        assert state.unresolved_reservations() == [("k1", "pay_1", 50_000)]

    def test_a_second_refund_cannot_spend_a_balance_a_timeout_is_holding(
        self, state: SqliteState, clock: FrozenClock
    ) -> None:
        """The whole reason a timeout keeps its claim, stated as a test."""
        naka, signed = _naka(state, _Timeout(seed="t"), clock)
        naka.execute(_refund("k1", 60_000), signed)  # type: ignore[arg-type]

        second = naka.execute(_refund("k2", 60_000), signed)  # type: ignore[arg-type]
        assert second.decision.verdict is Verdict.DENY
        assert second.decision.check_id == "refund.exceeds_capture"
        assert second.money_moved == 0

    def test_a_denied_request_claims_nothing(self, state: SqliteState, clock: FrozenClock) -> None:
        """A refusal that quietly held balance would be a denial-of-service on refunds."""
        naka, signed = _naka(state, SimRail(seed="s"), clock)
        over = naka.execute(_refund("k1", CAPTURED * 10), signed)  # type: ignore[arg-type]

        assert over.decision.verdict is Verdict.DENY
        assert state.held_amount("pay_1") == 0
        assert state.refundable_amount("pay_1") == CAPTURED

    def test_a_step_up_holds_nothing_while_it_waits_for_a_human(
        self, state: SqliteState, clock: FrozenClock
    ) -> None:
        """Policy steps up refunds above Rs 1,000. An approval can sit for a long time."""
        state.record_capture("pay_1", 200_000)  # Rs 3,000 captured in total
        naka, signed = _naka(state, SimRail(seed="s"), clock, max_total=300_000)
        request = _refund("k1", 150_000)  # Rs 1,500: over the band, inside the capture

        result = naka.execute(request, signed)  # type: ignore[arg-type]
        assert result.decision.verdict is Verdict.STEP_UP
        assert state.held_amount("pay_1") == 0
        assert state.reservation_state(reservation_key(request)) is None
        # And the balance is genuinely still available to somebody else meanwhile.
        assert state.refundable_amount("pay_1") == 300_000

    def test_a_replayed_refund_claims_nothing_a_second_time(
        self, state: SqliteState, clock: FrozenClock
    ) -> None:
        rail = SimRail(seed="s")
        naka, signed = _naka(state, rail, clock)

        # A capture the sim rail knows about, so the refund can actually succeed.
        order = rail.create_order(amount=CAPTURED, currency="INR", receipt="r", idempotency_key="o")
        payment = rail.pay_order(order_id=order.order_id, method="upi", idempotency_key="p")
        rail.capture_payment(payment_id=payment.payment_id, amount=CAPTURED, idempotency_key="c")
        state.record_capture(payment.payment_id, CAPTURED)
        state.record_return(payment.payment_id)
        # The paperwork a real order leaves behind. Without it the gate stops at
        # payment.unknown_origin before any of the reservation logic runs -- which is the
        # correct answer to "refund a payment that came from nowhere", and not the question
        # this test is asking.
        state.record_order(
            order.order_id,
            mandate_id=signed.mandate.mandate_id,
            subject=signed.mandate.subject,
            session_id=signed.mandate.session_id,
            clock=clock,
        )
        state.link_payment(payment.payment_id, order.order_id, clock=clock)

        request = MoneyRequest(
            action="create_refund",
            request_id="r1",
            idempotency_key="k1",
            amount=50_000,
            payment_id=payment.payment_id,
        )
        first = naka.execute(request, signed)  # type: ignore[arg-type]
        assert first.executed

        again = naka.execute(request, signed)  # type: ignore[arg-type]
        assert again.decision.replayed
        assert again.money_moved == 0
        assert state.held_amount(payment.payment_id) == 0
        assert state.refunded_amount(payment.payment_id) == 50_000


class TestNoModelInTheGate:
    def test_the_reservation_did_not_smuggle_an_import_in(self) -> None:
        """The claim on camera: gate.py imports no LLM SDK. Re-asserted after a change."""
        import ast
        from pathlib import Path

        tree = ast.parse(Path("paynaka/gate.py").read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])

        assert not imported & {"anthropic", "openai", "mcp", "transformers", "torch"}
