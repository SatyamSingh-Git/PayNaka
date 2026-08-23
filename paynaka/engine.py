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

from paynaka.audit import AuditChain
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
    ) -> None:
        self.rail = rail
        self.policy = policy
        self.state = state
        self.audit = audit
        self.verifier = verifier
        self.clock = clock or SystemClock()

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
        record = self.audit.append(
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
            },
            clock=self.clock,
        )

        if decision.verdict is not Verdict.ALLOW:
            return ExecutionResult(
                decision=decision,
                executed=False,
                audit_seq=record.seq,
                audit_hash=record.hash,
                provenance=provenance,
            )

        if decision.replayed:
            # The gate already resolved this as a duplicate. Calling the rail again would
            # be the exact double-charge idempotency exists to prevent.
            return ExecutionResult(
                decision=decision,
                executed=False,
                audit_seq=record.seq,
                audit_hash=record.hash,
                provenance=provenance,
            )

        return self._act(request, mandate, decision, record.seq, record.hash, provenance)

    # ---------------------------------------------------------------- internals
    def _act(
        self,
        request: MoneyRequest,
        mandate: IntentMandate,
        decision: GateDecision,
        audit_seq: int,
        audit_hash: str,
        provenance: dict[str, Any],
    ) -> ExecutionResult:
        key = request.idempotency_key or f"auto_{uuid.uuid4().hex}"
        try:
            result = self._dispatch(request, key)
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
            )

        self._post(request, result)
        settled = self.audit.append(
            {
                "kind": "executed",
                "action": request.action,
                "request_id": request.request_id,
                "rail": self.rail.name,
                "result": _describe_result(result),
            },
            clock=self.clock,
        )
        return ExecutionResult(
            decision=decision,
            executed=True,
            rail_result=result,
            audit_seq=settled.seq,
            audit_hash=settled.hash,
            provenance=provenance,
        )

    def _dispatch(self, request: MoneyRequest, key: str) -> Any:
        action = request.action
        if action == "create_order":
            return self.rail.create_order(
                amount=request.effective_amount,
                currency=request.currency,
                receipt=request.request_id,
                idempotency_key=key,
            )
        if action == "capture_payment":
            if not request.payment_id:
                raise RailError("capture names no payment")
            return self.rail.capture_payment(
                payment_id=request.payment_id,
                amount=request.effective_amount,
                idempotency_key=key,
            )
        if action == "create_refund":
            if not request.payment_id:
                raise RailError("refund names no payment")
            return self.rail.create_refund(
                payment_id=request.payment_id,
                amount=request.effective_amount,
                idempotency_key=key,
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
        self.audit.append(
            {
                "kind": kind,
                "action": request.action,
                "request_id": request.request_id,
                "detail": detail,
                "provenance": provenance,
            },
            clock=self.clock,
        )

    def _record_rejection(
        self, request: MoneyRequest, reason: str, provenance: dict[str, Any]
    ) -> ExecutionResult:
        decision = GateDecision(
            verdict=Verdict.DENY,
            action=request.action,
            reason=reason,
            check_id="mandate.signature",
            evidence={"request_id": request.request_id},
            request_id=request.request_id,
        )
        record = self.audit.append(
            {
                "kind": "decision",
                "request": _describe(request),
                "decision": decision.to_dict(),
                "mandate": None,
                "provenance": provenance,
            },
            clock=self.clock,
        )
        return ExecutionResult(
            decision=decision,
            executed=False,
            audit_seq=record.seq,
            audit_hash=record.hash,
            provenance=provenance,
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
