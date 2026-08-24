"""The external MCP path, black-box, with no internal helper anywhere in it.

An independent review found that `McpProxy.bind()` was called from exactly one place: a
test fixture. An agent pointed at `/mcp` could initialize, list tools and call read tools,
and every money action answered "no mandate for this session". The documented product — a
drop-in checkpoint in front of an MCP server — had no working path for the thing it exists
to do, and the polished local demo hid that by going through `ToolBox → PayNaka.execute`
instead.

Underneath the missing route was a worse problem: session identity came from an
`mcp-session-id` header that nothing checked, so any authenticated caller could name any
session. A binding, had one existed, would have been claimable by whoever asked.

Every test here drives HTTP only. The rule is deliberate — a test that reaches for
`proxy.bind()` proves the object works and says nothing about whether anybody can get to it,
which was precisely the defect.

The acceptance sequence the review asked for, in order:
authenticate → issue intent → bind the session → list tools → call a read tool → create an
allowed order → reject replay, wrong session, and an unbound caller.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from merchant.app import reset_catalog

AGENT_TOKEN = "mcp-binding-agent-token-long-enough"
AUTH = {"Authorization": f"Bearer {AGENT_TOKEN}"}

OTHER_TOKEN = "mcp-binding-other-agent-token-long"
OTHER = {"Authorization": f"Bearer {OTHER_TOKEN}"}

ATTA = "ATTA-5KG"
HOME = "addr_home"
BUDGET = 199_900


@pytest.fixture
def client() -> Iterator[TestClient]:
    """Two authenticated callers, so "one caller cannot use another's session" is testable.

    The registry is replaced after startup, the same way the other integration file does
    it: `hub.open()` rebuilds it from the environment, so setting it before would be
    overwritten.
    """
    from paynaka.app import app, hub
    from paynaka.identity import TokenRegistry

    reset_catalog()
    with TestClient(app) as test_client:
        hub.callers = TokenRegistry({"buyer": AGENT_TOKEN, "other": OTHER_TOKEN})
        yield test_client
    reset_catalog()


def rpc(
    client: TestClient,
    method: str,
    params: dict[str, object] | None = None,
    *,
    headers: dict[str, str] | None = None,
    session: str = "s1",
    request_id: int = 1,
) -> dict[str, object]:
    response = client.post(
        "/mcp",
        headers={**(headers or AUTH), "mcp-session-id": session},
        json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}},
    )
    assert response.status_code == 200, response.text
    return response.json()  # type: ignore[no-any-return]


def issue_intent(client: TestClient, *, headers: dict[str, str] | None = None) -> dict[str, object]:
    response = client.post(
        "/api/intent",
        headers=headers or AUTH,
        json={
            "subject": "cust_kirana_001",
            "session_id": "sess_mcp",
            "budget_paise": BUDGET,
            "skus": [ATTA],
            "destinations": [HOME],
            "max_qty_per_sku": 1,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()  # type: ignore[no-any-return]


def order_args(amount: int = BUDGET, key: str = "idem_1") -> dict[str, object]:
    return {
        "amount": amount,
        "currency": "INR",
        "receipt": key,
        "notes": {"paynaka_items": f"{ATTA}:1:{amount}", "paynaka_destination": HOME},
    }


class TestTheAcceptanceSequence:
    def test_the_whole_flow_end_to_end_over_http(self, client: TestClient) -> None:
        """The sequence the review specified, in one test, so a reviewer can read it as a
        story rather than assembling it from six."""
        # 1. authenticate + issue intent
        issued = issue_intent(client)
        assert issued["mandate_grant"]

        # 2. bind the session by redeeming the grant at initialize
        initialised = rpc(client, "initialize", {"mandateGrant": issued["mandate_grant"]})
        assert initialised["result"]["boundSession"] == "sess_mcp"  # type: ignore[index]

        # 3. list tools
        listed = rpc(client, "tools/list", request_id=2)
        assert listed["result"]["tools"]  # type: ignore[index]

        # 4. a read tool passes through
        read = rpc(
            client,
            "tools/call",
            {"name": "fetch_payment", "arguments": {"payment_id": "pay_x"}},
            request_id=3,
        )
        assert "error" not in read

        # 5. an allowed money action now works, where it used to answer "no mandate"
        created = rpc(
            client, "tools/call", {"name": "create_order", "arguments": order_args()}, request_id=4
        )
        assert "error" not in created, created

    def test_without_a_grant_a_money_action_is_refused(self, client: TestClient) -> None:
        """The control, and the old behaviour. Reads still work; writes do not."""
        rpc(client, "initialize")
        answer = rpc(
            client, "tools/call", {"name": "create_order", "arguments": order_args()}, request_id=2
        )
        rendered = str(answer)
        assert "mandate" in rendered.lower()

    def test_a_read_tool_works_without_any_binding(self, client: TestClient) -> None:
        """Reads were never the problem, and must not become collateral damage."""
        rpc(client, "initialize")
        read = rpc(
            client,
            "tools/call",
            {"name": "fetch_payment", "arguments": {"payment_id": "pay_x"}},
            request_id=2,
        )
        assert "error" not in read


class TestAGrantIsSpentOnce:
    def test_redeeming_the_same_grant_twice_is_refused(self, client: TestClient) -> None:
        """A ticket that can be replayed is a long-lived credential with a short-lived
        name."""
        issued = issue_intent(client)
        first = rpc(client, "initialize", {"mandateGrant": issued["mandate_grant"]})
        second = rpc(
            client,
            "initialize",
            {"mandateGrant": issued["mandate_grant"]},
            session="s2",
            request_id=2,
        )
        assert "boundSession" in first["result"]  # type: ignore[operator]
        assert "error" in second, second

    @pytest.mark.parametrize(
        "token",
        ["", "not-a-real-grant", "x" * 64, "../../etc/passwd", "null"],
        ids=repr,
    )
    def test_a_forged_grant_binds_nothing(self, client: TestClient, token: str) -> None:
        answer = rpc(client, "initialize", {"mandateGrant": token})
        assert "error" in answer, answer

    def test_a_non_string_grant_is_refused(self, client: TestClient) -> None:
        answer = rpc(client, "initialize", {"mandateGrant": {"clever": True}})
        assert "error" in answer

    def test_the_refusal_does_not_say_which_kind_of_wrong_it_was(self, client: TestClient) -> None:
        """Unknown, spent and expired must be indistinguishable, or a prober learns whether
        a token they guessed ever existed."""
        issued = issue_intent(client)
        rpc(client, "initialize", {"mandateGrant": issued["mandate_grant"]})
        spent = rpc(client, "initialize", {"mandateGrant": issued["mandate_grant"]}, request_id=2)
        forged = rpc(client, "initialize", {"mandateGrant": "never-existed"}, request_id=3)
        assert spent["error"]["message"] == forged["error"]["message"]  # type: ignore[index]


class TestOneCallerCannotUseAnothersSession:
    def test_a_binding_does_not_carry_across_callers(self, client: TestClient) -> None:
        """The hole underneath the missing route: `mcp-session-id` is a claim about who you
        are that nothing checked. Session identity is composed from the *authenticated*
        caller, so naming somebody else's session buys nothing."""
        issued = issue_intent(client)
        rpc(client, "initialize", {"mandateGrant": issued["mandate_grant"]}, session="shared")

        # A different authenticated caller, naming the identical client session string.
        answer = rpc(
            client,
            "tools/call",
            {"name": "create_order", "arguments": order_args(key="idem_other")},
            headers=OTHER,
            session="shared",
            request_id=2,
        )
        assert "mandate" in str(answer).lower(), answer

    def test_the_same_caller_keeps_its_binding_across_calls(self, client: TestClient) -> None:
        """The other half. A binding that did not persist would make the flow unusable."""
        issued = issue_intent(client)
        rpc(client, "initialize", {"mandateGrant": issued["mandate_grant"]}, session="keep")
        first = rpc(
            client,
            "tools/call",
            {"name": "create_order", "arguments": order_args(key="k1")},
            session="keep",
            request_id=2,
        )
        assert "error" not in first, first

    def test_a_different_client_session_of_the_same_caller_is_not_bound(
        self, client: TestClient
    ) -> None:
        """Binding is per session, not per credential. One authenticated agent running two
        shopping trips must not have the second inherit the first's authority."""
        issued = issue_intent(client)
        rpc(client, "initialize", {"mandateGrant": issued["mandate_grant"]}, session="trip_a")
        answer = rpc(
            client,
            "tools/call",
            {"name": "create_order", "arguments": order_args(key="k_b")},
            session="trip_b",
            request_id=2,
        )
        assert "mandate" in str(answer).lower(), answer


class TestTheIssuingSurfaceIsNotOpen:
    def test_issuing_a_mandate_requires_authentication(self, client: TestClient) -> None:
        """It used to be open. Anybody who could reach the port could mint themselves a
        mandate, and the checkpoint would have verified it perfectly -- because it was
        genuinely signed."""
        response = client.post(
            "/api/intent",
            json={
                "subject": "c",
                "session_id": "s",
                "budget_paise": BUDGET,
                "skus": [ATTA],
                "destinations": [HOME],
            },
        )
        assert response.status_code == 401

    def test_the_response_names_who_it_was_issued_to(self, client: TestClient) -> None:
        """So an audit can answer "who asked for this authority", which a session id
        cannot."""
        assert issue_intent(client)["issued_to"] == "buyer"

    def test_the_grant_is_not_the_mandate(self, client: TestClient) -> None:
        """A short-lived ticket, not the signed authority itself. The mandate should not be
        travelling on every session-init, logged by everything in between."""
        issued = issue_intent(client)
        assert issued["mandate_grant"] != issued["signed"]
        assert isinstance(issued["grant_expires_at"], int)
