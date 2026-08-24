"""Adversarial tests for observe mode -- a checkpoint deliberately not stopping things.

Observe mode is the one feature in this project whose *purpose* is to let a refused
request through, which makes it the one most able to become a hole. So the tests are
arranged around the three ways it could go wrong rather than around its happy path:

* **It must actually observe.** A shadow deployment that quietly blocks something has
  changed production behaviour, which is the one thing it promised not to do. If these
  tests passed against an enforcing engine, they would be worthless.
* **It must be impossible to mistake for enforcement.** The mode is on the result and on
  every audit record. An operator must never be able to read the chain later and conclude
  the checkpoint was enforcing when it was not.
* **It must not suspend the checks that are not authority judgments.** Signature
  verification and idempotency stay live in both modes. Declining to enforce an authority
  check means not stopping what would have happened anyway; declining to enforce
  idempotency would mean *causing* a double charge, and declining to authenticate would
  mean executing whatever an attacker put in the payload.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from paynaka.audit import AuditChain
from paynaka.clock import FrozenClock
from paynaka.engine import PayNaka
from paynaka.gate import Verdict
from paynaka.mandate import IntentMandate, MandateSigner, SignedMandate, generate_keypair
from paynaka.mode import Mode
from paynaka.rails.sim import SimRail
from paynaka.state import SqliteState
from tests.conftest import ATTACK, AUTHORISED, GIFT_CARD, order

pytestmark = pytest.mark.adversarial


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock.at_ist("2026-08-23 15:00")


@pytest.fixture
def signer() -> MandateSigner:
    return MandateSigner(generate_keypair()[0])


@pytest.fixture
def audit(clock: FrozenClock) -> Iterator[AuditChain]:
    with AuditChain(":memory:", clock=clock) as chain:
        yield chain


def _engine(
    mode: Mode,
    *,
    rail: SimRail,
    policy: object,
    state: SqliteState,
    audit: AuditChain,
    signer: MandateSigner,
    clock: FrozenClock,
) -> PayNaka:
    return PayNaka(
        rail=rail,
        policy=policy,  # type: ignore[arg-type]
        state=state,
        audit=audit,
        verifier=signer.verifier(),
        clock=clock,
        mode=mode,
    )


@pytest.fixture
def observing(policy, state, audit, signer, clock) -> PayNaka:
    return _engine(
        Mode.OBSERVE,
        rail=SimRail(seed="observe"),
        policy=policy,
        state=state,
        audit=audit,
        signer=signer,
        clock=clock,
    )


@pytest.fixture
def enforcing(policy, state, audit, signer, clock) -> PayNaka:
    return _engine(
        Mode.ENFORCE,
        rail=SimRail(seed="enforce"),
        policy=policy,
        state=state,
        audit=audit,
        signer=signer,
        clock=clock,
    )


@pytest.fixture
def signed(signer: MandateSigner, mandate: IntentMandate) -> SignedMandate:
    return signer.sign(mandate)


#: An order the gate refuses: a SKU that is not in the frozen intent, for ₹52,000 against
#: a ₹1,999 mandate. The same request drives both modes throughout this file.
def _denied_order():  # type: ignore[no-untyped-def]
    return order(sku=GIFT_CARD, unit=ATTACK, key="idem_observe")


# ============================================================== it must actually observe
class TestItActuallyObserves:
    def test_a_refused_request_still_moves_money(
        self, observing: PayNaka, signed: SignedMandate
    ) -> None:
        """The headline, and it reads alarmingly on purpose. A shadow deployment that
        blocks something has changed the behaviour it promised to leave alone."""
        result = observing.execute(_denied_order(), signed)
        assert result.decision.verdict is Verdict.DENY
        assert result.executed is True
        assert result.money_moved == ATTACK

    def test_the_same_request_moves_nothing_when_enforcing(
        self, enforcing: PayNaka, signed: SignedMandate
    ) -> None:
        """The control. Without this the test above proves only that something ran."""
        result = enforcing.execute(_denied_order(), signed)
        assert result.decision.verdict is Verdict.DENY
        assert result.executed is False
        assert result.money_moved == 0

    def test_a_permitted_request_is_unaffected_by_the_mode(
        self, observing: PayNaka, enforcing: PayNaka, signed: SignedMandate
    ) -> None:
        """Observe mode changes what happens to refusals, and nothing else."""
        allowed = enforcing.execute(order(key="k_enforce"), signed)
        watched = observing.execute(order(key="k_observe"), signed)
        assert allowed.money_moved == AUTHORISED
        assert watched.money_moved == AUTHORISED
        assert watched.suppressed is False

    @pytest.mark.parametrize(
        ("kwargs", "check_id"),
        [
            ({"sku": GIFT_CARD}, "envelope.item_not_in_intent"),
            ({"unit": ATTACK}, "envelope.total_exceeded"),
            ({"qty": 99}, "envelope.qty_exceeded"),
            ({"destination": "addr_attacker"}, "envelope.destination"),
            ({"currency": "USD"}, "envelope.currency"),
        ],
        ids=["wrong-sku", "over-budget", "over-qty", "wrong-destination", "wrong-currency"],
    )
    def test_every_authority_check_is_observed_rather_than_enforced(
        self, observing: PayNaka, signed: SignedMandate, kwargs: dict[str, object], check_id: str
    ) -> None:
        """Not one check sampled -- each of them, so no check is quietly still blocking."""
        result = observing.execute(order(key=f"k_{check_id}", **kwargs), signed)  # type: ignore[arg-type]
        assert result.decision.check_id == check_id
        assert result.suppressed is True
        assert result.executed is True


# ====================================================== it cannot be mistaken for enforcing
class TestTheModeIsImpossibleToMissLater:
    def test_the_result_names_the_mode_and_flags_the_suppression(
        self, observing: PayNaka, signed: SignedMandate
    ) -> None:
        result = observing.execute(_denied_order(), signed)
        assert result.mode is Mode.OBSERVE
        assert result.suppressed is True
        assert result.to_dict()["mode"] == "observe"
        assert result.to_dict()["suppressed"] is True

    def test_an_enforced_result_never_claims_suppression(
        self, enforcing: PayNaka, signed: SignedMandate
    ) -> None:
        result = enforcing.execute(_denied_order(), signed)
        assert result.mode is Mode.ENFORCE
        assert result.suppressed is False

    def test_every_decision_record_in_the_chain_carries_the_mode(
        self, observing: PayNaka, signed: SignedMandate
    ) -> None:
        """Reading the audit log later must not permit the conclusion that the checkpoint
        was enforcing. That is the failure this design fears most."""
        observing.execute(_denied_order(), signed)
        decisions = [r for r in observing.audit.records() if r.payload.get("kind") == "decision"]
        assert decisions
        assert all(r.payload["mode"] == "observe" for r in decisions)

    def test_an_observed_refusal_is_its_own_named_record(
        self, observing: PayNaka, signed: SignedMandate
    ) -> None:
        """A shadow-mode report is generated from these, so they must name the check and
        the amount that was at stake."""
        observing.execute(_denied_order(), signed)
        observed = [r for r in observing.audit.records() if r.payload.get("kind") == "observed"]
        assert len(observed) == 1
        assert observed[0].payload["check_id"] == "envelope.item_not_in_intent"
        assert observed[0].payload["amount"] == ATTACK

    def test_enforcing_writes_no_observed_record(
        self, enforcing: PayNaka, signed: SignedMandate
    ) -> None:
        enforcing.execute(_denied_order(), signed)
        assert not [r for r in enforcing.audit.records() if r.payload.get("kind") == "observed"]

    def test_the_chain_still_verifies_after_an_observed_run(
        self, observing: PayNaka, signed: SignedMandate
    ) -> None:
        observing.execute(_denied_order(), signed)
        assert observing.audit.verify() is None


# ============================================ what observe mode does NOT suspend
class TestAuthenticationIsNeverSuspended:
    def test_a_forged_mandate_is_refused_even_when_observing(
        self, observing: PayNaka, mandate: IntentMandate
    ) -> None:
        """There is no "what would have happened anyway" to preserve: without this
        checkpoint there would be no mandate at all. Acting on an unverifiable one means
        executing whatever an attacker put in the payload."""
        forged = SignedMandate(mandate=mandate, signature=b"\x00" * 64)
        result = observing.execute(order(key="k_forged"), forged)
        assert result.executed is False
        assert result.money_moved == 0
        assert result.decision.check_id == "mandate.signature"

    def test_a_mandate_signed_by_the_wrong_key_is_refused_even_when_observing(
        self, observing: PayNaka, mandate: IntentMandate
    ) -> None:
        other = MandateSigner(generate_keypair()[0])
        result = observing.execute(order(key="k_wrongkey"), other.sign(mandate))
        assert result.executed is False
        assert result.money_moved == 0


class TestIdempotencyIsNeverSuspended:
    def test_a_duplicate_request_does_not_double_charge_when_observing(
        self, observing: PayNaka, signed: SignedMandate
    ) -> None:
        """The line between observation and damage. Not enforcing an authority check
        declines to stop what would have happened anyway. Not enforcing idempotency would
        mean issuing a second payment this checkpoint had already made itself."""
        first = observing.execute(order(key="dup"), signed)
        second = observing.execute(order(key="dup"), signed)
        assert first.money_moved == AUTHORISED
        assert second.money_moved == 0
        assert second.executed is False

    def test_twenty_redeliveries_move_the_money_once(
        self, observing: PayNaka, signed: SignedMandate
    ) -> None:
        results = [observing.execute(order(key="storm"), signed) for _ in range(20)]
        assert sum(r.money_moved for r in results) == AUTHORISED


class TestTheBreakerDoesNotTripWhileObserving:
    def test_a_run_of_refusals_withdraws_no_authority(
        self, observing: PayNaka, signed: SignedMandate, mandate: IntentMandate, state: SqliteState
    ) -> None:
        """Withdrawing a session's authority is an enforcement action, and there is no
        retry loop to bound when nothing is being refused."""
        for i in range(60):
            observing.execute(order(sku=GIFT_CARD, unit=ATTACK, key=f"burn_{i}"), signed)
        assert state.is_revoked(mandate.session_id) is False
        assert state.is_revoked(mandate.subject) is False

    def test_no_circuit_tripped_record_is_written(
        self, observing: PayNaka, signed: SignedMandate
    ) -> None:
        for i in range(60):
            observing.execute(order(sku=GIFT_CARD, unit=ATTACK, key=f"burn_{i}"), signed)
        tripped = [
            r for r in observing.audit.records() if r.payload.get("kind") == "circuit.tripped"
        ]
        assert not tripped

    def test_the_same_run_does_trip_the_breaker_when_enforcing(
        self, enforcing: PayNaka, signed: SignedMandate, mandate: IntentMandate, state: SqliteState
    ) -> None:
        """The control, so the test above is about the mode and not about the fixture."""
        for i in range(60):
            enforcing.execute(order(sku=GIFT_CARD, unit=ATTACK, key=f"burn_{i}"), signed)
        assert state.is_revoked(mandate.session_id) is True


# ============================================================== configuration
class TestAMisconfiguredModeIsAStartupFailure:
    @pytest.mark.parametrize(
        "raw",
        [
            "enfroce",
            "observ",
            "off",
            "none",
            "disabled",
            "true",
            "0",
            "enforce,observe",
            "ENFORCE!",
        ],
        ids=repr,
    )
    def test_a_typo_never_resolves_to_a_mode(self, raw: str) -> None:
        """``PAYNAKA_MODE=enfroce`` must not quietly become the mode that enforces
        nothing -- nor silently become the one that does, since the operator asked for
        something and would be told nothing."""
        with pytest.raises(ValueError, match="is not a mode"):
            Mode.from_env(raw)

    def test_the_error_names_the_permitted_values(self) -> None:
        with pytest.raises(ValueError, match="enforce, observe"):
            Mode.from_env("nonsense")

    def test_the_engine_enforces_when_no_mode_is_given(
        self, policy, state, audit, signer, clock
    ) -> None:
        """The constructor default, asserted rather than assumed."""
        naka = PayNaka(
            rail=SimRail(seed="default"),
            policy=policy,
            state=state,
            audit=audit,
            verifier=signer.verifier(),
            clock=clock,
        )
        assert naka.mode is Mode.ENFORCE
