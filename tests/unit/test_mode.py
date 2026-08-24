"""Forward tests for the mode: does it parse, and does it default to enforcing?

The behavioural half -- what a mode actually does to a money request -- lives in
``tests/adversarial/test_observe_mode.py``, because "the checkpoint deliberately lets a
denied request through" is a claim that needs hostile tests around it, not friendly ones.
"""

from __future__ import annotations

import pytest

from paynaka.mode import MODE_ENV_VAR, Mode


class TestParsing:
    def test_nothing_configured_enforces(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A checkpoint that does not enforce unless told to is one somebody forgot to
        switch on."""
        monkeypatch.delenv(MODE_ENV_VAR, raising=False)
        assert Mode.from_env() is Mode.ENFORCE

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("enforce", Mode.ENFORCE),
            ("observe", Mode.OBSERVE),
            ("ENFORCE", Mode.ENFORCE),
            ("Observe", Mode.OBSERVE),
            ("  observe  ", Mode.OBSERVE),
            ("", Mode.ENFORCE),
            ("   ", Mode.ENFORCE),
        ],
        ids=repr,
    )
    def test_the_configured_value_is_read(self, raw: str, expected: Mode) -> None:
        assert Mode.from_env(raw) is expected

    def test_the_environment_is_read_when_no_value_is_passed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(MODE_ENV_VAR, "observe")
        assert Mode.from_env() is Mode.OBSERVE

    def test_enforcing_is_true_for_exactly_one_mode(self) -> None:
        assert Mode.ENFORCE.enforcing is True
        assert Mode.OBSERVE.enforcing is False

    def test_the_value_is_the_string_an_audit_record_carries(self) -> None:
        assert Mode.OBSERVE.value == "observe"
        assert Mode.ENFORCE.value == "enforce"
