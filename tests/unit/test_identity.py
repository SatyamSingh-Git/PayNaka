"""Forward tests for caller authentication: does it let the right caller in?

The adversarial half lives in ``tests/adversarial/test_identity_adversarial.py`` and is
much longer, which is the correct ratio for an auth check. This file establishes that a
correctly configured caller can actually get through -- without which every rejection test
below would pass on a check that rejects everything.
"""

from __future__ import annotations

import pytest

from paynaka.identity import (
    MIN_TOKEN_LENGTH,
    Caller,
    TokenRegistry,
    load_or_create_dev_token,
    parse_bearer,
)

GOOD = "a" * 40
OTHER = "b" * 40


class TestParseBearer:
    def test_extracts_the_token(self) -> None:
        assert parse_bearer(f"Bearer {GOOD}") == GOOD

    @pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER", "BeArEr"])
    def test_the_scheme_is_case_insensitive_because_http_says_so(self, scheme: str) -> None:
        assert parse_bearer(f"{scheme} {GOOD}") == GOOD

    def test_a_token_may_contain_characters_that_look_structural(self) -> None:
        """Base64url and JWT-shaped tokens carry dots, dashes and underscores."""
        token = "eyJhbGc.iOiJIUzI1NiJ9-_x" + "y" * 20
        assert parse_bearer(f"Bearer {token}") == token


class TestAuthenticate:
    def test_a_configured_caller_gets_in_and_is_named(self) -> None:
        registry = TokenRegistry({"buyer-agent": GOOD})
        assert registry.authenticate(f"Bearer {GOOD}") == Caller(name="buyer-agent")

    def test_each_caller_is_told_apart_by_its_own_token(self) -> None:
        registry = TokenRegistry({"buyer": GOOD, "console": OTHER})
        assert registry.authenticate(f"Bearer {GOOD}").name == "buyer"
        assert registry.authenticate(f"Bearer {OTHER}").name == "console"

    def test_names_are_sorted_and_carry_no_secrets(self) -> None:
        registry = TokenRegistry({"zeta": GOOD, "alpha": OTHER})
        assert registry.names == ("alpha", "zeta")
        assert len(registry) == 2


class TestFromEnv:
    def test_configured_pairs_are_parsed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PAYNAKA_AGENT_TOKENS", f"buyer:{GOOD},console:{OTHER}")
        registry = TokenRegistry.from_env()
        assert registry.names == ("buyer", "console")
        assert registry.authenticate(f"Bearer {GOOD}").name == "buyer"

    def test_a_token_may_contain_a_colon_only_the_first_one_separates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        token = "ey:JhbGciOiJIUzI1NiJ9:abcdefghij"
        monkeypatch.setenv("PAYNAKA_AGENT_TOKENS", f"buyer:{token}")
        assert TokenRegistry.from_env().authenticate(f"Bearer {token}").name == "buyer"

    def test_whitespace_around_entries_is_forgiven(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An operator pasting into a YAML block should not be punished for indentation."""
        monkeypatch.setenv("PAYNAKA_AGENT_TOKENS", f"  buyer : {GOOD} ")
        assert TokenRegistry.from_env().names == ("buyer",)


class TestTheDevelopmentCredential:
    def test_with_nothing_configured_and_the_simulator_a_credential_is_minted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: object
    ) -> None:
        monkeypatch.delenv("PAYNAKA_AGENT_TOKENS", raising=False)
        path = f"{tmp_path}/dev-token"
        registry = TokenRegistry.from_env(rail="sim", dev_token_path=path)
        assert registry.names == ("dev-agent",)

    def test_the_check_is_still_live_the_credential_is_merely_generated(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: object
    ) -> None:
        """The dev path removes the need to invent a credential, not to present one."""
        monkeypatch.delenv("PAYNAKA_AGENT_TOKENS", raising=False)
        path = f"{tmp_path}/dev-token"
        registry = TokenRegistry.from_env(rail="sim", dev_token_path=path)
        with pytest.raises(Exception, match="no valid bearer"):
            registry.authenticate(None)
        minted = load_or_create_dev_token(path)
        assert registry.authenticate(f"Bearer {minted}").name == "dev-agent"

    def test_the_credential_persists_so_a_restart_does_not_break_the_demo(
        self, tmp_path: object
    ) -> None:
        path = f"{tmp_path}/dev-token"
        first = load_or_create_dev_token(path)
        assert load_or_create_dev_token(path) == first
        assert len(first) >= MIN_TOKEN_LENGTH
