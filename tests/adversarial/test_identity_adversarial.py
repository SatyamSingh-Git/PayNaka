"""Adversarial tests for caller authentication.

An auth check is one of the few places where the hostile cases *are* the specification, so
this file is much longer than its forward counterpart. Four groups:

* **Presentation** -- every way a credential can be malformed, absent, or nearly right.
  Table-driven and probing the boundary, because "nearly right" is what a prober sends.
* **Configuration** -- weak, duplicated and malformed configuration, all of which must be a
  startup failure rather than a silently degraded runtime.
* **Fail closed** -- with nothing configured and a real payment rail, refuse to build.
* **Leakage** -- no error message, repr or public accessor may carry a token.
"""

from __future__ import annotations

import pytest

from paynaka.identity import (
    MIN_TOKEN_LENGTH,
    TokenRegistry,
    Unauthenticated,
    load_or_create_dev_token,
)

GOOD = "s3cret-token-value-that-is-long-enough"
OTHER = "another-token-value-that-is-long-enough"


@pytest.fixture
def registry() -> TokenRegistry:
    return TokenRegistry({"buyer-agent": GOOD})


# ============================================================== presentation
class TestNothingUsableIsPresented:
    @pytest.mark.parametrize(
        "header",
        [
            None,
            "",
            " ",
            "Bearer",  # scheme with no separator at all
            "Bearer ",  # separator, empty token
            "Bearer  ",  # separator, whitespace token
            "bearer\t" + GOOD,  # tab is not the separator the RFC names
            GOOD,  # bare token, no scheme
            f"Basic {GOOD}",
            f"Token {GOOD}",
            f"MAC {GOOD}",
            f"Bearer2 {GOOD}",
            f"NotBearer {GOOD}",
        ],
        ids=repr,
    )
    def test_every_malformed_presentation_is_refused(
        self, registry: TokenRegistry, header: str | None
    ) -> None:
        with pytest.raises(Unauthenticated):
            registry.authenticate(header)

    @pytest.mark.parametrize(
        "header",
        [f"Bearer  {GOOD}", f"Bearer {GOOD} ", f"Bearer {GOOD}\n", f"Bearer {GOOD}\t"],
        ids=repr,
    )
    def test_padding_around_the_token_is_not_quietly_stripped(
        self, registry: TokenRegistry, header: str
    ) -> None:
        """A copy-paste that picked up a newline must fail loudly and consistently.

        Stripping would make the same credential work in one client and not another
        depending on how the header was assembled, which is the worst of both.
        """
        with pytest.raises(Unauthenticated):
            registry.authenticate(header)

    def test_a_second_token_after_the_first_is_not_accepted(self, registry: TokenRegistry) -> None:
        with pytest.raises(Unauthenticated):
            registry.authenticate(f"Bearer {GOOD} {GOOD}")


