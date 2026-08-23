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
from paynaka.app import AUTHORISED, GIFT, app

pytestmark = pytest.mark.integration


@pytest.fixture
def client() -> Iterator[TestClient]:
    reset_catalog()
    with TestClient(app) as c:
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
            "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
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
            headers={"mcp-session-id": "fresh"},
        ).json()
        assert response["error"]["code"] == -32001

    def test_malformed_json_is_a_400_not_a_500(self, client: TestClient) -> None:
        response = client.post(
            "/mcp", content=b"{not json", headers={"content-type": "application/json"}
        )
        assert response.status_code == 400

    def test_a_notification_returns_no_body(self, client: TestClient) -> None:
        response = client.post("/mcp", json={"jsonrpc": "2.0", "method": "tools/list"})
        assert response.json() is None


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
