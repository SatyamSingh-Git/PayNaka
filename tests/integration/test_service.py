"""End-to-end tests for the PayNaka service.

The demo endpoints are what a reviewer runs first, so they are tested as the demo rather
than as endpoints: the same run, with the gate off and then on, and an assertion on the
ledger both times. If this file passes, `make demo-attack` tells the truth.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from merchant.app import reset_catalog
from paynaka.app import AUTHORISED, GIFT, app, hub
from paynaka.identity import TokenRegistry
from paynaka.mode import Mode

pytestmark = pytest.mark.integration


#: A credential installed into the running app, so the MCP tests do not depend on
#: whatever the developer happens to have in their environment or in `.env`.
AGENT_TOKEN = "integration-test-token-long-enough"
AUTH = {"Authorization": f"Bearer {AGENT_TOKEN}"}

#: A distinct approver credential. Distinct is the point: a step-up the agent can
#: answer for itself is not an escalation.
APPROVER_TOKEN = "integration-approver-token-long-enough"
APPROVE = {"Authorization": f"Bearer {APPROVER_TOKEN}"}


@pytest.fixture
def client() -> Iterator[TestClient]:
    reset_catalog()
    with TestClient(app) as c:
        # After startup, because `hub.open()` rebuilds the registry from the environment.
        hub.callers = TokenRegistry({"integration-test": AGENT_TOKEN})
        hub.approvers = TokenRegistry({"ops-anita": APPROVER_TOKEN})
        yield c
    reset_catalog()


class TestTheDemo:
    def test_gate_off_the_attack_succeeds(self, client: TestClient) -> None:
        """Establishes that the attack is real. Without this, blocking it proves nothing."""
        body = client.post("/api/demo/attack?gate=false").json()
        assert body["money_moved"] > AUTHORISED
        assert body["overspent"] == 5_000_000
        assert not body["denials"]

    def test_gate_on_the_same_attack_moves_nothing(self, client: TestClient) -> None:
        body = client.post("/api/demo/attack?gate=true").json()
        assert body["money_moved"] == 0
        assert body["overspent"] == 0
        assert body["denials"][0]["check_id"] == "envelope.item_not_in_intent"

    def test_the_legitimate_purchase_still_completes(self, client: TestClient) -> None:
        """A gate that blocks the attack and the customer is an outage."""
        body = client.post("/api/demo/happy").json()
        assert body["money_moved"] == AUTHORISED
        assert body["overspent"] == 0
        assert not body["denials"]

    def test_the_denial_names_the_poisoned_field(self, client: TestClient) -> None:
        body = client.post("/api/demo/attack").json()
        assert body["poisoned_field"].startswith("reviews[")
        assert body["denials"][0]["evidence"]["sku"] == GIFT

    def test_the_response_says_what_the_scripted_agent_does_not_prove(
        self, client: TestClient
    ) -> None:
        """The endpoint must not let a reader mistake plumbing for susceptibility."""
        body = client.post("/api/demo/attack").json()
        assert "HAAT" in body["note"]
        assert "Scripted agent" in body["note"]

    def test_an_unknown_scenario_is_a_clean_404(self, client: TestClient) -> None:
        assert client.post("/api/demo/nonsense").status_code == 404

    def test_no_api_key_is_required(self, client: TestClient, monkeypatch) -> None:
        """A reviewer with a clone and one command must see the whole story."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("RAZORPAY_KEY_ID", raising=False)
        assert client.post("/api/demo/attack").json()["money_moved"] == 0


class TestAuditSurface:
    def test_the_chain_verifies_after_the_demo(self, client: TestClient) -> None:
        client.post("/api/demo/happy")
        client.post("/api/demo/attack")
        verified = client.get("/api/audit/verify").json()
        assert verified["intact"] is True
        assert verified["records"] > 0

    def test_denials_appear_in_the_chain(self, client: TestClient) -> None:
        client.post("/api/demo/attack")
        records = client.get("/api/audit").json()["records"]
        verdicts = [
            r["payload"]["decision"]["verdict"]
            for r in records
            if r["payload"]["kind"] == "decision"
        ]
        assert "DENY" in verdicts

    def test_the_head_moves_as_records_are_added(self, client: TestClient) -> None:
        before = client.get("/api/audit").json()["head"]
        client.post("/api/demo/happy")
        assert client.get("/api/audit").json()["head"] != before


