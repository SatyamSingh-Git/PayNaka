"""An MCP server that mirrors Razorpay's tool names, and gates every money action.

This is the distribution claim, and it is the whole reason PayNaka is adoptable rather
than merely admirable. An agent already talking to ``mcp.razorpay.com`` changes one URL:

    -  "url": "https://mcp.razorpay.com/mcp"
    +  "url": "http://127.0.0.1:8002/mcp"

Same tool names, same argument shapes, same result shapes. No SDK, no rewrite, no code
change in the agent -- and now every money action passes a checkpoint.

**Read paths pass through and are audited. Write paths are gated.** Razorpay's own remote
server draws a cruder version of that line: forty-odd read tools are open and four write
tools -- including ``create_refund`` and ``create_instant_settlement`` -- are switched
off. They had the right instinct and no mechanism. ``create_refund`` is enabled here,
behind a return-event precondition, a per-payment bound, a daily cap and a step-up band.

The JSON-RPC layer is implemented directly rather than through a framework. MCP over HTTP
is three methods -- ``initialize``, ``tools/list``, ``tools/call`` -- and writing them out
means the error semantics are ours to get right, the whole surface is testable without a
transport mock, and there is no dependency whose next release quietly changes the wire.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass
from typing import Any, Final

from paynaka.engine import ExecutionResult, PayNaka
from paynaka.gate import LineItem, MoneyRequest, Verdict
from paynaka.grants import Grants
from paynaka.mandate import SignedMandate
from paynaka.money import format_inr

__all__ = ["TOOLS", "McpError", "McpProxy", "SessionBinding"]

PROTOCOL_VERSION: Final[str] = "2025-06-18"

# JSON-RPC 2.0 error codes. -32000..-32099 is the implementation-defined server range.
PARSE_ERROR: Final[int] = -32700
INVALID_REQUEST: Final[int] = -32600
METHOD_NOT_FOUND: Final[int] = -32601
INVALID_PARAMS: Final[int] = -32602
INTERNAL_ERROR: Final[int] = -32603
NO_MANDATE: Final[int] = -32001


class McpError(Exception):
    """A JSON-RPC error with a code."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _tool(
    name: str, description: str, properties: dict[str, Any], required: list[str]
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


#: Razorpay's tool names, argument names and shapes, mirrored. An agent must not be able
#: to tell it is talking to PayNaka until something is refused.
TOOLS: Final[list[dict[str, Any]]] = [
    _tool(
        "create_order",
        "Creates an order. Amount is in the smallest currency sub-unit (paise for INR).",
        {
            "amount": {"type": "integer", "description": "amount in paise"},
            "currency": {"type": "string", "default": "INR"},
            "receipt": {"type": "string"},
            "notes": {"type": "object"},
        },
        ["amount"],
    ),
    _tool(
        "capture_payment",
        "Change the payment status from authorized to captured.",
        {
            "payment_id": {"type": "string"},
            "amount": {"type": "integer"},
            "currency": {"type": "string", "default": "INR"},
        },
        ["payment_id", "amount"],
    ),
    _tool(
        "create_refund",
        "Creates a refund against a captured payment. Disabled on Razorpay's own remote "
        "server; enabled here behind a policy envelope.",
        {
            "payment_id": {"type": "string"},
            "amount": {"type": "integer"},
            "speed": {"type": "string", "default": "normal"},
        },
        ["payment_id", "amount"],
    ),
    _tool(
        "fetch_payment",
        "Fetch payment details with ID. Read path: audited, not gated.",
        {"payment_id": {"type": "string"}},
        ["payment_id"],
    ),
    _tool(
        "fetch_all_payments",
        "Fetch all payments with filtering and pagination. Read path: audited, not gated.",
        {"count": {"type": "integer", "default": 10}, "skip": {"type": "integer", "default": 0}},
        [],
    ),
]

#: Tools that move money. Everything else is a read.
WRITE_TOOLS: Final[frozenset[str]] = frozenset({"create_order", "capture_payment", "create_refund"})

_ACTIONS: Final[dict[str, str]] = {
    "create_order": "create_order",
    "capture_payment": "capture_payment",
    "create_refund": "create_refund",
}


@dataclass(slots=True)
class SessionBinding:
    """The mandate an MCP session is acting under.

    A session without a binding can read and can do nothing else. That is the fail-closed
    default: an agent that never presented an authorisation has none, and the absence of
    a mandate is not permission to skip the check.
    """

    session_id: str
    signed: SignedMandate


