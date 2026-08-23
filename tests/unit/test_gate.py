"""Forward tests for paynaka.gate -- the ordinary shopping day.

The adversarial suite proves the gate refuses what it should. This one proves it permits
what it should, which is the half that keeps it from being an outage dressed as security.
"""

from __future__ import annotations

import dataclasses

import pytest

from paynaka.gate import (
    GateDecision,
    LineItem,
    MoneyRequest,
    Verdict,
    check_items_subset,
    check_total,
    evaluate,
    request_hash,
)
from paynaka.mandate import IntentMandate
from tests.conftest import ATTA, AUTHORISED, HOME, order


class TestHappyPath:
    def test_the_purchase_the_shopper_asked_for_is_allowed(
        self, legit_order, mandate, state, policy, clock
    ) -> None:
        result = evaluate(legit_order, mandate, state=state, policy=policy, clock=clock)
        assert result.verdict is Verdict.ALLOW
        assert result.check_id is None
        assert result.reason == "within the frozen intent and merchant policy"

    def test_allow_carries_the_amounts_for_the_ledger_view(
        self, legit_order, mandate, state, policy, clock
    ) -> None:
        result = evaluate(legit_order, mandate, state=state, policy=policy, clock=clock)
        assert result.evidence == {"amount": AUTHORISED, "authorised": AUTHORISED}

    def test_open_ended_budget_permits_any_sku(self, clock, state, policy) -> None:
        """'Something under two thousand rupees' -- no SKU named, so any SKU qualifies."""
        open_ended = IntentMandate.create(
            clock=clock, subject="c", session_id="s", max_total=AUTHORISED
        )
        result = evaluate(
            order(sku="DAL-1KG", unit=50_000), open_ended, state=state, policy=policy, clock=clock
        )
        assert result.allowed

    def test_multi_line_cart_within_budget_is_allowed(self, mandate, state, policy, clock) -> None:
        request = MoneyRequest(
            action="create_order",
            request_id="r",
            idempotency_key="k",
            items=(LineItem(ATTA, 2, 50_000), LineItem(ATTA, 1, 40_000)),
            destination=HOME,
        )
        assert evaluate(request, mandate, state=state, policy=policy, clock=clock).allowed


class TestEffectiveAmount:
    def test_line_items_are_summed(self) -> None:
        request = MoneyRequest(
            action="create_order",
            request_id="r",
            items=(LineItem(ATTA, 2, 50_000), LineItem("DAL", 1, 30_000)),
        )
        assert request.effective_amount == 130_000

    def test_explicit_amount_wins_when_there_are_no_items(self) -> None:
        request = MoneyRequest(action="create_refund", request_id="r", amount=12_345)
        assert request.effective_amount == 12_345

    def test_no_items_and_no_amount_is_zero(self) -> None:
        assert MoneyRequest(action="create_order", request_id="r").effective_amount == 0

    def test_line_item_total_multiplies(self) -> None:
        assert LineItem(ATTA, 3, 50_000).total == 150_000


class TestChecksInIsolation:
    """Each check is a pure function, so each can be exercised on its own."""

    def test_items_subset_passes_for_an_authorised_sku(self, mandate) -> None:
        assert check_items_subset(order(), mandate) is None

    def test_items_subset_returns_a_decision_for_an_unauthorised_sku(self, mandate) -> None:
        result = check_items_subset(order(sku="GIFT-50K"), mandate)
        assert isinstance(result, GateDecision)
        assert result.verdict is Verdict.DENY

    def test_total_passes_at_the_boundary(self, mandate, policy) -> None:
        assert check_total(order(unit=AUTHORISED), mandate, policy) is None

    def test_checks_do_not_mutate_their_inputs(self, mandate, policy) -> None:
        request = order()
        before = dataclasses.asdict(request)
        check_total(request, mandate, policy)
        check_items_subset(request, mandate)
        assert dataclasses.asdict(request) == before


class TestRequestHash:
    def test_is_stable(self) -> None:
        assert request_hash(order()) == request_hash(order())

    @pytest.mark.parametrize(
        "change",
        [{"currency": "USD"}, {"destination": "addr_other"}, {"payment_id": "pay_1"}],
    )
    def test_changing_a_money_relevant_field_changes_the_hash(
        self, change: dict[str, object]
    ) -> None:
        assert request_hash(order()) != request_hash(dataclasses.replace(order(), **change))

    def test_amount_change_changes_the_hash(self) -> None:
        assert request_hash(order()) != request_hash(order(unit=AUTHORISED + 1))

    def test_hash_is_hex_sha256(self) -> None:
        digest = request_hash(order())
        assert len(digest) == 64
        int(digest, 16)


class TestVerdict:
    def test_allowed_property_matches_the_verdict(self) -> None:
        assert GateDecision(Verdict.ALLOW, "a", "r").allowed is True
        assert GateDecision(Verdict.DENY, "a", "r").allowed is False
        assert GateDecision(Verdict.STEP_UP, "a", "r").allowed is False

    def test_verdict_serialises_as_a_plain_string(self) -> None:
        assert GateDecision(Verdict.DENY, "a", "r").to_dict()["verdict"] == "DENY"
