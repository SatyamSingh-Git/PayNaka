"""Denial of wallet: bounding an attacker who cannot move money but can burn it.

A refusal costs PayNaka microseconds and costs whoever is driving the agent a full model
turn. That asymmetry runs the wrong way: an attacker who can keep an agent looping against
a wall spends nothing and drains somebody else's budget, and ``max_turns`` bounds one run
rather than an adversary who can start many.

The breaker does not make the attack free to defend, and no test here pretends otherwise.
The turns before it trips are spent. What changes is that the number of them is a
configured constant instead of however long the attacker feels like continuing.

The awkward half is tested too: a breaker on a money path locks out the legitimate session
along with the attacker, that is the correct direction for a fail-closed system, and it
must be recoverable by a human.
"""

from __future__ import annotations

import dataclasses

import pytest

from haat.runner import _fresh_stack
from paynaka.clock import FrozenClock
from paynaka.gate import LineItem, MoneyRequest, Verdict
from paynaka.mandate import IntentMandate
from paynaka.policy import CircuitBreaker, Policy, PolicyError
from paynaka.state import SqliteState, StateError

pytestmark = pytest.mark.adversarial

NOW = "2026-08-24 15:00"


def _stack(  # type: ignore[no-untyped-def]
    seed: str, breaker: CircuitBreaker | None = None, *, budget: int = 199_900
):
    naka, signer, _rail, clock = _fresh_stack(seed)
    if breaker is not None:
        naka.policy = dataclasses.replace(naka.policy, circuit_breaker=breaker)
    mandate = IntentMandate.create(
        clock=clock,
        subject="cust_1",
        session_id="sess_1",
        max_total=budget,
        allowed_skus=("ATTA-5KG",),
        allowed_destinations=("addr_home",),
        max_qty_per_sku=3,
        allowed_actions=("create_order",),
    )
    return naka, signer.sign(mandate), clock


def _refused(n: int) -> MoneyRequest:
    """A request the gate will always refuse: a SKU the mandate never authorised."""
    return MoneyRequest(
        action="create_order",
        request_id=f"r{n}",
        idempotency_key=f"k{n}",
        items=(LineItem(sku="GIFT-50K", qty=1, unit_paise=5_000_000),),
        destination="addr_home",
    )


def _allowed(n: int) -> MoneyRequest:
    return MoneyRequest(
        action="create_order",
        request_id=f"ok{n}",
        idempotency_key=f"ok{n}",
        items=(LineItem(sku="ATTA-5KG", qty=1, unit_paise=49_900),),
        destination="addr_home",
    )


# ====================================================================== the counter


class TestTheCounter:
    @pytest.fixture
    def state(self) -> SqliteState:
        return SqliteState(":memory:", clock=FrozenClock.at_ist(NOW))

    def test_it_counts_up(self, state: SqliteState) -> None:
        assert [state.bump_denial("s") for _ in range(3)] == [1, 2, 3]

    def test_scopes_are_independent(self, state: SqliteState) -> None:
        state.bump_denial("session:a")
        state.bump_denial("session:a")
        state.bump_denial("session:b")
        clock = FrozenClock.at_ist(NOW)
        assert state.denial_count("session:a", clock.epoch()) == 2
        assert state.denial_count("session:b", clock.epoch()) == 1

    def test_an_unseen_scope_is_zero(self, state: SqliteState) -> None:
        assert state.denial_count("never", FrozenClock.at_ist(NOW).epoch()) == 0

    def test_the_window_is_an_ist_day(self, state: SqliteState) -> None:
        """23:00 and 01:00 the next morning are three hours apart and different days."""
        late = FrozenClock.at_ist("2026-08-24 23:00")
        early = FrozenClock.at_ist("2026-08-25 01:00")
        state.bump_denial("s", clock=late)
        state.bump_denial("s", clock=late)
        assert state.denial_count("s", late.epoch()) == 2
        assert state.denial_count("s", early.epoch()) == 0

    def test_an_operator_can_clear_it(self, state: SqliteState) -> None:
        state.bump_denial("s")
        state.clear_denials("s")
        assert state.denial_count("s", FrozenClock.at_ist(NOW).epoch()) == 0

    def test_an_empty_scope_is_refused(self, state: SqliteState) -> None:
        with pytest.raises(StateError, match="scope"):
            state.bump_denial("")

    def test_concurrent_increments_do_not_lose_any(self, state: SqliteState) -> None:
        """A breaker that undercounts under load fails to trip exactly when it matters."""
        import threading
        from concurrent.futures import ThreadPoolExecutor

        start = threading.Barrier(24, timeout=10)

        def hammer(_: int) -> None:
            start.wait()
            state.bump_denial("hot")

        with ThreadPoolExecutor(max_workers=24) as pool:
            list(pool.map(hammer, range(24)))

        assert state.denial_count("hot", FrozenClock.at_ist(NOW).epoch()) == 24


