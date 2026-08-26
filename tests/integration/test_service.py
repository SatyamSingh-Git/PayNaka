"""End-to-end tests for the PayNaka service.

The demo endpoints are what a reviewer runs first, so they are tested as the demo rather
than as endpoints: the same run, with the gate off and then on, and an assertion on the
ledger both times. If this file passes, `make demo-attack` tells the truth.
"""

from __future__ import annotations

import json
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

#: The credential that *creates* authority, and a third distinct set. The agent's token
#: opened this route until an audit asked who is allowed to create the constraint; the
#: answer was "the constrained agent". The entry's name is the subject it can issue for.
SHOPPER_TOKEN = "integration-shopper-token-long-enough"
SHOP = {"Authorization": f"Bearer {SHOPPER_TOKEN}"}
SUBJECT = "cust_kirana_001"

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
        hub.shoppers = TokenRegistry({SUBJECT: SHOPPER_TOKEN})
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


NEWLINE = chr(10)


class TestTheMetricsSurface:
    """An audit chain nobody watches breaks quietly. These are what makes it watched."""

    def test_the_exposition_scrapes(self, client: TestClient) -> None:
        client.post("/api/demo/attack")
        body = client.get("/metrics").text
        assert "paynaka_decisions_total" in body
        assert "paynaka_denied_total" in body
        assert body.endswith(NEWLINE)

    def test_the_alarm_metric_reports_a_healthy_chain(self, client: TestClient) -> None:
        client.post("/api/demo/happy")
        assert "paynaka_audit_chain_intact 1" in client.get("/metrics").text

    def test_a_scrape_can_decline_to_pay_for_verification(self, client: TestClient) -> None:
        """A full rehash on every fifteen-second scrape is a real cost on a long chain, so
        it is opt-out -- but verifying is the default, because tamper detection that
        defaults to not looking is worse than none."""
        assert "paynaka_audit_chain_intact 1" in client.get("/metrics?verify=false").text

    def test_the_denial_shows_up_attributed_to_its_check(self, client: TestClient) -> None:
        client.post("/api/demo/attack")
        body = client.get("/metrics").text
        assert 'check_id="envelope.item_not_in_intent"' in body

    def test_money_moved_is_paise_and_matches_the_demo(self, client: TestClient) -> None:
        client.post("/api/demo/happy")
        body = client.get("/api/metrics").json()
        assert body["money_moved"] == AUTHORISED
        assert body["money_moved_formatted"].startswith("₹")

    def test_the_json_and_the_exposition_agree(self, client: TestClient) -> None:
        """Two renderings of one derivation. If these disagree, one of them is a second
        source of truth, which is the thing this module exists not to be."""
        client.post("/api/demo/attack")
        body = client.get("/api/metrics").json()
        text = client.get("/metrics").text
        assert f"paynaka_decisions_total {body['decisions']}" in text
        assert f"paynaka_denied_total {body['denied']}" in text


class TestTheIntentSurface:
    """Where a mandate comes from. Everything else in the system is downstream of this,
    and until now nothing in the repository produced one outside a test fixture."""

    def test_stated_intent_becomes_a_signed_mandate(self, client: TestClient) -> None:
        body = client.post(
            "/api/intent",
            headers=SHOP,
            json={
                "subject": "cust_kirana_001",
                "session_id": "sess_http",
                "budget_paise": 199_900,
                "skus": ["ATTA-5KG"],
                "destinations": ["addr_home"],
            },
        ).json()
        assert body["signed"]["mandate"]["max_total"] == 199_900
        assert body["signed"]["signature"]
        assert body["budget_formatted"].startswith("₹")

    def test_it_records_when_intent_was_frozen(self, client: TestClient) -> None:
        """The ordering the design rests on, made a matter of record."""
        body = client.post(
            "/api/intent",
            headers=SHOP,
            json={
                "subject": SUBJECT,
                "session_id": "s",
                "budget_paise": 199_900,
                "skus": ["ATTA-5KG"],
                "destinations": ["addr_home"],
            },
        ).json()
        assert body["frozen_at"] > 0

    @pytest.mark.parametrize(
        ("payload", "why"),
        [
            ({"skus": []}, "no SKU allow-list is a blank cheque"),
            ({"destinations": []}, "goods could go anywhere"),
            ({"budget_paise": 0}, "a budget that authorises nothing"),
            ({"budget_paise": -1}, "a negative budget"),
            ({"ttl_seconds": 99_999_999}, "authority left lying around"),
        ],
        ids=["no-skus", "no-destinations", "zero-budget", "negative-budget", "huge-window"],
    )
    def test_an_unbounded_intent_is_a_400_not_a_500(
        self, client: TestClient, payload: dict[str, object], why: str
    ) -> None:
        """Nothing went wrong; something was declined."""
        base = {
            "subject": SUBJECT,
            "session_id": "s",
            "budget_paise": 199_900,
            "skus": ["ATTA-5KG"],
            "destinations": ["addr_home"],
        }
        response = client.post("/api/intent", headers=SHOP, json={**base, **payload})
        assert response.status_code == 400, why


