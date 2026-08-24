"""Adversarial tests for the human-approval flow.

An approval is a capability to move money that a person hands over by clicking a button,
so the interesting question is never "does approve work". It is what an approval can be
made to do that nobody intended:

* **Reuse** -- spend one approval twice, or spend it on a different request.
* **Timing** -- answer after the window closed, or spend an approval whose window closed
  while it sat there approved.
* **Racing** -- two approvers, two retries, an approve and a deny arriving together.
* **Self-approval** -- the buying agent answering its own escalation, which would make the
  whole mechanism theatre.

The forward case is one test at the top. Everything else is an attempt to break it.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from paynaka.audit import AuditChain
from paynaka.clock import FrozenClock
from paynaka.engine import PayNaka
from paynaka.gate import LineItem, MoneyRequest, Verdict, request_hash
from paynaka.identity import TokenRegistry, load_approvers
from paynaka.mandate import IntentMandate, MandateSigner, SignedMandate, generate_keypair
from paynaka.policy import Policy
from paynaka.rails.sim import SimRail
from paynaka.state import SqliteState

pytestmark = pytest.mark.adversarial

#: `policy.yaml` steps up create_order above ₹2,000. This is comfortably over.
OVER_THRESHOLD = 350_000
HOME = "addr_home"
BIG = "MIXER-GRINDER"


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


@pytest.fixture
def big_mandate(clock: FrozenClock) -> IntentMandate:
    """A mandate that permits the amount, so the only thing stopping it is the step-up.

    That separation is the point: this file is about the approval mechanism, not about the
    envelope. If the mandate refused the amount, every test below would pass for the wrong
    reason.
    """
    return IntentMandate.create(
        clock=clock,
        subject="cust_kirana_001",
        session_id="sess_stepup",
        max_total=400_000,
        allowed_skus=(BIG,),
        allowed_destinations=(HOME,),
        max_qty_per_sku=3,
        allowed_actions=("create_order",),
    )


@pytest.fixture
def naka(policy: Policy, state: SqliteState, audit: AuditChain, signer, clock) -> PayNaka:
    return PayNaka(
        rail=SimRail(seed="stepup"),
        policy=policy,
        state=state,
        audit=audit,
        verifier=signer.verifier(),
        clock=clock,
    )


@pytest.fixture
def signed(signer: MandateSigner, big_mandate: IntentMandate) -> SignedMandate:
    return signer.sign(big_mandate)


def big_order(key: str = "idem_big", amount: int = OVER_THRESHOLD) -> MoneyRequest:
    return MoneyRequest(
        action="create_order",
        request_id=f"req_{key}",
        idempotency_key=key,
        items=(LineItem(sku=BIG, qty=1, unit_paise=amount),),
        currency="INR",
        destination=HOME,
    )


# ============================================================== the flow completes at all
class TestTheFlowCompletes:
    def test_above_the_band_it_waits_for_a_person(
        self, naka: PayNaka, signed: SignedMandate
    ) -> None:
        result = naka.execute(big_order(), signed)
        assert result.decision.verdict is Verdict.STEP_UP
        assert result.executed is False
        assert result.money_moved == 0
        assert result.provenance["escalation"]["state"] == "pending"

    def test_approved_and_retried_the_money_moves(
        self, naka: PayNaka, signed: SignedMandate
    ) -> None:
        """The whole point, and it did not work before the gate's ordering was fixed: the
        step-up used to claim the idempotency key, so the retry after approval was
        classified as a duplicate and replayed a result that was never produced."""
        first = naka.execute(big_order(), signed)
        escalation_id = first.provenance["escalation"]["id"]

        assert naka.decide_escalation(escalation_id, approve=True, by="ops-anita")

        second = naka.execute(big_order(), signed)
        assert second.decision.verdict is Verdict.ALLOW
        assert second.executed is True
        assert second.money_moved == OVER_THRESHOLD

    def test_the_allow_names_the_approval_that_released_it(
        self, naka: PayNaka, signed: SignedMandate
    ) -> None:
        """An ALLOW above the auto-approval band must not be readable without the approval
        that unlocked it."""
        first = naka.execute(big_order(), signed)
        escalation_id = first.provenance["escalation"]["id"]
        naka.decide_escalation(escalation_id, approve=True, by="ops-anita")
        second = naka.execute(big_order(), signed)
        assert second.decision.evidence["released_by_escalation"] == escalation_id

    def test_denied_and_retried_the_money_never_moves(
        self, naka: PayNaka, signed: SignedMandate
    ) -> None:
        first = naka.execute(big_order(), signed)
        naka.decide_escalation(first.provenance["escalation"]["id"], approve=False, by="ops-anita")
        second = naka.execute(big_order(), signed)
        assert second.money_moved == 0
        assert second.decision.verdict is Verdict.STEP_UP

    def test_below_the_band_nobody_is_asked(self, naka: PayNaka, signed: SignedMandate) -> None:
        """A gate that escalates everything is an outage with a queue attached."""
        result = naka.execute(big_order(key="small", amount=150_000), signed)
        assert result.decision.verdict is Verdict.ALLOW
        assert not naka.state.pending_escalations(clock=naka.clock)


# ============================================================== reuse
class TestAnApprovalIsSingleUse:
    def test_one_approval_does_not_release_two_executions(
        self, naka: PayNaka, signed: SignedMandate
    ) -> None:
        """The replay that matters. A second execution on one approval is money nobody
        agreed to twice."""
        first = naka.execute(big_order(), signed)
        naka.decide_escalation(first.provenance["escalation"]["id"], approve=True, by="ops")

        moved = [naka.execute(big_order(), signed).money_moved for _ in range(5)]
        assert sum(moved) == OVER_THRESHOLD

    def test_the_approval_is_marked_consumed_not_merely_used(
        self, naka: PayNaka, signed: SignedMandate
    ) -> None:
        first = naka.execute(big_order(), signed)
        escalation_id = first.provenance["escalation"]["id"]
        naka.decide_escalation(escalation_id, approve=True, by="ops")
        naka.execute(big_order(), signed)
        record = naka.state.escalation(escalation_id)
        assert record is not None
        assert record.state == "consumed"

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("amount", OVER_THRESHOLD + 1),
            ("amount", OVER_THRESHOLD - 1),
            ("amount", 399_900),
        ],
        ids=["one-paisa-more", "one-paisa-less", "still-inside-the-mandate"],
    )
    def test_an_approval_does_not_release_a_different_amount(
        self, naka: PayNaka, signed: SignedMandate, field: str, value: int
    ) -> None:
        """Bound to the request hash, so "yes to ₹3,500" is not "yes to ₹3,500-ish"."""
        first = naka.execute(big_order(), signed)
        naka.decide_escalation(first.provenance["escalation"]["id"], approve=True, by="ops")

        tampered = naka.execute(big_order(key="other", amount=value), signed)
        assert tampered.money_moved == 0
        assert tampered.decision.verdict is Verdict.STEP_UP

    def test_an_approval_does_not_release_a_different_destination(
        self, naka: PayNaka, signer: MandateSigner, clock: FrozenClock
    ) -> None:
        """The request hash covers the whole body, not just the number a human read."""
        mandate = IntentMandate.create(
            clock=clock,
            subject="cust_kirana_001",
            session_id="sess_dest",
            max_total=400_000,
            allowed_skus=(BIG,),
            allowed_destinations=(HOME, "addr_other"),
            max_qty_per_sku=3,
            allowed_actions=("create_order",),
        )
        signed = signer.sign(mandate)
        first = naka.execute(big_order(), signed)  # type: ignore[has-type]
        naka.decide_escalation(  # type: ignore[has-type]
            first.provenance["escalation"]["id"], approve=True, by="ops"
        )

        elsewhere = MoneyRequest(
            action="create_order",
            request_id="req_elsewhere",
            idempotency_key="idem_elsewhere",
            items=(LineItem(sku=BIG, qty=1, unit_paise=OVER_THRESHOLD),),
            currency="INR",
            destination="addr_other",
        )
        result = naka.execute(elsewhere, signed)  # type: ignore[has-type]
        assert result.money_moved == 0

    def test_the_hash_binding_is_not_incidental(self, state: SqliteState) -> None:
        """Directly at the store, so the property does not depend on the engine above it."""
        a, b = big_order("k_a"), big_order("k_b", amount=OVER_THRESHOLD + 500)
        assert request_hash(a) != request_hash(b)
        state.open_escalation(
            escalation_id="esc_a",
            request_hash=request_hash(a),
            mandate_id="m",
            session_id="s",
            subject="c",
            action="create_order",
            amount=OVER_THRESHOLD,
            summary={},
            timeout_seconds=300,
        )
        state.decide_escalation("esc_a", approve=True, by="ops")
        assert state.consume_approval(request_hash(b)) is None
        assert state.consume_approval(request_hash(a)) == "esc_a"


# ============================================================== timing
class TestTheWindowIsRealInBothDirections:
    def test_an_unanswered_escalation_expires_to_nothing(
        self, naka: PayNaka, signed: SignedMandate, clock: FrozenClock, policy: Policy
    ) -> None:
        """`on_timeout: DENY` and not configurable. Nobody answered, so nothing moves."""
        naka.execute(big_order(), signed)
        clock.advance(seconds=policy.step_up_timeout_seconds + 1)
        assert not naka.state.pending_escalations(clock=clock)
        assert naka.state.expired_escalations(clock=clock)
        assert naka.execute(big_order(), signed).money_moved == 0

    def test_an_answer_after_the_window_does_not_apply(
        self, naka: PayNaka, signed: SignedMandate, clock: FrozenClock, policy: Policy
    ) -> None:
        """Otherwise a late approval sits in the table looking valid."""
        first = naka.execute(big_order(), signed)
        escalation_id = first.provenance["escalation"]["id"]
        clock.advance(seconds=policy.step_up_timeout_seconds + 1)
        assert naka.decide_escalation(escalation_id, approve=True, by="ops-late") is None
        assert naka.execute(big_order(), signed).money_moved == 0

    def test_an_approval_that_expires_before_it_is_spent_is_not_spent(
        self, naka: PayNaka, signed: SignedMandate, clock: FrozenClock, policy: Policy
    ) -> None:
        """Approved in time, retried too late. The window bounds the approval, not just
        the asking."""
        first = naka.execute(big_order(), signed)
        naka.decide_escalation(first.provenance["escalation"]["id"], approve=True, by="ops")
        clock.advance(seconds=policy.step_up_timeout_seconds + 1)
        assert naka.execute(big_order(), signed).money_moved == 0

    @pytest.mark.parametrize("offset", [-2, -1, 0, 1])
    def test_the_boundary_is_where_it_says_it_is(
        self,
        naka: PayNaka,
        signed: SignedMandate,
        clock: FrozenClock,
        policy: Policy,
        offset: int,
    ) -> None:
        """Both sides of the expiry instant. `expires_at` is exclusive: at exactly the
        deadline the window has closed, because "300 seconds to answer" that accepts an
        answer at 300 is 301 seconds."""
        first = naka.execute(big_order(), signed)
        escalation_id = first.provenance["escalation"]["id"]
        clock.advance(seconds=policy.step_up_timeout_seconds + offset)
        landed = naka.decide_escalation(escalation_id, approve=True, by="ops")
        assert (landed is not None) is (offset < 0)


# ============================================================== racing
class TestOnlyOneAnswerCounts:
    def test_a_second_answer_changes_nothing(self, naka: PayNaka, signed: SignedMandate) -> None:
        first = naka.execute(big_order(), signed)
        escalation_id = first.provenance["escalation"]["id"]
        first_answer = naka.decide_escalation(escalation_id, approve=False, by="ops-a")
        assert first_answer is not None and first_answer.state == "denied"
        assert naka.decide_escalation(escalation_id, approve=True, by="ops-b") is None
        assert naka.execute(big_order(), signed).money_moved == 0

    def test_approve_then_deny_does_not_retract_an_approval(
        self, naka: PayNaka, signed: SignedMandate
    ) -> None:
        """Stated rather than implied: the first answer is the answer. A retraction would
        need its own mechanism, and pretending `deny` is one would be worse than not
        offering it."""
        first = naka.execute(big_order(), signed)
        escalation_id = first.provenance["escalation"]["id"]
        naka.decide_escalation(escalation_id, approve=True, by="ops-a")
        assert naka.decide_escalation(escalation_id, approve=False, by="ops-b") is None

    def test_a_duplicate_delivery_does_not_queue_a_second_approval(
        self, naka: PayNaka, signed: SignedMandate
    ) -> None:
        """Two rows for one request means a human can approve it twice, and the second
        approval is authority nobody granted twice."""
        ids = {naka.execute(big_order(), signed).provenance["escalation"]["id"] for _ in range(6)}
        assert len(ids) == 1
        assert len(naka.state.pending_escalations(clock=naka.clock)) == 1

    @pytest.mark.parametrize("escalation_id", ["", "esc_nonexistent", "../etc/passwd", "%00"])
    def test_answering_something_that_is_not_an_escalation_does_not_apply(
        self, naka: PayNaka, escalation_id: str
    ) -> None:
        if escalation_id:
            assert naka.decide_escalation(escalation_id, approve=True, by="ops") is None
        else:
            with pytest.raises(Exception, match="needs an escalation id"):
                naka.decide_escalation(escalation_id, approve=True, by="ops")


# ============================================================== self-approval
class TestTheAgentCannotApproveItself:
    def test_an_agent_and_an_approver_sharing_a_name_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        token_a, token_b = "a" * 40, "b" * 40
        agents = TokenRegistry({"buyer": token_a})
        monkeypatch.setenv("PAYNAKA_APPROVER_TOKENS", f"buyer:{token_b}")
        with pytest.raises(ValueError, match="both an agent and an approver"):
            load_approvers(agents)

    def test_an_agent_and_an_approver_sharing_a_token_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The dangerous configuration is not two entries with the same label. It is one
        secret that opens two doors."""
        token = "s" * 40
        agents = TokenRegistry({"buyer": token})
        monkeypatch.setenv("PAYNAKA_APPROVER_TOKENS", f"ops:{token}")
        with pytest.raises(ValueError, match="the same token"):
            load_approvers(agents)

    def test_an_agent_credential_does_not_authenticate_as_an_approver(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent_token, approver_token = "a" * 40, "b" * 40
        agents = TokenRegistry({"buyer": agent_token})
        monkeypatch.setenv("PAYNAKA_APPROVER_TOKENS", f"ops:{approver_token}")
        approvers = load_approvers(agents)
        with pytest.raises(Exception, match="no valid bearer"):
            approvers.authenticate(f"Bearer {agent_token}")
        assert approvers.authenticate(f"Bearer {approver_token}").name == "ops"

    def test_with_no_approvers_configured_nobody_can_approve(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail closed: every step-up then runs out its window, which is what
        "unanswered" is supposed to mean."""
        monkeypatch.delenv("PAYNAKA_APPROVER_TOKENS", raising=False)
        approvers = load_approvers(TokenRegistry({"buyer": "a" * 40}))
        assert len(approvers) == 0
        with pytest.raises(Exception, match="no valid bearer"):
            approvers.authenticate("Bearer " + "a" * 40)


# ============================================================== the record
class TestTheDecisionIsOnTheRecord:
    def test_opening_and_deciding_are_both_audited(
        self, naka: PayNaka, signed: SignedMandate
    ) -> None:
        first = naka.execute(big_order(), signed)
        naka.decide_escalation(first.provenance["escalation"]["id"], approve=True, by="ops-anita")
        kinds = [r.payload.get("kind") for r in naka.audit.records()]
        assert "escalation.opened" in kinds
        assert "escalation.decided" in kinds

    def test_the_record_names_who_approved(self, naka: PayNaka, signed: SignedMandate) -> None:
        """ "A human approved it" is not an audit trail. Which human is."""
        first = naka.execute(big_order(), signed)
        naka.decide_escalation(first.provenance["escalation"]["id"], approve=True, by="ops-anita")
        decided = [r for r in naka.audit.records() if r.payload.get("kind") == "escalation.decided"]
        assert decided[-1].payload["decided_by"] == "ops-anita"
        assert decided[-1].payload["outcome"] == "approved"

    def test_the_chain_still_verifies(self, naka: PayNaka, signed: SignedMandate) -> None:
        first = naka.execute(big_order(), signed)
        naka.decide_escalation(first.provenance["escalation"]["id"], approve=True, by="ops")
        naka.execute(big_order(), signed)
        assert naka.audit.verify() is None