# ====================================================================== the breaker


class TestItTrips:
    def test_authority_is_withdrawn_after_the_configured_number(self) -> None:
        naka, signed, _clock = _stack("trip", CircuitBreaker(denials_per_session=5))
        for i in range(5):
            assert naka.execute(_refused(i), signed).decision.check_id != "revoked"
        assert naka.execute(_refused(99), signed).decision.check_id == "revoked"

    def test_the_loop_is_bounded_rather_than_endless(self) -> None:
        """The claim, stated as arithmetic: 200 attempts cost the attacker 5 real checks."""
        naka, signed, _clock = _stack("bounded", CircuitBreaker(denials_per_session=5))
        checks = [naka.execute(_refused(i), signed).decision.check_id for i in range(200)]
        substantive = [c for c in checks if c != "revoked"]
        assert len(substantive) == 5
        assert checks.count("revoked") == 195

    def test_tripping_is_recorded_on_the_chain(self) -> None:
        naka, signed, _clock = _stack("recorded", CircuitBreaker(denials_per_session=3))
        for i in range(6):
            naka.execute(_refused(i), signed)

        tripped = [
            r.payload
            for r in naka.audit.records(limit=200)
            if r.payload.get("kind") == "circuit.tripped"
        ]
        assert len(tripped) == 1
        assert tripped[0]["scope"] == "session"
        assert tripped[0]["limit"] == 3

    def test_it_trips_exactly_once(self) -> None:
        """A revoked session hammering the wall must not keep re-announcing itself."""
        naka, signed, _clock = _stack("once", CircuitBreaker(denials_per_session=2))
        for i in range(40):
            naka.execute(_refused(i), signed)
        kinds = [r.payload.get("kind") for r in naka.audit.records(limit=400)]
        assert kinds.count("circuit.tripped") == 1

    def test_the_subject_breaker_catches_an_attacker_who_rotates_sessions(self) -> None:
        """Per-session alone is defeated by a new session; the subject bound is not."""
        naka, _signed, clock = _stack(
            "rotate", CircuitBreaker(denials_per_session=3, denials_per_subject=7)
        )
        from paynaka.mandate import MandateSigner, generate_keypair

        signer = MandateSigner(generate_keypair()[0])
        naka.verifier = signer.verifier()

        seen: list[str | None] = []
        for session in range(6):
            mandate = IntentMandate.create(
                clock=clock,
                subject="cust_1",  # the same shopper throughout
                session_id=f"sess_{session}",  # a fresh session every time
                max_total=199_900,
                allowed_skus=("ATTA-5KG",),
                allowed_destinations=("addr_home",),
                max_qty_per_sku=3,
                allowed_actions=("create_order",),
            )
            signed = signer.sign(mandate)
            for attempt in range(2):
                seen.append(
                    naka.execute(_refused(session * 10 + attempt), signed).decision.check_id
                )

        assert "revoked" in seen, "rotating sessions must not reset the bound"
        assert naka.state.is_revoked("cust_1")