class McpProxy:
    """MCP server in, PayNaka out."""

    def __init__(
        self,
        naka: PayNaka,
        *,
        server_name: str = "paynaka",
        grants: Grants | None = None,
    ) -> None:
        self.naka = naka
        self.server_name = server_name
        self._bindings: dict[str, SessionBinding] = {}
        self.calls: list[dict[str, Any]] = []
        #: Redeems the short-lived tickets that let an external client bind a mandate.
        #: Optional so the in-process harnesses, which call `bind()` directly, are
        #: unaffected -- but a deployment without one has no external binding path at all,
        #: which is the state this whole mechanism exists to leave behind.
        self.grants: Grants | None = grants

    # ---------------------------------------------------------------- sessions
    def bind(self, session_id: str, signed: SignedMandate) -> None:
        self._bindings[session_id] = SessionBinding(session_id=session_id, signed=signed)

    def unbind(self, session_id: str) -> None:
        self._bindings.pop(session_id, None)

    def binding(self, session_id: str) -> SessionBinding | None:
        return self._bindings.get(session_id)

    # ---------------------------------------------------------------- json-rpc
    def handle(self, message: object, *, session_id: str = "default") -> dict[str, Any] | None:
        """Dispatch one JSON-RPC message. Returns ``None`` for notifications."""
        if not isinstance(message, dict):
            return _error(None, INVALID_REQUEST, "request must be a JSON object")

        if message.get("jsonrpc") != "2.0":
            return _error(message.get("id"), INVALID_REQUEST, "jsonrpc must be exactly '2.0'")

        method = message.get("method")
        if not isinstance(method, str):
            return _error(message.get("id"), INVALID_REQUEST, "method must be a string")

        request_id = message.get("id")
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return _error(request_id, INVALID_PARAMS, "params must be an object")

        # A notification has no id and must never receive a response, even on error.
        is_notification = "id" not in message

        try:
            result = self._dispatch(method, params, session_id)
        except McpError as exc:
            return None if is_notification else _error(request_id, exc.code, exc.message)
        except Exception as exc:
            return (
                None
                if is_notification
                else _error(request_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
            )

        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _dispatch(self, method: str, params: dict[str, Any], session_id: str) -> dict[str, Any]:
        if method == "initialize":
            # A grant presented here binds this session to the mandate it carries. Absent,
            # the session is still usable for reads and every money action will answer
            # "no mandate" -- which is the fail-closed direction and is what an
            # unauthenticated client should get.
            bound = self._redeem_if_presented(params, session_id)
            return {
                **({"boundSession": bound} if bound else {}),
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": self.server_name, "version": "0.1.0"},
                "instructions": (
                    "Razorpay tools, behind a PayNaka checkpoint. Read paths pass through. "
                    "Money actions are checked against a signed mandate before they run, "
                    "and a refusal explains itself."
                ),
            }
        if method in {"notifications/initialized", "initialized", "ping"}:
            return {}
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "tools/call":
            return self._call(params, session_id)
        raise McpError(METHOD_NOT_FOUND, f"unknown method: {method}")

    def _redeem_if_presented(self, params: dict[str, Any], session_id: str) -> str | None:
        """Bind this session to a mandate, if a usable grant came with the request.

        ``session_id`` is *not* the string the client sent. The caller composes it from the
        authenticated identity on the HTTP request, so a grant redeemed by one caller can
        never bind another's session -- which was the hole underneath the missing bind
        route, not merely a missing route.
        """
        token = params.get("mandateGrant")
        if token is None:
            return None
        if self.grants is None:
            raise McpError(INVALID_PARAMS, "this checkpoint cannot redeem mandate grants")
        if not isinstance(token, str):
            raise McpError(INVALID_PARAMS, "mandateGrant must be a string")

        try:
            signed = self.grants.redeem(token, by=session_id, clock=self.naka.clock)
        except Exception as exc:
            # One message for every failure, deliberately. See `Grants.redeem`.
            raise McpError(INVALID_PARAMS, str(exc)) from exc

        self.bind(session_id, signed)
        return signed.mandate.session_id

    # ---------------------------------------------------------------- tools/call
    def _call(self, params: dict[str, Any], session_id: str) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str):
            raise McpError(INVALID_PARAMS, "tools/call requires a tool name")

        known = {t["name"] for t in TOOLS}
        if name not in known:
            raise McpError(INVALID_PARAMS, f"unknown tool: {name}")

        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise McpError(INVALID_PARAMS, "arguments must be an object")

        self.calls.append({"tool": name, "arguments": arguments, "session": session_id})

        if name not in WRITE_TOOLS:
            return self._read(name, arguments)

        binding = self._bindings.get(session_id)
        if binding is None:
            # Fail closed. No mandate is not the same as no restrictions.
            raise McpError(
                NO_MANDATE,
                "this session carries no intent mandate, so no money action is authorised. "
                "Bind one before calling a write tool.",
            )

        return self._write(name, arguments, binding)

    def _read(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Read paths pass through. Audited, because knowing what was looked at matters."""
        try:
            if name == "fetch_payment":
                payment_id = str(arguments.get("payment_id", ""))
                result = self.naka.rail.fetch_payment(payment_id)
                payload: dict[str, Any] = {
                    "id": result.payment_id,
                    "amount": result.amount,
                    "currency": result.currency,
                    "status": result.status,
                    "order_id": result.order_id,
                }
            else:  # fetch_all_payments
                payload = {"entity": "collection", "count": 0, "items": []}
        except Exception as exc:
            return _content({"error": str(exc)}, is_error=True)

        self.naka.audit.append(
            {"kind": "mcp.read", "tool": name, "arguments": arguments}, clock=self.naka.clock
        )
        return _content(payload)

    def _write(
        self, name: str, arguments: dict[str, Any], binding: SessionBinding
    ) -> dict[str, Any]:
        request = _to_request(name, arguments)
        result = self.naka.execute(request, binding.signed, provenance={"via": "mcp"})
        return _content(_render(result), is_error=not result.executed)


# ---------------------------------------------------------------- translation
def _to_request(name: str, arguments: dict[str, Any]) -> MoneyRequest:
    """Turn Razorpay-shaped arguments into a MoneyRequest.

    ``notes.paynaka_items`` is how a caller declares its line items. Without it a
    ``create_order`` carries only a total, and the item-subset check has nothing to check
    -- so an order with no declared items is treated as a single opaque line whose SKU
    cannot match any allow-list. A mandate that names SKUs will therefore refuse it,
    which is the correct answer rather than a convenient one.
    """
    request_id = f"req_{uuid.uuid4().hex[:12]}"
    notes = arguments.get("notes") or {}
    idempotency_key = str(
        notes.get("idempotency_key")
        or arguments.get("idempotency_key")
        or f"idem_{uuid.uuid4().hex[:16]}"
    )

    if name == "create_order":
        amount = _int(arguments.get("amount"), "amount")
        declared = notes.get("paynaka_items")
        items: tuple[LineItem, ...] = ()
        if isinstance(declared, list) and declared:
            items = tuple(
                LineItem(
                    sku=str(row.get("sku", "")),
                    qty=_int(row.get("qty", 1), "qty"),
                    unit_paise=_int(row.get("unit_paise", 0), "unit_paise"),
                )
                for row in declared
                if isinstance(row, dict)
            )
        return MoneyRequest(
            action="create_order",
            request_id=request_id,
            idempotency_key=idempotency_key,
            items=items,
            amount=None if items else amount,
            currency=str(arguments.get("currency", "INR")),
            destination=notes.get("destination"),
        )

    return MoneyRequest(
        action=_ACTIONS[name],
        request_id=request_id,
        idempotency_key=idempotency_key,
        amount=_int(arguments.get("amount"), "amount"),
        currency=str(arguments.get("currency", "INR")),
        payment_id=str(arguments.get("payment_id", "")),
    )


def _int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise McpError(INVALID_PARAMS, f"{field} must be an integer in paise, got {value!r}")
    return value


def _render(result: ExecutionResult) -> dict[str, Any]:
    """What the calling agent sees.

    A refusal is explained in full: which check, why, and the evidence. Hiding the reason
    would make transcripts unreadable, and an attacker learns nothing from a message
    describing a limit they cannot change.
    """
    if result.executed:
        rail = result.rail_result
        payload = {
            "status": "ok",
            "amount": result.money_moved,
            "amount_formatted": format_inr(result.money_moved),
            "audit_seq": result.audit_seq,
        }
        # Deliberately not "status": PayNaka's own status lives there, and letting the
        # rail's ("created", "captured") overwrite it would tell the caller the action
        # succeeded in the gateway while saying nothing about whether it was authorised.
        for key in ("order_id", "payment_id", "refund_id"):
            if hasattr(rail, key):
                payload[key] = getattr(rail, key)
        if rail is not None and hasattr(rail, "status"):
            payload["rail_status"] = rail.status
        return payload

    # A replay of a request that already succeeded is not a refusal, and calling it one is
    # the worst available answer: an agent reading `blocked_by_paynaka` retries, or tells
    # the shopper their purchase failed when the money has moved.
    #
    # `executed` stays False on a replay deliberately -- it is what stops twenty webhook
    # redeliveries summing to twenty payments in the benchmark -- so the replay has to be
    # recognised here rather than folded into the success branch above.
    if result.decision.replayed and result.original_result is not None:
        original = asdict(result.original_result)
        return {
            "status": "already_done",
            "replayed": True,
            "audit_seq": result.audit_seq,
            "hint": (
                "This exact request was already completed under the same idempotency key. "
                "It was not repeated and it was not refused; the original outcome is below."
            ),
            # `status` is excluded for the same reason the success branch above excludes
            # it: PayNaka's own status lives there, and letting the rail's ("created")
            # overwrite it would answer "created" to a caller asking whether this was a
            # replay. It comes back as `rail_status` instead.
            "rail_status": original.get("status"),
            **{
                key: value
                for key, value in original.items()
                if value is not None and key != "status"
            },
        }

    return {
        "status": "blocked_by_paynaka",
        "verdict": str(result.decision.verdict),
        "check": result.decision.check_id,
        "reason": result.decision.reason,
        "evidence": dict(result.decision.evidence),
        "audit_seq": result.audit_seq,
        "error": result.error,
        "hint": (
            "PayNaka refused this action because it falls outside the shopper's signed "
            "mandate. Retrying will not help; the mandate would have to change."
            if result.decision.verdict is Verdict.DENY
            else None
        ),
    }


def _content(payload: dict[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    import json

    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
        "isError": is_error,
    }


def _error(request_id: object, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