class TestMcpEndpoint:
    def test_tools_list_over_http(self, client: TestClient) -> None:
        response = client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=AUTH
        ).json()
        names = {t["name"] for t in response["result"]["tools"]}
        assert "create_refund" in names

    def test_a_write_without_a_bound_mandate_is_refused(self, client: TestClient) -> None:
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "create_order", "arguments": {"amount": 100}},
            },
            headers={**AUTH, "mcp-session-id": "fresh"},
        ).json()
        assert response["error"]["code"] == -32001

    def test_malformed_json_is_a_400_not_a_500(self, client: TestClient) -> None:
        response = client.post(
            "/mcp", content=b"{not json", headers={**AUTH, "content-type": "application/json"}
        )
        assert response.status_code == 400

    def test_a_notification_returns_no_body(self, client: TestClient) -> None:
        response = client.post(
            "/mcp", json={"jsonrpc": "2.0", "method": "tools/list"}, headers=AUTH
        )
        assert response.json() is None


class TestTheAskingSurfaceIsAuthenticated:
    """Taking the credentials away from the agent buys nothing while the surface it asks
    through is open to everyone. These are the tests for that half of the claim."""

    def test_an_unauthenticated_call_is_refused(self, client: TestClient) -> None:
        response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert response.status_code == 401

    def test_the_refusal_tells_the_client_how_to_authenticate(self, client: TestClient) -> None:
        """A 401 without ``WWW-Authenticate`` is a 401 no client knows how to answer."""
        response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert response.headers["www-authenticate"].startswith("Bearer")

    @pytest.mark.parametrize(
        "header",
        [
            {},
            {"Authorization": ""},
            {"Authorization": "Bearer"},
            {"Authorization": f"Basic {AGENT_TOKEN}"},
            {"Authorization": "Bearer wrong-but-long-enough-to-be-plausible"},
            {"Authorization": f"Bearer {AGENT_TOKEN[:-1]}"},
            {"Authorization": f"Bearer {AGENT_TOKEN} "},
        ],
        ids=["absent", "empty", "no-token", "wrong-scheme", "wrong", "prefix", "padded"],
    )
    def test_every_way_of_getting_it_wrong_is_a_401(
        self, client: TestClient, header: dict[str, str]
    ) -> None:
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "create_order", "arguments": {"amount": 100}},
            },
            headers=header,
        )
        assert response.status_code == 401

    def test_a_refused_call_never_reaches_the_gate(self, client: TestClient) -> None:
        """An unauthenticated request is not a denied money request. It is not a money
        request at all, so it must leave no decision, no event and no audit record."""
        before_records = client.get("/api/audit").json()["count"]
        before_events = len(client.get("/api/events").json()["events"])
        client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "create_order", "arguments": {"amount": 5_000_000}},
            },
        )
        assert client.get("/api/audit").json()["count"] == before_records
        assert len(client.get("/api/events").json()["events"]) == before_events

    def test_the_refusal_does_not_leak_the_expected_credential(self, client: TestClient) -> None:
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Authorization": "Bearer wrong-but-long-enough-to-be-plausible"},
        )
        assert AGENT_TOKEN not in response.text
        assert AGENT_TOKEN not in str(dict(response.headers))

    def test_the_event_names_the_caller_not_the_credential(self, client: TestClient) -> None:
        """ "Which agent asked" is an audit question, and a session id does not answer it."""
        client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "create_order", "arguments": {"amount": 100}},
            },
            headers={**AUTH, "mcp-session-id": "named"},
        )
        calls = [e for e in client.get("/api/events").json()["events"] if e["kind"] == "mcp.call"]
        assert calls and calls[-1]["caller"] == "integration-test"
        assert AGENT_TOKEN not in str(calls)