class TestItDoesNotTripOnHonestTraffic:
    def test_approvals_are_never_counted(self) -> None:
        # Budgeted for ten purchases on purpose. `max_total` is cumulative now, so a
        # mandate sized for one order would exhaust itself here and the test would be
        # measuring mandate authority instead of the breaker it is named after.
        naka, signed, _clock = _stack(
            "approvals", CircuitBreaker(denials_per_session=3), budget=10 * 199_900
        )
        for i in range(10):
            result = naka.execute(_allowed(i), signed)
            assert result.decision.verdict is Verdict.ALLOW
        assert not naka.state.is_revoked("sess_1")

    def test_a_replayed_request_is_not_counted(self) -> None:
        """A duplicate webhook is not an attack, and must not spend the budget."""
        naka, signed, _clock = _stack("replay", CircuitBreaker(denials_per_session=3))
        for _ in range(12):
            naka.execute(_allowed(1), signed)
        assert not naka.state.is_revoked("sess_1")

    def test_a_step_up_is_not_counted(self) -> None:
        """Waiting for a human to approve something is not being refused."""
        naka, signed, clock = _stack("stepup", CircuitBreaker(denials_per_session=2))
        mandate = IntentMandate.create(
            clock=clock,
            subject="cust_1",
            session_id="sess_1",
            max_total=400_000,
            allowed_skus=("ATTA-5KG",),
            allowed_destinations=("addr_home",),
            max_qty_per_sku=3,
            allowed_actions=("create_order",),
        )
        from paynaka.mandate import MandateSigner, generate_keypair

        signer = MandateSigner(generate_keypair()[0])
        naka.verifier = signer.verifier()
        signed = signer.sign(mandate)

        for i in range(8):
            result = naka.execute(
                MoneyRequest(
                    action="create_order",
                    request_id=f"s{i}",
                    idempotency_key=f"s{i}",
                    items=(LineItem(sku="ATTA-5KG", qty=1, unit_paise=300_000),),
                    destination="addr_home",
                ),
                signed,
            )
            assert result.decision.verdict is Verdict.STEP_UP
        assert not naka.state.is_revoked("sess_1")

    def test_a_disabled_breaker_counts_nothing(self) -> None:
        naka, signed, _clock = _stack("off", CircuitBreaker(enabled=False))
        for i in range(50):
            naka.execute(_refused(i), signed)
        assert not naka.state.is_revoked("sess_1")


class TestTheAwkwardHalf:
    def test_the_legitimate_session_is_locked_out_too(self) -> None:
        """Not a bug. It is what fail-closed means, and pretending otherwise is worse.

        An attacker who can induce refusals on a session can end that session. The
        alternative is a session that can be made to burn budget indefinitely, and on a
        money path the conservative direction is the defensible one.
        """
        naka, signed, _clock = _stack("lockout", CircuitBreaker(denials_per_session=3))
        for i in range(4):
            naka.execute(_refused(i), signed)

        honest = naka.execute(_allowed(1), signed)
        assert honest.decision.check_id == "revoked"
        assert honest.money_moved == 0

    def test_a_human_can_put_it_back(self) -> None:
        """Recoverable, and only by somebody with access to the state, never by the agent."""
        naka, signed, _clock = _stack("recover", CircuitBreaker(denials_per_session=3))
        for i in range(4):
            naka.execute(_refused(i), signed)
        assert naka.execute(_allowed(1), signed).decision.check_id == "revoked"

        naka.state.unrevoke("sess_1")
        naka.state.clear_denials("session:sess_1")

        recovered = naka.execute(_allowed(2), signed)
        assert recovered.decision.verdict is Verdict.ALLOW
        assert recovered.money_moved == 49_900

    def test_clearing_the_revocation_alone_is_not_enough(self) -> None:
        """The counter is still at the limit, so the next refusal trips it straight back.

        Deliberate: an operator who unrevokes without looking at why has not fixed
        anything, and the breaker should not quietly pretend they did.
        """
        naka, signed, _clock = _stack("half", CircuitBreaker(denials_per_session=3))
        for i in range(4):
            naka.execute(_refused(i), signed)
        naka.state.unrevoke("sess_1")

        naka.execute(_refused(99), signed)
        assert naka.state.is_revoked("sess_1")


class TestConfiguration:
    def test_the_default_policy_has_a_breaker(self) -> None:
        breaker = Policy.from_yaml("policy.yaml").circuit_breaker
        assert breaker.enabled
        assert breaker.denials_per_session >= 1

    @pytest.mark.parametrize("value", [0, -1])
    def test_a_budget_below_one_is_refused(self, value: int) -> None:
        with pytest.raises(PolicyError, match="at least 1"):
            CircuitBreaker(denials_per_session=value)

    @pytest.mark.parametrize("value", [1.5, "5", True])
    def test_a_budget_must_be_an_int(self, value: object) -> None:
        with pytest.raises(PolicyError, match="must be int"):
            CircuitBreaker(denials_per_session=value)  # type: ignore[arg-type]

    def test_a_subject_bound_below_the_session_bound_is_a_typo(self) -> None:
        """The wider bound would trip first and the narrower one would be unreachable."""
        with pytest.raises(PolicyError, match="below"):
            CircuitBreaker(denials_per_session=20, denials_per_subject=5)

    def test_an_unknown_key_is_a_startup_failure(self) -> None:
        with pytest.raises(PolicyError, match="unknown key"):
            Policy.from_text("version: 1\nmerchant: m\ncircuit_breaker:\n  denials_per_sesion: 5\n")
