"""Forward tests for paynaka.policy, plus the load-time strictness that protects it.

A policy file is the merchant's own caution expressed as configuration. The single most
valuable property is that a typo fails at startup rather than silently removing a limit.
"""

from __future__ import annotations

import pytest

from paynaka.policy import ActionPolicy, Policy, PolicyError

MINIMAL = {"version": 1, "merchant": "kirana-co"}


class TestShippedPolicy:
    """The policy.yaml that actually ships must load and mean what it says."""

    def test_loads(self, policy: Policy) -> None:
        assert policy.merchant == "kirana-co"
        assert policy.require_idempotency_key is True

    def test_refunds_are_enabled_but_fenced(self, policy: Policy) -> None:
        """The action Razorpay switched off, re-enabled behind real constraints."""
        refund = policy.for_action("create_refund")
        assert refund.enabled is True
        assert refund.require_return_event is True
        assert refund.max_amount == 500_000
        assert refund.step_up_above == 100_000
        assert refund.daily_cap == 2_000_000

    def test_payouts_are_off(self, policy: Policy) -> None:
        assert policy.for_action("create_payout").enabled is False

    def test_regulatory_defaults_are_the_real_statutory_values(self, policy: Policy) -> None:
        reg = policy.regulatory
        assert reg.npci_mandate_retries == 3
        assert reg.afa_threshold == 1_500_000
        assert reg.pre_debit_notice_seconds == 86_400
        assert str(reg.contact_window) == "08:00-19:00 IST"
        assert [str(w) for w in reg.debit_blackout] == ["10:00-13:00 IST"]


class TestUnconfiguredIsDisabled:
    def test_action_with_no_entry_is_disabled_not_unrestricted(self) -> None:
        """The fail-closed default. A forgotten action must not become an open door."""
        policy = Policy.from_dict(MINIMAL)
        assert policy.for_action("create_payout").enabled is False
        assert policy.for_action("create_order").enabled is False

    def test_unknown_action_name_is_disabled(self, policy: Policy) -> None:
        assert policy.for_action("transfer_everything").enabled is False


class TestStrictLoading:
    @pytest.mark.parametrize(
        "bad",
        [
            {"version": 1, "merchant": "m", "max_amont": 500},  # typo at top level
            {"version": 1, "merchant": "m", "defaults": {"currncy": "INR"}},
            {"version": 1, "merchant": "m", "actions": {"create_order": {"max_amont": 5}}},
            {"version": 1, "merchant": "m", "regulatory": {"npci_retries": 3}},
            {"version": 1, "merchant": "m", "escalation": {"on_timout": "DENY"}},
            {"version": 1, "merchant": "m", "kill_switch": {"revoke_all": True}},
        ],
    )
    def test_a_typo_is_a_startup_failure(self, bad: dict[str, object]) -> None:
        with pytest.raises(PolicyError, match=r"unknown key|unknown action"):
            Policy.from_dict(bad)

    def test_unknown_action_in_policy_is_refused(self) -> None:
        with pytest.raises(PolicyError, match="unknown action"):
            Policy.from_dict(MINIMAL | {"actions": {"drain_account": {}}})

    @pytest.mark.parametrize("raw", [None, [], "policy", 42])
    def test_non_mapping_policy_refused(self, raw: object) -> None:
        with pytest.raises(PolicyError, match="must be a mapping"):
            Policy.from_dict(raw)

    def test_invalid_yaml_is_refused(self) -> None:
        with pytest.raises(PolicyError, match="not valid YAML"):
            Policy.from_text("actions: [unclosed")

    def test_missing_merchant_refused(self) -> None:
        with pytest.raises(PolicyError, match="merchant"):
            Policy.from_dict({"version": 1})

    def test_version_bump_refused(self) -> None:
        with pytest.raises(PolicyError, match="version"):
            Policy.from_dict({"version": 2, "merchant": "m"})


class TestFailClosedByConstruction:
    def test_failing_open_on_step_up_timeout_is_not_offered(self) -> None:
        """Not a knob. Someone would turn it the wrong way at 3am during an incident."""
        with pytest.raises(PolicyError, match="failing open is not offered"):
            Policy.from_dict(MINIMAL | {"escalation": {"on_timeout": "ALLOW"}})

    def test_non_inr_currency_refused(self) -> None:
        with pytest.raises(PolicyError, match="currency"):
            Policy.from_dict(MINIMAL | {"defaults": {"currency": "USD"}})

    def test_reinterpreting_the_rules_in_another_timezone_is_refused(self) -> None:
        """The encoded rules are Indian; reading them in UTC would be a compliance error."""
        with pytest.raises(PolicyError, match="Asia/Kolkata"):
            Policy.from_dict(MINIMAL | {"regulatory": {"timezone": "UTC"}})


class TestAmountValidation:
    @pytest.mark.parametrize("value", [0, -1, -500_000])
    def test_non_positive_limits_refused(self, value: int) -> None:
        with pytest.raises(PolicyError, match="positive"):
            ActionPolicy(max_amount=value)

    @pytest.mark.parametrize("value", [1.5, "500000", True])
    def test_non_int_limits_refused(self, value: object) -> None:
        with pytest.raises(PolicyError, match="int paise"):
            ActionPolicy(max_amount=value)  # type: ignore[arg-type]

    def test_unreachable_step_up_band_is_refused(self) -> None:
        """step_up_above beyond max_amount means the band can never fire. Almost surely a typo."""
        with pytest.raises(PolicyError, match="unreachable"):
            ActionPolicy(max_amount=100, step_up_above=200)

    def test_step_up_equal_to_max_is_allowed(self) -> None:
        assert ActionPolicy(max_amount=100, step_up_above=100).step_up_above == 100

    @pytest.mark.parametrize("retries", [-1, 11, 1000])
    def test_absurd_retry_counts_refused(self, retries: int) -> None:
        with pytest.raises(PolicyError, match="out of range"):
            Policy.from_dict(MINIMAL | {"regulatory": {"npci_mandate_retries": retries}})

    def test_malformed_window_is_a_startup_failure(self) -> None:
        with pytest.raises(PolicyError, match="malformed time window"):
            Policy.from_dict(MINIMAL | {"regulatory": {"contact_window": "8am-7pm"}})


class TestRoundTrip:
    def test_yaml_and_dict_paths_agree(self, policy: Policy) -> None:
        from pathlib import Path

        assert Policy.from_text(Path("policy.yaml").read_text(encoding="utf-8")) == policy