class TestTheWebhookSurface:
    """An unverified webhook is an instruction to write the ledger, from anybody at all."""

    def test_an_unsigned_webhook_is_refused(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "a-real-endpoint-secret-value")
        response = client.post("/webhooks/razorpay", json={"event": "payment.captured"})
        assert response.status_code == 401

    def test_a_wrongly_signed_webhook_is_refused(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "a-real-endpoint-secret-value")
        response = client.post(
            "/webhooks/razorpay",
            json={"event": "payment.captured"},
            headers={"X-Razorpay-Signature": "0" * 64},
        )
        assert response.status_code == 401

    def test_a_correctly_signed_webhook_is_accepted(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import hashlib
        import hmac

        secret = "a-real-endpoint-secret-value"
        monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", secret)
        body = json.dumps(
            {
                "event": "payment.captured",
                "id": "evt_1",
                "payload": {"payment": {"entity": {"id": "pay_1", "amount": 199_900}}},
            }
        ).encode("utf-8")
        signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        response = client.post(
            "/webhooks/razorpay",
            content=body,
            headers={"X-Razorpay-Signature": signature, "content-type": "application/json"},
        )
        assert response.status_code == 200
        assert response.json()["payment_id"] == "pay_1"

    def test_with_no_secret_configured_nothing_is_accepted(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """503, not 200. There is no development mode that skips verification."""
        monkeypatch.delenv("RAZORPAY_WEBHOOK_SECRET", raising=False)
        monkeypatch.setattr("paynaka.webhooks.os.environ", {})
        response = client.post("/webhooks/razorpay", json={"event": "payment.captured"})
        assert response.status_code == 503


class TestTheWebhookRouteProcessesRatherThanAcknowledges:
    """Verification answers *who sent this*. Processing is a separate question, and the
    route used to stop at the first one -- it acknowledged a verified event and applied
    nothing, which made the reconciliation claim true of the engine and untrue of the
    deployed path.

    Driven black-box through HTTP, because an internal helper proves nothing about a route.
    """

    SECRET = "a-real-endpoint-secret-value"

    def _post(self, client, body: bytes, *, event_id: str | None = "evt_1", sign: bool = True):  # type: ignore[no-untyped-def]
        import hashlib
        import hmac

        headers = {"content-type": "application/json"}
        if sign:
            headers["X-Razorpay-Signature"] = hmac.new(
                self.SECRET.encode(), body, hashlib.sha256
            ).hexdigest()
        if event_id:
            headers["X-Razorpay-Event-Id"] = event_id
        return client.post("/webhooks/razorpay", content=body, headers=headers)

    def _captured(self, amount: int = 199_900, payment_id: str = "pay_route") -> bytes:
        return json.dumps(
            {
                "event": "payment.captured",
                "payload": {"payment": {"entity": {"id": payment_id, "amount": amount}}},
            }
        ).encode("utf-8")

    @pytest.fixture(autouse=True)
    def _secret(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", self.SECRET)

    def test_a_verified_capture_is_applied(self, client: TestClient) -> None:
        body = self._captured()
        answer = self._post(client, body).json()
        assert answer["accepted"] is True
        assert answer["duplicate"] is False
        assert answer["applied"] == "capture_recorded"

    def test_the_event_id_comes_from_the_header(self, client: TestClient) -> None:
        """Where Razorpay documents it. This module originally read an `id` off the top of
        the body -- a shape I invented -- so a redelivery would have arrived with no
        dependable id and duplicate suppression would have had nothing to work with."""
        answer = self._post(client, self._captured(), event_id="evt_from_header").json()
        assert answer["event_id"] == "evt_from_header"

    def test_a_redelivery_is_suppressed_rather_than_reapplied(self, client: TestClient) -> None:
        """At-least-once is the only delivery guarantee on offer, so this is ordinary
        traffic. `make chaos` measures what applying one twice costs: Rs 3,994."""
        body = self._captured(payment_id="pay_dup")
        first = self._post(client, body, event_id="evt_dup").json()
        second = self._post(client, body, event_id="evt_dup").json()
        assert first["duplicate"] is False
        assert second["duplicate"] is True
        assert second["applied"] is None

    def test_a_redelivery_still_returns_200(self, client: TestClient) -> None:
        """A duplicate is not an error. Anything but a 200 makes the provider retry it."""
        body = self._captured(payment_id="pay_ack")
        self._post(client, body, event_id="evt_ack")
        assert self._post(client, body, event_id="evt_ack").status_code == 200

    def test_two_distinct_events_are_both_applied(self, client: TestClient) -> None:
        """The control. Suppressing everything would satisfy the duplicate test above."""
        a = self._post(client, self._captured(payment_id="pay_a"), event_id="evt_a").json()
        b = self._post(client, self._captured(payment_id="pay_b"), event_id="evt_b").json()
        assert a["applied"] == "capture_recorded"
        assert b["applied"] == "capture_recorded"

    def test_an_unsigned_event_is_never_processed(self, client: TestClient) -> None:
        response = self._post(client, self._captured(), sign=False)
        assert response.status_code == 401

    def test_a_forged_signature_is_never_processed(self, client: TestClient) -> None:
        body = self._captured()
        import hashlib
        import hmac

        wrong = hmac.new(b"the-wrong-endpoint-secret", body, hashlib.sha256).hexdigest()
        response = client.post(
            "/webhooks/razorpay",
            content=body,
            headers={"X-Razorpay-Signature": wrong, "content-type": "application/json"},
        )
        assert response.status_code == 401

    def test_an_event_this_route_does_not_act_on_is_still_acknowledged(
        self, client: TestClient
    ) -> None:
        """Out-of-order and unknown lifecycle events arrive. Refusing them would make the
        provider retry something this system has no transition for."""
        body = json.dumps({"event": "payment.failed", "payload": {}}).encode("utf-8")
        answer = self._post(client, body, event_id="evt_failed").json()
        assert answer["accepted"] is True
        assert answer["applied"] is None


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
