"""Who is allowed to create the constraint?

An independent audit asked that question and found the wrong answer. `/api/intent`
authenticated against the **agent** registry, so the buying agent's own credential could
ask this service to sign a mandate of the agent's own design, redeem the resulting grant,
and spend inside a bound it had written itself. Nothing was forged. A genuine signature was
requested over an invented constraint, and the checkpoint downstream verified it perfectly,
because it was genuine.

The route's docstring had said "this is the shopper's surface, not the agent's" the whole
time. It was true of the intent and false of the code, which is the most expensive kind of
comment.

Two things close it, and both are tested here. Issuance authenticates against a third
credential set that no agent token opens. And the subject is taken from the credential
rather than the request body, so a shopper creates authority over their own account and
nobody else's.

The tests are mostly negative on purpose. A route that issues correctly for the right
caller proves very little; the claim is about who is turned away.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from merchant.app import reset_catalog
from paynaka.app import app, hub
from paynaka.identity import TokenRegistry

pytestmark = pytest.mark.integration

AGENT_TOKEN = "issuance-agent-token-long-enough-x"
APPROVER_TOKEN = "issuance-approver-token-long-enough"
SHOPPER_TOKEN = "issuance-shopper-token-long-enough"
OTHER_SHOPPER_TOKEN = "issuance-other-shopper-token-long"

SUBJECT = "cust_kirana_001"
OTHER_SUBJECT = "cust_someone_else"

AS_AGENT = {"Authorization": f"Bearer {AGENT_TOKEN}"}
AS_APPROVER = {"Authorization": f"Bearer {APPROVER_TOKEN}"}
AS_SHOPPER = {"Authorization": f"Bearer {SHOPPER_TOKEN}"}
AS_OTHER_SHOPPER = {"Authorization": f"Bearer {OTHER_SHOPPER_TOKEN}"}

INTENT = {
    "session_id": "sess_authority",
    "budget_paise": 199_900,
    "skus": ["ATTA-5KG"],
    "destinations": ["addr_home"],
}


@pytest.fixture
def client() -> Iterator[TestClient]:
    reset_catalog()
    with TestClient(app) as test_client:
        # After startup: `hub.open()` rebuilds every registry from the environment.
        hub.callers = TokenRegistry({"buyer-agent": AGENT_TOKEN})
        hub.approvers = TokenRegistry({"ops-anita": APPROVER_TOKEN})
        hub.shoppers = TokenRegistry({SUBJECT: SHOPPER_TOKEN, OTHER_SUBJECT: OTHER_SHOPPER_TOKEN})
        yield test_client
    reset_catalog()


class TestTheAgentCannotCreateItsOwnAuthority:
    """The finding, closed. Each of these was a 200 before."""

    def test_an_agent_credential_cannot_issue_a_mandate(self, client: TestClient) -> None:
        response = client.post("/api/intent", headers=AS_AGENT, json={**INTENT})
        assert response.status_code == 401, response.text
        assert "mandate" not in response.text.lower()

    def test_an_agent_cannot_issue_a_wider_mandate_than_it_was_given(
        self, client: TestClient
    ) -> None:
        """The attack in its natural form: not forging authority, but asking for more of
        it. A ₹5,00,000 budget over every SKU is refused at the door rather than signed."""
        response = client.post(
            "/api/intent",
            headers=AS_AGENT,
            json={**INTENT, "budget_paise": 50_000_000, "skus": ["ATTA-5KG", "GIFT-50K"]},
        )
        assert response.status_code == 401
        assert "signed_mandate" not in response.text

    def test_an_approver_credential_cannot_issue_either(self, client: TestClient) -> None:
        """Approving a step-up and creating authority are different powers. A credential
        holding both could widen a mandate and then wave the widening through."""
        assert client.post("/api/intent", headers=AS_APPROVER, json={**INTENT}).status_code == 401

    def test_no_credential_at_all_cannot_issue(self, client: TestClient) -> None:
        assert client.post("/api/intent", json={**INTENT}).status_code == 401

    @pytest.mark.parametrize(
        "header",
        [
            {"Authorization": f"Basic {SHOPPER_TOKEN}"},
            {"Authorization": SHOPPER_TOKEN},
            {"Authorization": f"Bearer {SHOPPER_TOKEN} "},
            {"Authorization": f"Bearer  {SHOPPER_TOKEN}"},
            {"Authorization": "Bearer "},
        ],
        ids=["wrong-scheme", "no-scheme", "trailing-space", "double-space", "empty"],
    )
    def test_a_malformed_credential_is_not_a_credential(
        self, client: TestClient, header: dict[str, str]
    ) -> None:
        assert client.post("/api/intent", headers=header, json={**INTENT}).status_code == 401

    def test_the_refusal_does_not_confirm_the_token_is_real_elsewhere(
        self, client: TestClient
    ) -> None:
        """401 rather than 403, deliberately. A 403 for a valid agent token would tell a
        prober the token is genuine and merely wrong for this route, which is half the
        answer. A real agent token and an invented one get the same refusal."""
        real = client.post("/api/intent", headers=AS_AGENT, json={**INTENT})
        invented = client.post(
            "/api/intent",
            headers={"Authorization": "Bearer not-a-real-token-but-long-enough"},
            json={**INTENT},
        )
        assert real.status_code == invented.status_code == 401
        assert real.json()["detail"] == invented.json()["detail"]


class TestAuthorityIsCreatedOnlyForOneself:
    def test_the_subject_comes_from_the_credential(self, client: TestClient) -> None:
        body = client.post("/api/intent", headers=AS_SHOPPER, json={**INTENT}).json()
        assert body["issued_to"] == SUBJECT

    def test_a_shopper_cannot_issue_for_another_subject(self, client: TestClient) -> None:
        """Refused rather than silently rewritten. A caller who believes they set a field
        and did not is a caller who will not read the response."""
        response = client.post(
            "/api/intent", headers=AS_SHOPPER, json={**INTENT, "subject": OTHER_SUBJECT}
        )
        assert response.status_code == 403, response.text
        assert OTHER_SUBJECT in response.json()["detail"]

    def test_each_shopper_gets_their_own_subject(self, client: TestClient) -> None:
        """Two credentials, two subjects, same body. The body does not decide."""
        first = client.post("/api/intent", headers=AS_SHOPPER, json={**INTENT}).json()
        second = client.post(
            "/api/intent", headers=AS_OTHER_SHOPPER, json={**INTENT, "session_id": "sess_two"}
        ).json()
        assert first["issued_to"] == SUBJECT
        assert second["issued_to"] == OTHER_SUBJECT

    def test_an_omitted_subject_is_filled_in_rather_than_refused(self, client: TestClient) -> None:
        """The body need not name a subject at all, because it never decided one."""
        response = client.post("/api/intent", headers=AS_SHOPPER, json={**INTENT})
        assert response.status_code == 200
        assert response.json()["issued_to"] == SUBJECT

    def test_a_matching_subject_is_accepted(self, client: TestClient) -> None:
        """Naming your own subject is redundant, not wrong. Refusing it would break every
        client that fills the field in for readability."""
        response = client.post(
            "/api/intent", headers=AS_SHOPPER, json={**INTENT, "subject": SUBJECT}
        )
        assert response.status_code == 200


class TestTheThreeRolesStaySeparate:
    """Enforced at startup rather than at request time, so a dangerous configuration is a
    service that does not boot rather than a privilege overlap nobody notices."""

    def test_a_shared_name_is_also_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from paynaka.identity import load_shoppers

        monkeypatch.setenv("PAYNAKA_SHOPPER_TOKENS", f"buyer-agent:{OTHER_SHOPPER_TOKEN}")
        agents = TokenRegistry({"buyer-agent": AGENT_TOKEN})
        with pytest.raises(ValueError, match="both roles"):
            load_shoppers(agents, TokenRegistry({}))

    def test_a_shared_token_is_refused_even_under_different_names(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The dangerous configuration is not two entries with the same label. It is one
        secret that opens two doors, which careful naming does not prevent."""
        from paynaka.identity import load_shoppers

        monkeypatch.setenv("PAYNAKA_SHOPPER_TOKENS", f"{SUBJECT}:{AGENT_TOKEN}")
        agents = TokenRegistry({"buyer-agent": AGENT_TOKEN})
        with pytest.raises(ValueError, match="both roles"):
            load_shoppers(agents, TokenRegistry({}))

    def test_a_shopper_token_shared_with_an_approver_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from paynaka.identity import load_shoppers

        monkeypatch.setenv("PAYNAKA_SHOPPER_TOKENS", f"{SUBJECT}:{APPROVER_TOKEN}")
        with pytest.raises(ValueError, match="both roles"):
            load_shoppers(TokenRegistry({}), TokenRegistry({"ops-anita": APPROVER_TOKEN}))

    def test_a_real_rail_with_nothing_configured_issues_to_nobody(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail closed. An unconfigured production deployment must issue no mandates at
        all rather than mint a development credential and issue them to whoever asks."""
        from paynaka.identity import load_shoppers

        monkeypatch.delenv("PAYNAKA_SHOPPER_TOKENS", raising=False)
        monkeypatch.setenv("PAYNAKA_RAIL", "test")
        shoppers = load_shoppers(TokenRegistry({}), TokenRegistry({}))
        assert len(shoppers) == 0
        with pytest.raises(Exception, match="no valid bearer credential"):
            shoppers.authenticate(f"Bearer {SHOPPER_TOKEN}")


class TestTheBodyIsParsedStrictly:
    """Authority is created here, and JSON has more shapes than these fields do.

    `bool("false")` is `True`. A body carrying the *string* "false" for `allow_refunds`
    therefore granted refund authority -- a quoting mistake in a client, silently upgraded
    into permission to move money out of an account. An audit reproduced it.

    Nothing is coerced now. Every field is the type it claims or the request is a 400.
    """

    @pytest.mark.parametrize(
        "value",
        ["false", "true", "0", "1", 0, 1, [], None],
        ids=["str-false", "str-true", "str-0", "str-1", "int-0", "int-1", "list", "null"],
    )
    def test_only_a_real_boolean_can_grant_refunds(self, client: TestClient, value: object) -> None:
        response = client.post(
            "/api/intent", headers=AS_SHOPPER, json={**INTENT, "allow_refunds": value}
        )
        assert response.status_code == 400, f"{value!r} was accepted as a boolean"
        assert "true or false" in response.json()["detail"]

    def test_a_real_boolean_still_works(self, client: TestClient) -> None:
        """The other direction. A parser that refuses everything refuses the honest caller
        too, and this field has to remain usable."""
        response = client.post(
            "/api/intent", headers=AS_SHOPPER, json={**INTENT, "allow_refunds": True}
        )
        assert response.status_code == 200
        assert "create_refund" in response.json()["signed"]["mandate"]["allowed_actions"]

    def test_refunds_stay_off_by_default(self, client: TestClient) -> None:
        body = client.post("/api/intent", headers=AS_SHOPPER, json={**INTENT}).json()
        assert "create_refund" not in body["signed"]["mandate"]["allowed_actions"]

    @pytest.mark.parametrize("field", ["budget_paise", "max_qty_per_sku", "ttl_seconds"])
    @pytest.mark.parametrize(
        "value", ["199900", 1999.0, True, None, []], ids=["str", "float", "bool", "null", "list"]
    )
    def test_numbers_are_not_coerced(self, client: TestClient, field: str, value: object) -> None:
        """`int("199900")` works and `int(1999.9)` truncates. Both are a value that looked
        close enough and was not the thing, on a path that decides how much may be spent."""
        response = client.post("/api/intent", headers=AS_SHOPPER, json={**INTENT, field: value})
        assert response.status_code == 400, f"{field}={value!r} was accepted"

    def test_a_bare_string_is_not_a_list_of_skus(self, client: TestClient) -> None:
        """The one worth naming: `tuple("ATTA-5KG")` is twenty-one single-character SKUs,
        none of which exist, and the mandate would have been issued over them."""
        response = client.post(
            "/api/intent", headers=AS_SHOPPER, json={**INTENT, "skus": "ATTA-5KG"}
        )
        assert response.status_code == 400
        assert "list of strings" in response.json()["detail"]

    def test_a_list_with_a_non_string_in_it_is_refused(self, client: TestClient) -> None:
        response = client.post(
            "/api/intent", headers=AS_SHOPPER, json={**INTENT, "skus": ["ATTA-5KG", 5]}
        )
        assert response.status_code == 400


class TestReferencePricesReachTheMandate:
    """The bound that stops the attack this project leads with -- a merchant repricing
    between the agent reading a page and paying for it -- existed on `ShopperIntent` and
    was never read here. The check was live and unreachable, which is the same as absent
    for anybody using the API.
    """

    def test_a_reference_price_is_carried_into_the_signed_mandate(self, client: TestClient) -> None:
        response = client.post(
            "/api/intent",
            headers=AS_SHOPPER,
            json={**INTENT, "reference_prices": {"ATTA-5KG": 199_900}},
        )
        assert response.status_code == 200, response.text
        mandate = response.json()["signed"]["mandate"]
        assert mandate["reference_prices"], "the price the shopper was shown was dropped"

    def test_a_tolerance_is_carried_too(self, client: TestClient) -> None:
        response = client.post(
            "/api/intent",
            headers=AS_SHOPPER,
            json={
                **INTENT,
                "reference_prices": {"ATTA-5KG": 199_900},
                "price_tolerance_bps": 50,
            },
        )
        assert response.json()["signed"]["mandate"]["price_tolerance_bps"] == 50

    @pytest.mark.parametrize(
        "prices",
        [{"ATTA-5KG": "199900"}, {"ATTA-5KG": 1999.0}, {"ATTA-5KG": True}, ["ATTA-5KG"], "x"],
        ids=["str-paise", "float-paise", "bool-paise", "list", "string"],
    )
    def test_a_malformed_reference_price_is_refused(
        self, client: TestClient, prices: object
    ) -> None:
        """Money crosses every boundary as int paise. A rupee string here would bound the
        price at a hundredth of what the shopper saw, or a hundred times it."""
        response = client.post(
            "/api/intent", headers=AS_SHOPPER, json={**INTENT, "reference_prices": prices}
        )
        assert response.status_code == 400

    def test_omitting_them_is_still_allowed(self, client: TestClient) -> None:
        """Not every shopper was shown a per-item price. The budget still bounds the
        basket, and requiring this would break the simple case."""
        assert client.post("/api/intent", headers=AS_SHOPPER, json={**INTENT}).status_code == 200