class TestNearlyRight:
    @pytest.mark.parametrize("length", range(1, len(GOOD)))
    def test_no_prefix_of_a_valid_token_is_accepted(
        self, registry: TokenRegistry, length: int
    ) -> None:
        """Every prefix, not a sample of them. This is the shape of an incremental guess."""
        with pytest.raises(Unauthenticated):
            registry.authenticate(f"Bearer {GOOD[:length]}")

    @pytest.mark.parametrize(
        "mutant",
        [
            GOOD + "x",  # one character appended
            "x" + GOOD,  # one character prepended
            GOOD[:-1] + "x",  # last character changed
            "x" + GOOD[1:],  # first character changed
            GOOD.upper(),  # case-shifted: tokens are exact bytes
            GOOD.replace("-", "_"),  # a plausible transcription error
            GOOD.replace("s3cret", "secret"),  # a plausible typo
            GOOD[::-1],  # reversed
        ],
        ids=repr,
    )
    def test_a_token_off_by_anything_is_refused(self, registry: TokenRegistry, mutant: str) -> None:
        with pytest.raises(Unauthenticated):
            registry.authenticate(f"Bearer {mutant}")

    @pytest.mark.parametrize(
        "payload",
        [
            "s3cret-token-vаlue-that-is-long-enough",  # Cyrillic а for Latin a
            "s3cret-tоken-value-that-is-long-enough",  # Cyrillic о for Latin o
            "s3cret-token-value-that-is-long-enough​",  # zero-width space
            "s3cret-token-value​that-is-long-enough",  # zero-width in the middle
            "﻿s3cret-token-value-that-is-long-enough",  # BOM
        ],
        ids=["cyrillic-a", "cyrillic-o", "zwsp-tail", "zwsp-middle", "bom"],
    )
    def test_homoglyphs_and_invisible_characters_do_not_match(
        self, registry: TokenRegistry, payload: str
    ) -> None:
        """A token that *renders* identically is still a different credential."""
        with pytest.raises(Unauthenticated):
            registry.authenticate(f"Bearer {payload}")

    @pytest.mark.parametrize(
        "payload",
        ["\ud800", "tok\ud800en-that-is-long-enough-here", "\udfff" * 30],
        ids=["lone-surrogate", "surrogate-inside", "surrogate-run"],
    )
    def test_a_lone_surrogate_is_a_refusal_not_an_encoding_error(
        self, registry: TokenRegistry, payload: str
    ) -> None:
        """Regression. Comparing the header as text made non-ASCII an unhandled TypeError
        on the auth path -- a 500 where a 401 belongs, reachable before any credential is
        known. Comparison is over bytes now, and encoding cannot raise."""
        with pytest.raises(Unauthenticated):
            registry.authenticate(f"Bearer {payload}")

    def test_a_token_belonging_to_nobody_is_refused_even_when_well_formed(self) -> None:
        registry = TokenRegistry({"buyer": GOOD, "console": OTHER})
        with pytest.raises(Unauthenticated):
            registry.authenticate("Bearer c" * 40)

    def test_an_oversize_token_is_refused_without_blowing_up(self, registry: TokenRegistry) -> None:
        with pytest.raises(Unauthenticated):
            registry.authenticate("Bearer " + "x" * 1_000_000)

    def test_an_empty_registry_admits_nobody(self) -> None:
        """Not a configuration anything should reach, and it must still fail closed."""
        empty = TokenRegistry({})
        for header in (None, "", f"Bearer {GOOD}"):
            with pytest.raises(Unauthenticated):
                empty.authenticate(header)


# ============================================================== configuration
class TestWeakConfigurationIsAStartupFailure:
    @pytest.mark.parametrize("length", [0, 1, 8, MIN_TOKEN_LENGTH - 2, MIN_TOKEN_LENGTH - 1])
    def test_a_token_below_the_floor_is_refused(self, length: int) -> None:
        with pytest.raises(ValueError, match="is the minimum"):
            TokenRegistry({"buyer": "x" * length})

    @pytest.mark.parametrize("length", [MIN_TOKEN_LENGTH, MIN_TOKEN_LENGTH + 1, 200])
    def test_a_token_at_or_above_the_floor_is_accepted(self, length: int) -> None:
        """The other side of the boundary, so the floor is a floor and not a wall."""
        assert TokenRegistry({"buyer": "x" * length}).names == ("buyer",)

    def test_two_callers_sharing_a_token_is_refused(self) -> None:
        """An audit record could not say which of them acted."""
        with pytest.raises(ValueError, match="share one token"):
            TokenRegistry({"buyer": GOOD, "console": GOOD})

    @pytest.mark.parametrize("name", ["", " ", " buyer", "buyer ", "\tbuyer"], ids=repr)
    def test_a_padded_or_empty_caller_name_is_refused(self, name: str) -> None:
        with pytest.raises(ValueError, match="non-empty and unpadded"):
            TokenRegistry({name: GOOD})