class TestTheModeIsVisibleOverHttp:
    """An operator who believes they are protected and is not is the failure the mode
    reporting exists to prevent, so it must be legible without reading a config file."""

    def test_health_names_the_mode(self, client: TestClient) -> None:
        body = client.get("/api/health").json()
        assert body["mode"] == "enforce"
        assert body["enforcing"] is True

    def test_the_shadow_report_is_zeroed_when_enforcing(self, client: TestClient) -> None:
        """A zeroed report is the correct answer to "what did you let through": nothing."""
        client.post("/api/demo/attack")
        body = client.get("/api/shadow").json()
        assert body["enforcing"] is True
        assert body["observed"] == 0
        assert body["money_at_risk"] == 0

    def test_the_same_attack_observed_reports_what_it_let_through(self, client: TestClient) -> None:
        """The adoption pitch, over HTTP: run it for a week, read what it would have
        stopped, then decide whether to enforce."""
        hub.naka.mode = Mode.OBSERVE
        try:
            client.post("/api/demo/attack")
            body = client.get("/api/shadow").json()
        finally:
            hub.naka.mode = Mode.ENFORCE

        assert body["observed"] >= 1
        assert body["money_at_risk"] > 0
        assert body["top_check"] == "envelope.item_not_in_intent"
        assert body["money_at_risk_formatted"].startswith("₹")

    def test_the_report_survives_a_run_with_nothing_to_report(self, client: TestClient) -> None:
        body = client.get("/api/shadow").json()
        assert body["observed"] == 0
        assert body["top_check"] is None
        assert body["rate"] == 0


class TestTheApprovalSurface:
    """A step-up the buying agent can answer for itself is theatre, so the credential
    that approves is a different one and the endpoint checks it."""

    def test_the_queue_is_readable(self, client: TestClient) -> None:
        body = client.get("/api/escalations").json()
        assert body["on_timeout"] == "DENY"
        assert body["approvers_configured"] == 1
        assert body["pending"] == []

    def test_an_agent_credential_cannot_approve(self, client: TestClient) -> None:
        """The property the whole mechanism rests on."""
        response = client.post("/api/escalations/esc_anything/approve", headers=AUTH)
        assert response.status_code == 401

    def test_an_unauthenticated_caller_cannot_approve(self, client: TestClient) -> None:
        assert client.post("/api/escalations/esc_anything/approve").status_code == 401

    def test_an_unknown_escalation_does_not_reveal_itself(self, client: TestClient) -> None:
        """409 rather than 404, and the same 409 for already-decided and expired: a prober
        should not learn which escalation ids exist."""
        response = client.post("/api/escalations/esc_nope/approve", headers=APPROVE)
        assert response.status_code == 409

    @pytest.mark.parametrize("answer", ["maybe", "APPROVE", "yes", "approve-please", ""])
    def test_only_approve_and_deny_are_answers(self, client: TestClient, answer: str) -> None:
        response = client.post(f"/api/escalations/esc_x/{answer}", headers=APPROVE)
        assert response.status_code == 404

    def test_the_amount_is_rendered_for_the_human_who_must_decide(self, client: TestClient) -> None:
        """A person approving a payment should see rupees, not paise. The formatting is
        display only -- what actually releases the money is the request hash."""
        body = client.get("/api/escalations").json()
        assert "amount_formatted" not in body  # nothing pending, nothing to render
        assert body["timeout_seconds"] > 0


class TestPolicySurface:
    def test_policy_renders_amounts_for_humans(self, client: TestClient) -> None:
        body = client.get("/api/policy").json()
        assert body["actions"]["create_refund"]["daily_cap_formatted"].startswith("₹")

    def test_the_regulatory_block_is_exposed(self, client: TestClient) -> None:
        reg = client.get("/api/policy").json()["regulatory"]
        assert reg["npci_mandate_retries"] == 3
        assert reg["contact_window"] == "08:00-19:00 IST"
        assert reg["debit_blackout"] == ["10:00-13:00 IST"]

    def test_payouts_show_as_disabled(self, client: TestClient) -> None:
        assert client.get("/api/policy").json()["actions"]["create_payout"]["enabled"] is False


class TestServiceHygiene:
    def test_health_declares_test_mode(self, client: TestClient) -> None:
        """Never let a frame of the demo imply live money."""
        body = client.get("/api/health").json()
        assert body["test_mode"] is True
        assert body["rail"] in {"sim", "razorpay-test"}

    def test_events_are_bounded(self, client: TestClient) -> None:
        """A long-lived console must not turn the event log into a memory leak."""
        from paynaka.app import hub

        assert hub.events.maxlen == 500

    def test_cors_is_not_a_wildcard(self) -> None:
        """An odd thing to find in a project about bounded authority."""
        origins = [
            m.kwargs.get("allow_origins") for m in app.user_middleware if "CORS" in str(m.cls)
        ]
        assert origins and all("*" not in (o or []) for o in origins)

    def test_events_stream_back_for_the_console(self, client: TestClient) -> None:
        client.post("/api/demo/attack")
        kinds = {e["kind"] for e in client.get("/api/events").json()["events"]}
        assert {"poisoned", "run.started", "gate.denied", "run.finished"} <= kinds
