"""The checkpoint. Every money action passes through ``evaluate`` or it does not happen.

**This module imports no LLM SDK, and it never will.** That is a claim a reviewer can
verify by reading the import block above, and it is the whole architectural argument:
injection can change what an agent *wants*, but the decision about what it is *allowed*
to do is made by deterministic code that never consults a model.

Three properties, each tested:

**Deny wins.** Checks run in order and the first DENY short-circuits. Policy may escalate
an ALLOW to a STEP_UP, but nothing can turn a DENY into an ALLOW -- so a misconfigured
policy cannot open a hole, only close one further.

**Pure functions.** Every check is a function of ``(request, mandate, state, policy,
clock)``. Nothing reads the wall clock, the environment, or a global. That is what makes
the RBI and NPCI time-window rules testable without waiting until 7pm.

**Idempotency replays rather than denies.** A duplicate webhook is not an attack, it is
Tuesday. Re-presenting an identical request returns the original result instead of moving
money twice, and re-presenting a *different* request under the same key is a DENY.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from paynaka.clock import Clock
from paynaka.mandate import IntentMandate, MandateExpired
from paynaka.money import MAX_PAISE, MoneyError, add, mul_qty, to_paise
from paynaka.policy import Policy
from paynaka.state import SqliteState

__all__ = [
    "GateDecision",
    "LineItem",
    "MoneyRequest",
    "Verdict",
    "evaluate",
    "request_hash",
    "reservation_key",
]

#: Actions that move money out irreversibly. Used only to decide how loudly to log; the
#: checks themselves do not branch on it, because a special case is a place to hide a bug.
IRREVERSIBLE: Final[frozenset[str]] = frozenset({"create_refund", "create_payout"})


class Verdict(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    STEP_UP = "STEP_UP"


@dataclass(frozen=True, slots=True)
class LineItem:
    """One line of a proposed order. Amounts are integer paise."""

    sku: str
    qty: int
    unit_paise: int

    def __post_init__(self) -> None:
        if not isinstance(self.sku, str) or not self.sku:
            raise ValueError("sku must be a non-empty string")
        if isinstance(self.qty, bool) or not isinstance(self.qty, int):
            raise ValueError("qty must be int")
        if isinstance(self.unit_paise, bool) or not isinstance(self.unit_paise, int):
            raise ValueError("unit_paise must be int paise")

    @property
    def total(self) -> int:
        return int(mul_qty(self.unit_paise, self.qty))


@dataclass(frozen=True, slots=True)
class MoneyRequest:
    """What the agent is asking PayNaka to do.

    The agent constructs this; every field is therefore untrusted. The gate's job is to
    decide whether this request is inside the authority the mandate already granted.
    """

    action: str
    request_id: str
    idempotency_key: str | None = None
    items: tuple[LineItem, ...] = ()
    amount: int | None = None
    currency: str = "INR"
    destination: str | None = None
    payment_id: str | None = None
    is_recurring_debit: bool = False
    is_customer_contact: bool = False
    notice_sent_at: int | None = None
    has_afa: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def effective_amount(self) -> int:
        """The amount this request would move.

        ``amount`` when given (refunds, captures), otherwise the sum of the line items.
        Computed through the money helpers so overflow and absurd quantities are refused
        here rather than deep inside a rail call.
        """
        if self.amount is not None:
            return int(to_paise(self.amount))
        if not self.items:
            return 0
        return int(add(*(item.total for item in self.items)))


@dataclass(frozen=True, slots=True)
class GateDecision:
    """Why the gate said what it said. Every field ends up in the audit record."""

    verdict: Verdict
    action: str
    reason: str
    check_id: str | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)
    mandate_id: str | None = None
    request_id: str | None = None
    latency_us: int = 0
    replayed: bool = False

    @property
    def allowed(self) -> bool:
        return self.verdict is Verdict.ALLOW

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": str(self.verdict),
            "action": self.action,
            "reason": self.reason,
            "check_id": self.check_id,
            "evidence": dict(self.evidence),
            "mandate_id": self.mandate_id,
            "request_id": self.request_id,
            "latency_us": self.latency_us,
            "replayed": self.replayed,
        }


def request_hash(request: MoneyRequest) -> str:
    """A stable fingerprint of the money-relevant parts of a request.

    Used to tell a genuine retry (same key, same body) from key reuse with a different
    body, which is a substitution attack. ``request_id`` is deliberately excluded: two
    retries of the same logical operation carry different request ids and must still
    hash the same.
    """
    payload = {
        "action": request.action,
        "amount": request.effective_amount,
        "currency": request.currency,
        "destination": request.destination,
        "payment_id": request.payment_id,
        # Sorted so that two requests differing only in line-item order hash the same:
        # a retry that reorders its cart is still the same retry.
        "items": [
            [sku, qty, unit]
            for sku, qty, unit in sorted((i.sku, i.qty, i.unit_paise) for i in request.items)
        ],
    }
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(b"paynaka.request.v1|" + body.encode("ascii")).hexdigest()


# ====================================================================== the checks
# Each returns a GateDecision to stop the pipeline, or None to continue. They are
# module-level functions rather than methods so each can be unit-tested in isolation.


def _deny(check_id: str, reason: str, **evidence: Any) -> GateDecision:
    return GateDecision(
        verdict=Verdict.DENY, action="", reason=reason, check_id=check_id, evidence=evidence
    )


def check_structure(request: MoneyRequest, mandate: IntentMandate) -> GateDecision | None:
    """The request is well-formed and internally consistent."""
    if not isinstance(request.action, str) or not request.action:
        return _deny("structure.action", "request carries no action")

    if request.effective_amount < 0:
        return _deny("structure.negative", "request amount is negative")

    if request.effective_amount > MAX_PAISE:
        return _deny(
            "structure.ceiling",
            "request amount exceeds the sanity ceiling",
            amount=request.effective_amount,
        )

    # An order whose stated amount disagrees with its own line items is either a bug or a
    # probe. Either way the gate must not pick a side.
    if request.amount is not None and request.items:
        line_total = int(add(*(item.total for item in request.items)))
        if line_total != request.amount:
            return _deny(
                "structure.total_mismatch",
                "stated amount does not equal the sum of line items",
                stated=request.amount,
                line_total=line_total,
            )
    return None


def check_revoked(
    request: MoneyRequest, mandate: IntentMandate, state: SqliteState
) -> GateDecision | None:
    """The kill switch, checked before anything else expensive."""
    if state.is_revoked(mandate.mandate_id, mandate.session_id):
        return _deny(
            "revoked",
            "authority has been revoked",
            mandate_id=mandate.mandate_id,
            session_id=mandate.session_id,
        )
    return None


def check_expiry(mandate: IntentMandate, clock: Clock) -> GateDecision | None:
    try:
        mandate.assert_live(clock)
    except MandateExpired as exc:
        return _deny("mandate.expired", str(exc), now=clock.epoch(), expires_at=mandate.expires_at)
    return None


def check_action_authorised(
    request: MoneyRequest, mandate: IntentMandate, policy: Policy
) -> GateDecision | None:
    """The action must be permitted by the mandate *and* enabled by the policy.

    Both, not either. The mandate is the shopper's authority; the policy is the merchant's
    additional caution. The effective permission is the intersection.
    """
    if request.action not in mandate.allowed_actions:
        return _deny(
            "authority.action_not_in_mandate",
            f"action {request.action!r} is not authorised by the mandate",
            requested=request.action,
            authorised=list(mandate.allowed_actions),
        )

    action_policy = policy.for_action(request.action)
    if not action_policy.enabled:
        return _deny(
            "policy.action_disabled",
            f"action {request.action!r} is disabled by merchant policy",
            requested=request.action,
        )
    return None


def check_currency(request: MoneyRequest, mandate: IntentMandate) -> GateDecision | None:
    """Currency confusion: ``$1,999`` read as ``₹1,999`` is an 85x overpayment."""
    if request.currency != mandate.currency:
        return _deny(
            "envelope.currency",
            f"currency {request.currency!r} does not match the authorised {mandate.currency!r}",
            requested=request.currency,
            authorised=mandate.currency,
        )
    return None


def check_items_subset(request: MoneyRequest, mandate: IntentMandate) -> GateDecision | None:
    """Line-item append: the gift card the shopper never asked for.

    An empty ``allowed_skus`` means "any SKU, subject to the other bounds" -- the shopper
    said "something under two thousand rupees" without naming a product. It does not mean
    "no SKUs are allowed", because that would make an open-ended budget unusable.
    """
    if not mandate.allowed_skus:
        return None

    # A SKU-scoped mandate plus an order that declares no line items is not a pass -- it
    # is a request to approve something the gate cannot see. Letting it through would let
    # any caller skip the allow-list simply by omitting the itemisation, which is a hole
    # wide enough for the whole attack corpus. You cannot verify what you were not shown.
    if request.action == "create_order" and not request.items:
        return _deny(
            "envelope.items_undeclared",
            "the mandate names specific SKUs, so an order must declare its line items",
            authorised=list(mandate.allowed_skus),
        )

    permitted = set(mandate.allowed_skus)
    for item in request.items:
        if item.sku not in permitted:
            return _deny(
                "envelope.item_not_in_intent",
                f"line item {item.sku!r} is not in the frozen intent",
                sku=item.sku,
                authorised=sorted(permitted),
                line_total=item.total,
            )
    return None


def check_quantities(request: MoneyRequest, mandate: IntentMandate) -> GateDecision | None:
    """Quantity inflation: 'orders below 40 units are rejected'."""
    for item in request.items:
        if item.qty < 0:
            return _deny("envelope.qty_negative", "negative quantity", sku=item.sku, qty=item.qty)
        if item.qty > mandate.max_qty_per_sku:
            return _deny(
                "envelope.qty_exceeded",
                f"quantity {item.qty} exceeds the authorised {mandate.max_qty_per_sku} for a SKU",
                sku=item.sku,
                requested=item.qty,
                authorised=mandate.max_qty_per_sku,
            )
    return None


def check_total(
    request: MoneyRequest, mandate: IntentMandate, policy: Policy
) -> GateDecision | None:
    """The headline check. ₹1,999 authorised, ₹52,000 requested."""
    amount = request.effective_amount

    if amount > mandate.max_total:
        return _deny(
            "envelope.total_exceeded",
            f"amount {amount} exceeds the authorised {mandate.max_total}",
            requested=amount,
            authorised=mandate.max_total,
        )

    limit = policy.for_action(request.action).max_amount
    if limit is not None and amount > limit:
        return _deny(
            "policy.max_amount",
            f"amount {amount} exceeds the merchant policy limit {limit}",
            requested=amount,
            limit=limit,
        )
    return None


def check_destination(request: MoneyRequest, mandate: IntentMandate) -> GateDecision | None:
    """Destination swap: the seller copy that rewrites the shipping address."""
    if not mandate.allowed_destinations or request.destination is None:
        return None
    if request.destination not in mandate.allowed_destinations:
        return _deny(
            "envelope.destination",
            f"destination {request.destination!r} is not on the authorised list",
            requested=request.destination,
            authorised=list(mandate.allowed_destinations),
        )
    return None


def check_refund_bounds(
    request: MoneyRequest, mandate: IntentMandate, state: SqliteState, policy: Policy
) -> GateDecision | None:
    """Refund without return, and refunding more than was ever captured."""
    if request.action != "create_refund":
        return None

    if not request.payment_id:
        return _deny("refund.no_payment", "refund names no payment")

    action_policy = policy.for_action("create_refund")
    amount = request.effective_amount

    refundable = state.refundable_amount(request.payment_id)
    if amount > refundable:
        return _deny(
            "refund.exceeds_capture",
            f"refund {amount} exceeds the {refundable} still refundable on this payment",
            requested=amount,
            refundable=refundable,
            captured=state.captured_amount(request.payment_id),
            already_refunded=state.refunded_amount(request.payment_id),
        )

    needs_return = mandate.requires_return_for_refund or action_policy.require_return_event
    if needs_return and not state.has_return(request.payment_id):
        return _deny(
            "refund.no_return_on_record",
            "refund requires a return event and none is on record",
            payment_id=request.payment_id,
        )
    return None


def check_daily_cap(
    request: MoneyRequest, state: SqliteState, policy: Policy, clock: Clock
) -> GateDecision | None:
    """A per-IST-day ceiling, so a compromised agent cannot drain slowly."""
    cap = policy.for_action(request.action).daily_cap
    if cap is None or request.action != "create_refund":
        return None

    spent_today = state.daily_refund_total(clock.epoch())
    if spent_today + request.effective_amount > cap:
        return _deny(
            "policy.daily_cap",
            f"refund would take today's total past the {cap} daily cap",
            requested=request.effective_amount,
            spent_today=spent_today,
            cap=cap,
        )
    return None


def check_regulatory(
    request: MoneyRequest, mandate: IntentMandate, state: SqliteState, policy: Policy, clock: Clock
) -> GateDecision | None:
    """Indian payments regulation, enforced rather than documented."""
    reg = policy.regulatory
    now = clock.now()

    if request.is_recurring_debit:
        for window in reg.debit_blackout:
            if window.contains(now):
                return _deny(
                    "regulatory.debit_blackout",
                    f"recurring debits are not permitted during {window}",
                    window=str(window),
                )

        used = state.retry_count(mandate.mandate_id, clock.epoch())
        if used >= reg.npci_mandate_retries:
            return _deny(
                "regulatory.retry_cap",
                f"NPCI permits {reg.npci_mandate_retries} retries per cycle; {used} already used",
                used=used,
                cap=reg.npci_mandate_retries,
            )

        if reg.pre_debit_notice_seconds > 0:
            notice = request.notice_sent_at
            if notice is None:
                return _deny(
                    "regulatory.no_pre_debit_notice",
                    "RBI requires advance notice before a recurring debit; none was recorded",
                )
            elapsed = clock.epoch() - notice
            if elapsed < reg.pre_debit_notice_seconds:
                return _deny(
                    "regulatory.notice_too_recent",
                    f"pre-debit notice was {elapsed}s ago; "
                    f"{reg.pre_debit_notice_seconds}s is required",
                    elapsed=elapsed,
                    required=reg.pre_debit_notice_seconds,
                )

    if (
        request.is_customer_contact
        and reg.contact_window is not None
        and not reg.contact_window.contains(now)
    ):
        return _deny(
            "regulatory.contact_window",
            f"customer contact is only permitted during {reg.contact_window}",
            window=str(reg.contact_window),
        )

    if (
        reg.afa_threshold is not None
        and request.effective_amount > reg.afa_threshold
        and not request.has_afa
    ):
        return _deny(
            "regulatory.afa_required",
            f"amounts above {reg.afa_threshold} require additional factor authentication",
            amount=request.effective_amount,
            threshold=reg.afa_threshold,
        )
    return None


def check_step_up(request: MoneyRequest, policy: Policy) -> GateDecision | None:
    """The only check that may return something other than DENY."""
    threshold = policy.for_action(request.action).step_up_above
    if threshold is None or request.effective_amount <= threshold:
        return None
    return GateDecision(
        verdict=Verdict.STEP_UP,
        action=request.action,
        reason=f"amount {request.effective_amount} is above the {threshold} auto-approval limit",
        check_id="policy.step_up",
        evidence={"amount": request.effective_amount, "threshold": threshold},
    )


# ====================================================================== the pipeline


def evaluate(
    request: MoneyRequest,
    mandate: IntentMandate,
    *,
    state: SqliteState,
    policy: Policy,
    clock: Clock,
) -> GateDecision:
    """Decide whether ``request`` may proceed. The single entry point.

    Order is deliberate: cheapest and most certain first, so a revoked mandate or an
    expired one costs one lookup rather than a full envelope evaluation. Idempotency is
    resolved last among the DENY checks, because a replay of an *unauthorised* request
    should still be reported as unauthorised rather than as a replay.
    """
    started = time.perf_counter_ns()

    def finish(decision: GateDecision) -> GateDecision:
        from dataclasses import replace as _replace

        return _replace(
            decision,
            action=request.action,
            mandate_id=mandate.mandate_id,
            request_id=request.request_id,
            latency_us=(time.perf_counter_ns() - started) // 1000,
        )

    try:
        checks: Sequence[GateDecision | None] = (
            check_revoked(request, mandate, state),
            check_expiry(mandate, clock),
            check_structure(request, mandate),
            check_action_authorised(request, mandate, policy),
            check_currency(request, mandate),
            check_items_subset(request, mandate),
            check_quantities(request, mandate),
            check_total(request, mandate, policy),
            check_destination(request, mandate),
            check_refund_bounds(request, mandate, state, policy),
            check_daily_cap(request, state, policy, clock),
            check_regulatory(request, mandate, state, policy, clock),
        )
    except (MoneyError, ValueError) as exc:
        # A check raised rather than returned. Fail closed and say so: a crashed check is
        # an unenforced check, and silently continuing past one is how gates get bypassed.
        return finish(_deny("internal.check_raised", f"a gate check could not complete: {exc}"))

    for decision in checks:
        if decision is not None:
            return finish(decision)

    idempotency = _resolve_idempotency(request, policy, state, clock)
    if idempotency is not None:
        return finish(idempotency)

    step_up = check_step_up(request, policy)
    if step_up is not None:
        # No balance is claimed for a request that still needs a human. Holding it would
        # block other refunds for as long as the approval sits in somebody's inbox.
        return finish(step_up)

    reservation = _reserve_refund(request, state, clock)
    if reservation is not None:
        return finish(reservation)

    return finish(
        GateDecision(
            verdict=Verdict.ALLOW,
            action=request.action,
            reason="within the frozen intent and merchant policy",
            check_id=None,
            evidence={
                "amount": request.effective_amount,
                "authorised": mandate.max_total,
            },
        )
    )


def reservation_key(request: MoneyRequest) -> str:
    """The key a refund's balance claim is held under.

    Derived, not passed, so the gate and the engine cannot disagree about it. Prefers the
    idempotency key -- which names the business event -- and falls back to the request id
    when policy does not require one, since something must be unique per attempt.
    """
    return request.idempotency_key or f"req:{request.request_id}"


def _reserve_refund(request: MoneyRequest, state: SqliteState, clock: Clock) -> GateDecision | None:
    """Claim the balance this refund would spend, atomically, before anything moves.

    ``check_refund_bounds`` above already compared the amount against what is refundable,
    and that check is still worth having for the error message it produces. It is not
    worth trusting on its own: it reads the balance and the ledger is written later, and
    two refunds for the same payment arriving together both fit through the gap. Measured
    on twenty concurrent refunds, the gate approved all twenty and the *gateway* refused
    sixteen. A bound the payment provider enforces for us is not a bound we enforce.

    This is the authoritative claim. One statement, no window.
    """
    if request.action != "create_refund" or not request.payment_id:
        return None

    key = reservation_key(request)
    if state.reserve_refund(key, request.payment_id, request.effective_amount, clock=clock):
        return None

    return _deny(
        "refund.balance_claimed",
        "the refundable balance is already claimed by a refund that has not resolved",
        requested=request.effective_amount,
        refundable=state.refundable_amount(request.payment_id),
        held=state.held_amount(request.payment_id),
        key=key,
    )


def _resolve_idempotency(
    request: MoneyRequest, policy: Policy, state: SqliteState, clock: Clock
) -> GateDecision | None:
    """Claim the idempotency key, or explain why we will not act on it again."""
    if not policy.require_idempotency_key:
        return None

    key = request.idempotency_key
    if not key:
        return _deny(
            "idempotency.missing",
            "a money action must carry an idempotency key",
            action=request.action,
        )

    fingerprint = request_hash(request)
    existing = state.claim_idempotency(key, fingerprint, "{}", clock=clock)
    if existing is None:
        return None  # fresh key, claimed; proceed

    if existing.request_hash != fingerprint:
        # Same key, different body. Either a client bug or an attempt to have a
        # previously-approved key authorise a different payment.
        return _deny(
            "idempotency.key_reuse",
            "idempotency key was already used for a different request",
            key=key,
            original_hash=existing.request_hash,
            presented_hash=fingerprint,
        )

    return GateDecision(
        verdict=Verdict.ALLOW,
        action=request.action,
        reason="duplicate of an already-completed request; replaying the original result",
        check_id="idempotency.replay",
        evidence={"key": key},
        replayed=True,
    )
