"""Adversarial tests for the MCP proxy.

The proxy is the trust boundary rendered as a network service, so it inherits every
concern the gate has plus the ones a transport brings: an agent it has never met, a
session it did not open, a JSON-RPC message that is not what it claims to be.

The property that matters most is **fail closed**. An MCP session that never presented a
mandate must be able to read and nothing else. The absence of an authorisation is not
permission to skip the check, and a proxy that treats "no mandate" as "no restrictions"
is worse than no proxy at all -- it would carry PayNaka's name while doing nothing.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

from paynaka.audit import AuditChain
from paynaka.clock import FrozenClock
from paynaka.engine import PayNaka
from paynaka.mandate import IntentMandate, MandateSigner, generate_keypair
from paynaka.policy import Policy
from paynaka.proxy.mcp import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    NO_MANDATE,
    PROTOCOL_VERSION,
    TOOLS,
    WRITE_TOOLS,
    McpProxy,
)
from paynaka.rails.sim import SimRail
from paynaka.state import SqliteState

pytestmark = pytest.mark.adversarial

AUTHORISED = 199_900
ATTACK = 5_200_000


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock.at_ist("2026-08-23 15:00")


@pytest.fixture
def signer() -> MandateSigner:
    return MandateSigner(generate_keypair()[0])


@pytest.fixture
def proxy(clock: FrozenClock, signer: MandateSigner) -> Iterator[McpProxy]:
    with (
        SqliteState(":memory:", clock=clock) as state,
        AuditChain(":memory:", clock=clock) as audit,
    ):
        naka = PayNaka(
            rail=SimRail(seed="mcp"),
            policy=Policy.from_yaml("policy.yaml"),
            state=state,
            audit=audit,
            verifier=signer.verifier(),
            clock=clock,
        )
        yield McpProxy(naka)


@pytest.fixture
def bound(proxy: McpProxy, signer: MandateSigner, clock: FrozenClock) -> McpProxy:
    mandate = IntentMandate.create(
        clock=clock,
        subject="cust_kirana_001",
        session_id="sess_mcp",
        max_total=AUTHORISED,
        allowed_skus=("ATTA-5KG",),
        allowed_destinations=("addr_home",),
        max_qty_per_sku=3,
        allowed_actions=("create_order", "capture_payment", "create_refund"),
    )
    proxy.bind("sess_mcp", signer.sign(mandate))
    return proxy


def rpc(
    proxy: McpProxy,
    method: str,
    params: dict | None = None,
    *,
    session: str = "sess_mcp",
    rid: int | str = 1,
):
    return proxy.handle(
        {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}},
        session_id=session,
    )


def payload(response: dict) -> dict:
    return json.loads(response["result"]["content"][0]["text"])


class TestFailsClosedWithoutAMandate:
    """The property that decides whether this proxy is real."""

    @pytest.mark.parametrize("tool", sorted(WRITE_TOOLS))
    def test_no_mandate_means_no_money_action(self, proxy: McpProxy, tool: str) -> None:
        response = rpc(
            proxy,
            "tools/call",
            {"name": tool, "arguments": {"amount": 1000, "payment_id": "pay_x"}},
            session="unbound",
        )
        assert response["error"]["code"] == NO_MANDATE

    def test_the_refusal_says_what_is_missing(self, proxy: McpProxy) -> None:
        response = rpc(
            proxy, "tools/call", {"name": "create_order", "arguments": {"amount": 1}}, session="u"
        )
        assert "no intent mandate" in response["error"]["message"]

    def test_reads_still_work_without_a_mandate(self, proxy: McpProxy) -> None:
        """A session with no authorisation is not a session with no access."""
        response = rpc(
            proxy,
            "tools/call",
            {"name": "fetch_all_payments", "arguments": {}},
            session="unbound",
        )
        assert "error" not in response

    def test_another_sessions_mandate_does_not_help(self, bound: McpProxy) -> None:
        """Binding is per session. Guessing a session id must not borrow its authority."""
        response = rpc(
            bound,
            "tools/call",
            {"name": "create_order", "arguments": {"amount": 1000}},
            session="sess_someone_else",
        )
        assert response["error"]["code"] == NO_MANDATE

    def test_unbinding_revokes_immediately(self, bound: McpProxy) -> None:
        bound.unbind("sess_mcp")
        response = rpc(bound, "tools/call", {"name": "create_order", "arguments": {"amount": 1000}})
        assert response["error"]["code"] == NO_MANDATE


class TestTheGateStillApplies:
    def test_an_authorised_order_goes_through(self, bound: McpProxy) -> None:
        response = rpc(
            bound,
            "tools/call",
            {
                "name": "create_order",
                "arguments": {
                    "amount": AUTHORISED,
                    "notes": {
                        "paynaka_items": [{"sku": "ATTA-5KG", "qty": 1, "unit_paise": AUTHORISED}],
                        "destination": "addr_home",
                    },
                },
            },
        )
        body = payload(response)
        assert body["status"] == "ok"
        assert body["amount"] == AUTHORISED
        assert body["order_id"].startswith("order_")

    def test_the_headline_attack_is_refused_through_mcp(self, bound: McpProxy) -> None:
        response = rpc(
            bound,
            "tools/call",
            {
                "name": "create_order",
                "arguments": {
                    "amount": ATTACK,
                    "notes": {
                        "paynaka_items": [
                            {"sku": "ATTA-5KG", "qty": 1, "unit_paise": AUTHORISED},
                            {"sku": "GIFT-50K", "qty": 1, "unit_paise": ATTACK - AUTHORISED},
                        ],
                        "destination": "addr_home",
                    },
                },
            },
        )
        body = payload(response)
        assert body["status"] == "blocked_by_paynaka"
        assert body["check"] == "envelope.item_not_in_intent"
        assert response["result"]["isError"] is True

    def test_an_order_with_no_declared_items_cannot_pass_a_sku_allow_list(
        self, bound: McpProxy
    ) -> None:
        """An opaque total has no items to check, so a SKU-scoped mandate must refuse it.

        Treating "no items declared" as "no items to object to" would be a hole big enough
        to drive the entire attack corpus through.
        """
        response = rpc(
            bound, "tools/call", {"name": "create_order", "arguments": {"amount": AUTHORISED}}
        )
        assert payload(response)["status"] == "blocked_by_paynaka"

    def test_a_refund_without_a_return_is_refused(self, bound: McpProxy) -> None:
        response = rpc(
            bound,
            "tools/call",
            {"name": "create_refund", "arguments": {"payment_id": "pay_x", "amount": 1000}},
        )
        assert payload(response)["status"] == "blocked_by_paynaka"

    def test_the_refusal_explains_that_retrying_will_not_help(self, bound: McpProxy) -> None:
        """A calling agent that retries forever is a denial of wallet even at zero rupees."""
        response = rpc(
            bound,
            "tools/call",
            {
                "name": "create_order",
                "arguments": {
                    "amount": ATTACK,
                    "notes": {
                        "paynaka_items": [{"sku": "GIFT-50K", "qty": 1, "unit_paise": ATTACK}]
                    },
                },
            },
        )
        assert "Retrying will not help" in payload(response)["hint"]


class TestRazorpayCompatibility:
    def test_tool_names_mirror_razorpay(self) -> None:
        """The one-URL-change claim is false the moment a name differs."""
        names = {t["name"] for t in TOOLS}
        assert {
            "create_order",
            "capture_payment",
            "create_refund",
            "fetch_payment",
            "fetch_all_payments",
        } <= names

    def test_create_refund_is_offered(self) -> None:
        """The tool Razorpay had to switch off. Its presence here is the entire pitch."""
        assert "create_refund" in {t["name"] for t in TOOLS}

    def test_amounts_are_documented_as_paise(self) -> None:
        order = next(t for t in TOOLS if t["name"] == "create_order")
        assert "paise" in order["inputSchema"]["properties"]["amount"]["description"]
        assert order["inputSchema"]["properties"]["amount"]["type"] == "integer"

    def test_every_tool_schema_is_closed(self) -> None:
        for tool in TOOLS:
            assert tool["inputSchema"]["additionalProperties"] is False

    def test_initialize_announces_a_protocol_version(self, proxy: McpProxy) -> None:
        result = rpc(proxy, "initialize")["result"]
        assert result["protocolVersion"] == PROTOCOL_VERSION
        assert result["serverInfo"]["name"] == "paynaka"

    def test_tools_list_needs_no_mandate(self, proxy: McpProxy) -> None:
        """Discovery must work before authorisation, or no client can ever start."""
        result = rpc(proxy, "tools/list", session="brand_new")["result"]
        assert len(result["tools"]) == len(TOOLS)


class TestMalformedJsonRpc:
    @pytest.mark.parametrize("message", [None, [], "hello", 42, True])
    def test_a_non_object_message_is_refused(self, proxy: McpProxy, message: object) -> None:
        assert proxy.handle(message)["error"]["code"] == INVALID_REQUEST

    @pytest.mark.parametrize("version", ["1.0", "2", 2.0, None, ""])
    def test_a_wrong_protocol_version_is_refused(self, proxy: McpProxy, version: object) -> None:
        response = proxy.handle({"jsonrpc": version, "id": 1, "method": "tools/list"})
        assert response["error"]["code"] == INVALID_REQUEST

    def test_an_unknown_method_is_refused(self, proxy: McpProxy) -> None:
        assert rpc(proxy, "tools/destroy")["error"]["code"] == METHOD_NOT_FOUND

    @pytest.mark.parametrize("params", ["string", 42, [1, 2]])
    def test_non_object_params_are_refused(self, proxy: McpProxy, params: object) -> None:
        response = proxy.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
        )
        assert response["error"]["code"] == INVALID_PARAMS

    def test_an_unknown_tool_is_refused(self, bound: McpProxy) -> None:
        response = rpc(bound, "tools/call", {"name": "drain_account", "arguments": {}})
        assert response["error"]["code"] == INVALID_PARAMS

    @pytest.mark.parametrize("amount", ["1000", 10.5, True, None, [1000]])
    def test_a_non_integer_amount_is_refused(self, bound: McpProxy, amount: object) -> None:
        """A string amount is where currency and unit confusion get in."""
        response = rpc(
            bound, "tools/call", {"name": "create_order", "arguments": {"amount": amount}}
        )
        assert response["error"]["code"] == INVALID_PARAMS

    def test_a_notification_never_gets_a_response(self, proxy: McpProxy) -> None:
        """JSON-RPC: a message with no id is a notification, even when it fails."""
        assert proxy.handle({"jsonrpc": "2.0", "method": "tools/list"}) is None
        assert proxy.handle({"jsonrpc": "2.0", "method": "nonsense"}) is None

    def test_the_id_is_echoed_back_exactly(self, proxy: McpProxy) -> None:
        for rid in (1, "abc", 0, -5):
            assert rpc(proxy, "tools/list", rid=rid)["id"] == rid

    def test_an_internal_failure_does_not_kill_the_transport(self, bound: McpProxy) -> None:
        response = rpc(
            bound,
            "tools/call",
            {"name": "fetch_payment", "arguments": {"payment_id": "pay_nope"}},
        )
        assert response["result"]["isError"] is True
        # ...and the proxy is still serving
        assert "error" not in rpc(bound, "tools/list")


class TestAuditing:
    def test_reads_are_audited(self, bound: McpProxy) -> None:
        before = len(bound.naka.audit)
        rpc(bound, "tools/call", {"name": "fetch_all_payments", "arguments": {}})
        records = bound.naka.audit.records()
        assert len(records) == before + 1
        assert records[-1].payload["kind"] == "mcp.read"

    def test_blocked_writes_are_audited(self, bound: McpProxy) -> None:
        rpc(
            bound,
            "tools/call",
            {
                "name": "create_order",
                "arguments": {
                    "amount": ATTACK,
                    "notes": {
                        "paynaka_items": [{"sku": "GIFT-50K", "qty": 1, "unit_paise": ATTACK}]
                    },
                },
            },
        )
        kinds = [r.payload["kind"] for r in bound.naka.audit.records()]
        assert "decision" in kinds

    def test_the_chain_survives_a_mixed_session(self, bound: McpProxy) -> None:
        rpc(bound, "tools/call", {"name": "fetch_all_payments", "arguments": {}})
        rpc(bound, "tools/call", {"name": "create_order", "arguments": {"amount": ATTACK}})
        rpc(bound, "tools/call", {"name": "create_order", "arguments": {"amount": 1}})
        assert bound.naka.audit.verify() is None

    def test_every_call_is_recorded_for_the_console(self, bound: McpProxy) -> None:
        rpc(bound, "tools/call", {"name": "fetch_all_payments", "arguments": {}})
        assert bound.calls[-1]["tool"] == "fetch_all_payments"