class TestMalformedEnvironmentIsAStartupFailure:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (f"{GOOD}", "is not 'name:token'"),  # bare token, no caller name
            (f"buyer:{GOOD},", "empty entry"),  # trailing comma
            (f",buyer:{GOOD}", "empty entry"),  # leading comma
            (f"buyer:{GOOD},,console:{OTHER}", "empty entry"),  # doubled comma
            (f"buyer:{GOOD},buyer:{OTHER}", "twice"),  # duplicate caller name
            (f"buyer:{GOOD},console:{GOOD}", "share one token"),  # duplicate token
            ("buyer:short", "is the minimum"),  # weak token
            ("buyer:", "is the minimum"),  # name with no token
        ],
        ids=[
            "bare-token",
            "trailing-comma",
            "leading-comma",
            "doubled-comma",
            "duplicate-name",
            "duplicate-token",
            "weak-token",
            "empty-token",
        ],
    )
    def test_nothing_malformed_is_silently_dropped(
        self, monkeypatch: pytest.MonkeyPatch, raw: str, expected: str
    ) -> None:
        """A dropped entry is a caller who cannot authenticate for an unreported reason."""
        monkeypatch.setenv("PAYNAKA_AGENT_TOKENS", raw)
        with pytest.raises(ValueError, match=expected):
            TokenRegistry.from_env()


# ============================================================== fail closed
class TestARealRailRequiresARealCredential:
    @pytest.mark.parametrize("rail", ["test", "TEST", " test ", "live", "nonsense"])
    def test_the_generated_credential_is_refused_in_front_of_anything_but_the_simulator(
        self, monkeypatch: pytest.MonkeyPatch, rail: str
    ) -> None:
        monkeypatch.delenv("PAYNAKA_AGENT_TOKENS", raising=False)
        with pytest.raises(ValueError, match="reaches a real"):
            TokenRegistry.from_env(rail=rail)

    def test_an_empty_or_whitespace_env_var_counts_as_unset_not_as_no_callers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``PAYNAKA_AGENT_TOKENS=""`` must not build a registry that admits nobody and
        then look like a working deployment until the first call fails."""
        monkeypatch.setenv("PAYNAKA_AGENT_TOKENS", "   ")
        with pytest.raises(ValueError, match="reaches a real"):
            TokenRegistry.from_env(rail="test")


class TestTheDevelopmentCredentialFileIsNotTrusted:
    @pytest.mark.parametrize("existing", ["", "short", "x" * (MIN_TOKEN_LENGTH - 1), "\n\n"])
    def test_a_truncated_or_hand_edited_file_is_replaced_not_honoured(
        self, tmp_path: object, existing: str
    ) -> None:
        """Accepting a six-character token because it was on disk would reopen the exact
        weak-secret path the length floor exists to close."""
        path = f"{tmp_path}/dev-token"
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(existing)
        token = load_or_create_dev_token(path)
        assert len(token) >= MIN_TOKEN_LENGTH
        assert token != existing

    def test_two_generations_do_not_collide(self, tmp_path: object) -> None:
        first = load_or_create_dev_token(f"{tmp_path}/a")
        second = load_or_create_dev_token(f"{tmp_path}/b")
        assert first != second


# ============================================================== leakage
class TestNothingLeaksTheToken:
    def test_the_rejection_message_does_not_name_the_expected_credential(
        self, registry: TokenRegistry
    ) -> None:
        try:
            registry.authenticate("Bearer wrong-but-long-enough-to-be-plausible")
        except Unauthenticated as exc:
            assert GOOD not in str(exc)
        else:  # pragma: no cover - the call above must raise
            pytest.fail("expected Unauthenticated")

    def test_every_failure_mode_gives_the_same_message(self, registry: TokenRegistry) -> None:
        """Distinguishing 'no header' from 'wrong token' tells a prober which half of the
        guess was right."""
        messages = set()
        for header in (None, "", f"Basic {GOOD}", "Bearer " + "x" * 40, "Bearer short"):
            try:
                registry.authenticate(header)
            except Unauthenticated as exc:
                messages.add(str(exc))
        assert len(messages) == 1

    def test_the_public_accessors_expose_names_and_never_tokens(
        self, registry: TokenRegistry
    ) -> None:
        assert GOOD not in repr(registry.names)
        assert GOOD not in "".join(registry.names)

    def test_the_weak_token_error_reports_a_length_not_the_value(self) -> None:
        """The operator needs to know it was too short, not to see it echoed into a log."""
        try:
            TokenRegistry({"buyer": "abc"})
        except ValueError as exc:
            assert "abc" not in str(exc)
            assert "3 characters" in str(exc)
        else:  # pragma: no cover - the call above must raise
            pytest.fail("expected ValueError")
