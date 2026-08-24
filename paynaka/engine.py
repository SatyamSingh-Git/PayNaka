"""The enforced path: the only way a money action reaches a rail.

``PayNaka.execute`` is a single funnel. It verifies the mandate's signature, asks the gate,
writes an audit record for the decision *whatever the decision was*, and only then -- if
and only if the verdict was ALLOW -- calls the rail and records the movement in the ledger.

Four properties, each of which exists because its absence is a known way to lose money:

**Denials are audited too.** A trail that only records what happened is a receipt book.
The interesting question after an incident is what was attempted and refused.

**The audit record is written before the rail is called.** If the process dies mid-call,
the intent to move money is already on record, and reconciliation has something to find.
The reverse order loses exactly the events you would most want.

**A rail failure is not a silent no-op.** Declines and timeouts are recorded as their own
audit events, and a timeout is explicitly *not* treated as a failure -- the outcome is
unknown, and the idempotency key is what resolves it.

**The ledger is updated from what the rail confirmed**, never from what was requested.
Those differ on partial capture, and trusting the request is how a ledger drifts.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from paynaka.anchor import AnchorLog, Notary, rail_note
from paynaka.audit import GENESIS, AuditChain, AuditRecord
from paynaka.clock import Clock, SystemClock
from paynaka.gate import (
    GateDecision,
    MoneyRequest,
    Verdict,
    evaluate,
    fingerprint_amount,
    request_hash,
    reservation_key,
)
from paynaka.mandate import (
    IntentMandate,
    MandateError,
    MandateVerifier,
    SignedMandate,
)
from paynaka.mode import Mode
from paynaka.policy import Policy
from paynaka.rails.base import Rail, RailDeclined, RailError
from paynaka.state import SqliteState

__all__ = ["ExecutionResult", "PayNaka"]


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """What happened, in enough detail for the console and the audit log."""

    decision: GateDecision
    executed: bool
    rail_result: Any | None = None
    error: str | None = None
    audit_seq: int | None = None
    audit_hash: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)
    #: Which mode produced this. Carried on the result, not inferred from it, so a reader
    #: never has to reconstruct whether the checkpoint was enforcing at the time.
    mode: Mode = Mode.ENFORCE
    #: A refusal that was computed and then not acted on, because the mode is ``observe``.
    #: This is the field a shadow-mode report counts.
    suppressed: bool = False

    @property
    def money_moved(self) -> int:
        """Paise actually moved. Zero unless a rail confirmed movement.

        HAAT scores attack success on this number rather than on the verdict, because the
        question a merchant cares about is not whether a gate said DENY but whether money
        left the account.
        """
        if not self.executed or self.rail_result is None:
            return 0
        return int(getattr(self.rail_result, "amount", 0))

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.to_dict(),
            "executed": self.executed,
            "money_moved": self.money_moved,
            "error": self.error,
            "audit_seq": self.audit_seq,
            "audit_hash": self.audit_hash,
            "provenance": self.provenance,
            "mode": self.mode.value,
            "suppressed": self.suppressed,
        }


class PayNaka:
    """The checkpoint, wired to a rail, a ledger and an audit chain."""

    def __init__(
        self,
        *,
        rail: Rail,
        policy: Policy,
        state: SqliteState,
        audit: AuditChain,
        verifier: MandateVerifier,
        clock: Clock | None = None,
        notary: Notary | None = None,
        anchors: AnchorLog | None = None,
        anchor_every: int = 20,
        mode: Mode = Mode.ENFORCE,
    ) -> None:
        self.rail = rail
        self.policy = policy
        self.state = state
        self.audit = audit
        self.verifier = verifier
        self.clock = clock or SystemClock()
        # Enforce unless told otherwise. A checkpoint that does not enforce by default
        # is a checkpoint somebody forgot to switch on.
        self.mode = mode

        # Witnessing, optional and off by default so nothing that already works changes
        # shape. Supply a notary and a log and the chain stops being merely self-consistent:
        # see paynaka/anchor.py for what each of the three tiers actually buys.
        self.notary = notary
        self.anchors = anchors
        self.anchor_every = anchor_every

    # ---------------------------------------------------------------- public API
    def execute(
        self,
        request: MoneyRequest,
        signed: SignedMandate,
        *,
        provenance: dict[str, Any] | None = None,
    ) -> ExecutionResult:
        """Verify, decide, record, and only then act."""
        provenance = provenance or {}

        try:
            mandate = self.verifier.verify(signed)
        except MandateError as exc:
            # A bad signature never reaches the gate. It is not a policy question.
            return self._record_rejection(request, str(exc), provenance)

        decision = evaluate(
            request, mandate, state=self.state, policy=self.policy, clock=self.clock
        )
        record = self._write(
            {
                "kind": "decision",
                "request": _describe(request),
                "decision": decision.to_dict(),
                "mandate": {
                    "id": mandate.mandate_id,
                    "session": mandate.session_id,
                    "subject": mandate.subject,
                    "max_total": mandate.max_total,
                },
                "provenance": provenance,
                # On every record, so it is never possible to read the chain later and
                # believe the checkpoint was enforcing when it was not.
                "mode": self.mode.value,
            }
        )

        if decision.verdict is not Verdict.ALLOW:
            if self.mode.enforcing:
                self._account_for_denial(decision, mandate)
                return ExecutionResult(
                    decision=decision,
                    executed=False,
                    audit_seq=record.seq,
                    audit_hash=record.hash,
                    provenance=provenance,
                    mode=self.mode,
                )
            # Observing. The refusal is computed and recorded, and then not acted on --
            # the point of a shadow deployment is that nothing changes while the operator
            # finds out what enforcement would have cost.
            #
            # The circuit breaker is deliberately not advanced. Withdrawing a session's
            # authority is an enforcement action, and there is no retry loop to bound when
            # nothing is being refused.
            self._write(
                {
                    "kind": "observed",
                    "request_id": request.request_id,
                    "check_id": decision.check_id,
                    "verdict": decision.verdict.value,
                    "amount": request.effective_amount,
                    "detail": (
                        f"{decision.check_id} would have stopped this request in enforce "
                        f"mode; the mode is observe, so it was allowed through"
                    ),
                }
            )

        if decision.replayed:
            # The gate already resolved this as a duplicate. Calling the rail again would
            # be the exact double-charge idempotency exists to prevent.
            #
            # This is checked in *both* modes, and the ordering is what makes that true: a
            # replay is an ALLOW, so it never reaches the suppression above. Declining to
            # enforce an authority check means not stopping what would have happened
            # anyway. Declining to enforce idempotency would mean issuing a second payment
            # this checkpoint had already made -- that is not observation, it is damage.
            return ExecutionResult(
                decision=decision,
                executed=False,
                audit_seq=record.seq,
                audit_hash=record.hash,
                provenance=provenance,
                mode=self.mode,
            )

        return self._act(
            request,
            mandate,
            decision,
            record.seq,
            record.hash,
            provenance,
            suppressed=decision.verdict is not Verdict.ALLOW,
        )

    # ---------------------------------------------------------------- internals
    def _act(
        self,
        request: MoneyRequest,
        mandate: IntentMandate,
        decision: GateDecision,
        audit_seq: int,
        audit_hash: str,
        provenance: dict[str, Any],
        *,
        suppressed: bool = False,
    ) -> ExecutionResult:
        key = request.idempotency_key or f"auto_{uuid.uuid4().hex}"
        try:
            result = self._dispatch(request, mandate, key)
        except RailDeclined as exc:
            # Definitive: the rail says it did not and will not. The claim the gate took
            # on the refundable balance goes back, so the next request can use it.
            self._release(request)
            self._append("rail.declined", request, str(exc), provenance)
            return ExecutionResult(
                decision=decision,
                executed=False,
                error=str(exc),
                audit_seq=audit_seq,
                audit_hash=audit_hash,
                provenance=provenance,
                mode=self.mode,
                suppressed=suppressed,
            )
        except RailError as exc:
            # Outcome unknown. Deliberately not recorded as a failure: the money may have
            # moved, and the idempotency key is what will settle it on retry.
            #
            # The balance claim is deliberately *not* released. Releasing it would let the
            # next refund spend a balance that may already be gone, and the conservative
            # direction on a money path is to keep it held until reconciliation resolves
            # it. ``state.unresolved_reservations()`` is that queue.
            self._append("rail.indeterminate", request, str(exc), provenance)
            return ExecutionResult(
                decision=decision,
                executed=False,
                error=f"outcome unknown: {exc}",
                audit_seq=audit_seq,
                audit_hash=audit_hash,
                provenance=provenance,
                mode=self.mode,
                suppressed=suppressed,
            )

        self._post(request, result)
        settled = self._write(
            {
                "kind": "executed",
                "action": request.action,
                "request_id": request.request_id,
                "rail": self.rail.name,
                "result": _describe_result(result),
            }
        )
        return ExecutionResult(
            decision=decision,
            executed=True,
            rail_result=result,
            audit_seq=settled.seq,
            audit_hash=settled.hash,
            provenance=provenance,
            mode=self.mode,
            suppressed=suppressed,
        )

    def _account_for_denial(self, decision: GateDecision, mandate: IntentMandate) -> None:
        """Count a refusal, and withdraw the session's authority if it has had too many.

        The gate decides; this does the accounting. Keeping them apart matters: a check
        that both refuses *and* changes what future checks will say is a check nobody can
        reason about in isolation, and every check in ``gate.py`` is a function somebody
        should be able to read on its own.

        A refusal the gate already made permanent is not counted again -- a revoked
        session hammering the wall must not keep inflating a number that has already done
        its job. Nor is a STEP_UP: waiting for a human is not being refused.
        """
        breaker = self.policy.circuit_breaker
        if not breaker.enabled or decision.verdict is not Verdict.DENY:
            return
        if decision.check_id == "revoked":
            return

        session_total = self.state.bump_denial(f"session:{mandate.session_id}", clock=self.clock)
        subject_total = self.state.bump_denial(f"subject:{mandate.subject}", clock=self.clock)

        tripped: list[tuple[str, str, int, int]] = []
        if session_total >= breaker.denials_per_session:
            tripped.append(
                ("session", mandate.session_id, session_total, breaker.denials_per_session)
            )
        if subject_total >= breaker.denials_per_subject:
            tripped.append(("subject", mandate.subject, subject_total, breaker.denials_per_subject))

        for scope, value, total, limit in tripped:
            if self.state.is_revoked(value):
                continue
            self.state.revoke(value, clock=self.clock)
            self._write(
                {
                    "kind": "circuit.tripped",
                    "scope": scope,
                    "value": value,
                    "denials": total,
                    "limit": limit,
                    "detail": (
                        f"{total} refusals in one day on this {scope}; authority withdrawn. "
                        f"An operator clears it with unrevoke() and clear_denials()."
                    ),
                }
            )

    def _write(self, payload: dict[str, Any]) -> AuditRecord:
        """Append to the chain, then let the notary witness it if it is time.

        Every audit write in this class goes through here. That is the point: witnessing
        attached to individual call sites is witnessing somebody will forget to attach,
        and the first version of this proved it by anchoring only after decision records.
        """
        record = self.audit.append(payload, clock=self.clock)
        self._maybe_anchor()
        return record

    def _maybe_anchor(self) -> None:
        """Ask the notary to witness the chain, every ``anchor_every`` records.

        Periodic rather than per-record because a witness costs a signature and the
        protection is not per-record: an anchor at length N pins everything at or before
        N. The interval is the window an attacker gets, and it is a knob rather than a
        constant so an operator can decide how large a window they will accept.
        """
        if self.notary is None or self.anchors is None:
            return
        length = len(self.audit)
        if length and length % self.anchor_every == 0:
            self.anchors.append(self.notary.witness(self.audit, clock=self.clock))

    def anchor_now(self) -> None:
        """Witness immediately. For shutdown, for a demo, and for after an incident."""
        if self.notary is None or self.anchors is None:
            raise RuntimeError("no notary configured; nothing can witness this chain")
        self.anchors.append(self.notary.witness(self.audit, clock=self.clock))

    def _rail_notes(self, request: MoneyRequest, mandate: IntentMandate) -> dict[str, str]:
        """What PayNaka asks the gateway to remember about this call.

        The audit head is the interesting one. Razorpay stores these against the payment
        and hands them back on read, so every money movement becomes a witness to the
        state of the local chain at the moment it happened -- and an attacker who rewrites
        that chain has to rewrite Razorpay's records too, which are not theirs to rewrite.

        Sent whether or not a notary is configured, because it costs nothing and the
        gateway is the most external witness available.
        """
        head = self.audit.head()
        notes = {
            "paynaka_mandate": mandate.mandate_id[:64],
            "paynaka_request": request.request_id[:64],
        }
        if head != GENESIS:
            notes["paynaka_audit_head"] = rail_note(head)
            notes["paynaka_audit_len"] = str(len(self.audit))
        return {k: v for k, v in notes.items() if v}

    def _dispatch(self, request: MoneyRequest, mandate: IntentMandate, key: str) -> Any:
        action = request.action
        notes = self._rail_notes(request, mandate)
        if action == "create_order":
            return self.rail.create_order(
                amount=request.effective_amount,
                currency=request.currency,
                receipt=request.request_id,
                idempotency_key=key,
                notes=notes,
            )
        if action == "capture_payment":
            if not request.payment_id:
                raise RailError("capture names no payment")
            return self.rail.capture_payment(
                payment_id=request.payment_id,
                amount=request.effective_amount,
                idempotency_key=key,
                notes=notes,
            )
        if action == "create_refund":
            if not request.payment_id:
                raise RailError("refund names no payment")
            return self.rail.create_refund(
                payment_id=request.payment_id,
                amount=request.effective_amount,
                idempotency_key=key,
                notes=notes,
            )
        raise RailError(f"no rail binding for action {action!r}")

    def _post(self, request: MoneyRequest, result: Any) -> None:
        """Update the ledger from what the rail confirmed, not from what was asked.

        They differ on a partial capture, and taking the request's word for it is how a
        ledger silently drifts away from reality.
        """
        confirmed = int(getattr(result, "amount", 0))
        if request.action == "create_refund":
            # The refund's ledger entry is written by settling the claim the gate took,
            # so the balance is never released and re-spent between the two.
            self.state.settle_reservation(reservation_key(request), confirmed, clock=self.clock)
            return
        if confirmed <= 0:
            return
        if request.action == "capture_payment":
            self.state.record_capture(
                getattr(result, "payment_id", request.payment_id or ""),
                confirmed,
                clock=self.clock,
            )

    def _release(self, request: MoneyRequest) -> None:
        """Hand a refund's balance claim back. Only on a refusal the rail stands behind."""
        if request.action == "create_refund":
            self.state.release_reservation(reservation_key(request))

    def _append(
        self, kind: str, request: MoneyRequest, detail: str, provenance: dict[str, Any]
    ) -> None:
        self._write(
            {
                "kind": kind,
                "action": request.action,
                "request_id": request.request_id,
                "detail": detail,
                "provenance": provenance,
            }
        )

    def _record_rejection(
        self, request: MoneyRequest, reason: str, provenance: dict[str, Any]
    ) -> ExecutionResult:
        """A mandate whose signature does not verify. Refused in **both** modes.

        Observe mode declines to enforce authority judgments about authenticated requests.
        It does not decline to authenticate. An unverifiable mandate is not a request the
        checkpoint is being strict about -- it is a request whose stated authority is
        unattributable, and acting on it would mean executing whatever an attacker put in
        the payload. There is no "what would have happened anyway" to preserve, because
        without this checkpoint there would be no mandate at all.
        """
        decision = GateDecision(
            verdict=Verdict.DENY,
            action=request.action,
            reason=reason,
            check_id="mandate.signature",
            evidence={"request_id": request.request_id},
            request_id=request.request_id,
        )
        record = self._write(
            {
                "kind": "decision",
                "request": _describe(request),
                "decision": decision.to_dict(),
                "mandate": None,
                "provenance": provenance,
                "mode": self.mode.value,
            }
        )
        return ExecutionResult(
            decision=decision,
            executed=False,
            audit_seq=record.seq,
            audit_hash=record.hash,
            provenance=provenance,
            mode=self.mode,
        )


def _describe(request: MoneyRequest) -> dict[str, Any]:
    """What the audit chain records about an attempt, whatever the attempt contained.

    Deliberately total. This runs for denials as well as approvals, and a request the gate
    refused because its own arithmetic is impossible is exactly the one an incident review
    wants to see.
    """
    return {
        "action": request.action,
        "request_id": request.request_id,
        "amount": fingerprint_amount(request),
        "currency": request.currency,
        "destination": request.destination,
        "payment_id": request.payment_id,
        "items": [{"sku": i.sku, "qty": i.qty, "unit_paise": i.unit_paise} for i in request.items],
        "fingerprint": request_hash(request),
    }


def _describe_result(result: Any) -> dict[str, Any]:
    return {
        key: getattr(result, key)
        for key in ("order_id", "payment_id", "refund_id", "amount", "status")
        if hasattr(result, key)
    }
