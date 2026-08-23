"""The merchant's envelope, as a declarative file rather than a prompt.

A policy narrows what a mandate already permits. It can never widen it: the mandate is
the shopper's authority and the policy is the merchant's own additional caution, so the
effective permission is always the intersection.

Every unknown key is a hard error at load time. A policy file with ``max_amont: 500000``
must fail loudly on startup rather than silently fall back to a default and let an
unbounded amount through six weeks later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import yaml

from paynaka.clock import TimeWindow, parse_window
from paynaka.money import MAX_PAISE

__all__ = [
    "ActionPolicy",
    "CircuitBreaker",
    "Policy",
    "PolicyError",
    "RegulatoryPolicy",
]

POLICY_VERSION: Final[int] = 1

#: Actions a policy may configure. Mirrors mandate.KNOWN_ACTIONS.
POLICY_ACTIONS: Final[frozenset[str]] = frozenset(
    {"create_order", "create_payment_link", "capture_payment", "create_refund", "create_payout"}
)


class PolicyError(Exception):
    """The policy file is invalid. Never start a gate with one of these outstanding."""


@dataclass(frozen=True, slots=True)
class ActionPolicy:
    """Per-action limits. ``enabled=False`` beats every other field here."""

    enabled: bool = True
    max_amount: int | None = None
    step_up_above: int | None = None
    daily_cap: int | None = None
    require_return_event: bool = False

    def __post_init__(self) -> None:
        for name in ("max_amount", "step_up_above", "daily_cap"):
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                raise PolicyError(f"{name} must be int paise, got {type(value).__name__}")
            if value <= 0:
                raise PolicyError(f"{name} must be positive")
            if value > MAX_PAISE:
                raise PolicyError(f"{name} exceeds the money ceiling")

        if (
            self.step_up_above is not None
            and self.max_amount is not None
            and self.step_up_above > self.max_amount
        ):
            raise PolicyError(
                f"step_up_above ({self.step_up_above}) exceeds max_amount ({self.max_amount}); "
                "the step-up band would be unreachable, which is almost certainly a typo"
            )


@dataclass(frozen=True, slots=True)
class RegulatoryPolicy:
    """Indian payments regulation, encoded. Defaults are the real statutory values."""

    npci_mandate_retries: int = 3
    debit_blackout: tuple[TimeWindow, ...] = ()
    contact_window: TimeWindow | None = None
    afa_threshold: int | None = 1_500_000  # ₹15,000
    pre_debit_notice_seconds: int = 86_400  # RBI: 24h notice before a recurring debit

    def __post_init__(self) -> None:
        if isinstance(self.npci_mandate_retries, bool) or not isinstance(
            self.npci_mandate_retries, int
        ):
            raise PolicyError("npci_mandate_retries must be int")
        if not 0 <= self.npci_mandate_retries <= 10:
            raise PolicyError(f"npci_mandate_retries out of range: {self.npci_mandate_retries}")
        if self.afa_threshold is not None and self.afa_threshold <= 0:
            raise PolicyError("afa_threshold must be positive")
        if self.pre_debit_notice_seconds < 0:
            raise PolicyError("pre_debit_notice_seconds must not be negative")


@dataclass(frozen=True, slots=True)
class CircuitBreaker:
    """How many refusals a session gets before its authority is withdrawn.

    A gate that refuses is free for the merchant and expensive for whoever is driving the
    agent: every denial costs a model turn. An attacker who can keep an agent looping
    against a wall therefore burns the operator's budget without moving a rupee, and
    ``max_turns`` bounds one run rather than an attacker who can start many.

    This bounds it. After ``denials_per_session`` refusals in an IST day the session is
    revoked, which turns a retryable "no" into a terminal one -- and a terminal answer is
    the only kind a looping agent cannot argue with.

    It does not make the attack free to defend: the turns *before* the breaker trips are
    still spent. The claim is bounded, not prevented, and the bound is this number.
    """

    enabled: bool = True
    denials_per_session: int = 12
    denials_per_subject: int = 40

    def __post_init__(self) -> None:
        for name in ("denials_per_session", "denials_per_subject"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise PolicyError(f"{name} must be int")
            if value < 1:
                raise PolicyError(f"{name} must be at least 1")
        if self.denials_per_subject < self.denials_per_session:
            raise PolicyError(
                f"denials_per_subject ({self.denials_per_subject}) is below "
                f"denials_per_session ({self.denials_per_session}); the wider bound would "
                "trip first and the narrower one would never be reached"
            )


@dataclass(frozen=True, slots=True)
class Policy:
    """A whole policy file, parsed and validated."""

    merchant: str
    version: int = POLICY_VERSION
    require_idempotency_key: bool = True
    currency: str = "INR"
    actions: dict[str, ActionPolicy] = field(default_factory=dict)
    regulatory: RegulatoryPolicy = field(default_factory=RegulatoryPolicy)
    step_up_timeout_seconds: int = 300
    on_step_up_timeout: str = "DENY"
    kill_switch: bool = False
    circuit_breaker: CircuitBreaker = field(default_factory=CircuitBreaker)

    def __post_init__(self) -> None:
        if self.version != POLICY_VERSION:
            raise PolicyError(f"unsupported policy version: {self.version}")
        if not self.merchant:
            raise PolicyError("merchant must be set")
        if self.currency != "INR":
            raise PolicyError(f"unsupported currency: {self.currency!r}")
        if self.on_step_up_timeout != "DENY":
            # Deliberately not configurable. An unanswered approval for a money action
            # must fail closed; making that a knob invites someone to turn it the wrong
            # way at 3am during an incident.
            raise PolicyError("on_step_up_timeout must be DENY; failing open is not offered")
        if self.step_up_timeout_seconds <= 0:
            raise PolicyError("step_up_timeout_seconds must be positive")

    def for_action(self, action: str) -> ActionPolicy:
        """The policy for ``action``.

        An action with no entry is **disabled**, not unrestricted. A policy that forgot to
        mention payouts must not thereby permit unlimited payouts.
        """
        return self.actions.get(action, ActionPolicy(enabled=False))

    # ---------------------------------------------------------------- loading
    @classmethod
    def from_yaml(cls, path: str | Path) -> Policy:
        text = Path(path).read_text(encoding="utf-8")
        return cls.from_text(text)

    @classmethod
    def from_text(cls, text: str) -> Policy:
        try:
            raw = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise PolicyError(f"policy is not valid YAML: {exc}") from exc
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: object) -> Policy:
        if not isinstance(raw, dict):
            raise PolicyError(f"policy must be a mapping, got {type(raw).__name__}")

        _reject_unknown(
            raw,
            {
                "version",
                "merchant",
                "defaults",
                "actions",
                "regulatory",
                "escalation",
                "kill_switch",
                "circuit_breaker",
            },
            "policy",
        )

        defaults = _as_mapping(raw.get("defaults", {}), "defaults")
        _reject_unknown(defaults, {"currency", "require_idempotency_key"}, "defaults")

        escalation = _as_mapping(raw.get("escalation", {}), "escalation")
        _reject_unknown(escalation, {"timeout_seconds", "on_timeout", "channel"}, "escalation")

        kill = _as_mapping(raw.get("kill_switch", {}), "kill_switch")
        _reject_unknown(kill, {"revoke_all_mandates"}, "kill_switch")

        breaker_raw = _as_mapping(raw.get("circuit_breaker", {}), "circuit_breaker")
        _reject_unknown(
            breaker_raw,
            {"enabled", "denials_per_session", "denials_per_subject"},
            "circuit_breaker",
        )
        fallback = CircuitBreaker()
        breaker = CircuitBreaker(
            enabled=_as_bool(breaker_raw.get("enabled", fallback.enabled), "enabled"),
            denials_per_session=_as_int(
                breaker_raw.get("denials_per_session", fallback.denials_per_session),
                "denials_per_session",
            ),
            denials_per_subject=_as_int(
                breaker_raw.get("denials_per_subject", fallback.denials_per_subject),
                "denials_per_subject",
            ),
        )

        return cls(
            merchant=str(raw.get("merchant", "")),
            version=_as_int(raw.get("version", POLICY_VERSION), "version"),
            currency=str(defaults.get("currency", "INR")),
            require_idempotency_key=_as_bool(
                defaults.get("require_idempotency_key", True), "require_idempotency_key"
            ),
            actions=_parse_actions(raw.get("actions", {})),
            regulatory=_parse_regulatory(raw.get("regulatory", {})),
            step_up_timeout_seconds=_as_int(
                escalation.get("timeout_seconds", 300), "timeout_seconds"
            ),
            on_step_up_timeout=str(escalation.get("on_timeout", "DENY")),
            kill_switch=_as_bool(kill.get("revoke_all_mandates", False), "revoke_all_mandates"),
            circuit_breaker=breaker,
        )


def _parse_actions(raw: object) -> dict[str, ActionPolicy]:
    mapping = _as_mapping(raw, "actions")
    unknown = set(mapping) - POLICY_ACTIONS
    if unknown:
        raise PolicyError(f"unknown action(s) in policy: {sorted(unknown)}")

    parsed: dict[str, ActionPolicy] = {}
    for name, body in mapping.items():
        cfg = _as_mapping(body, f"actions.{name}")
        _reject_unknown(
            cfg,
            {"enabled", "max_amount", "step_up_above", "daily_cap", "require_return_event"},
            f"actions.{name}",
        )
        parsed[name] = ActionPolicy(
            enabled=_as_bool(cfg.get("enabled", True), f"actions.{name}.enabled"),
            max_amount=_as_opt_int(cfg.get("max_amount"), f"actions.{name}.max_amount"),
            step_up_above=_as_opt_int(cfg.get("step_up_above"), f"actions.{name}.step_up_above"),
            daily_cap=_as_opt_int(cfg.get("daily_cap"), f"actions.{name}.daily_cap"),
            require_return_event=_as_bool(
                cfg.get("require_return_event", False), f"actions.{name}.require_return_event"
            ),
        )
    return parsed


def _parse_regulatory(raw: object) -> RegulatoryPolicy:
    cfg = _as_mapping(raw, "regulatory")
    _reject_unknown(
        cfg,
        {
            "npci_mandate_retries",
            "debit_blackout",
            "contact_window",
            "afa_threshold",
            "pre_debit_notice_seconds",
            "timezone",
        },
        "regulatory",
    )

    if "timezone" in cfg and cfg["timezone"] != "Asia/Kolkata":
        raise PolicyError(
            f"only Asia/Kolkata is supported, got {cfg['timezone']!r}; the encoded rules are "
            "Indian and reinterpreting them in another zone would be a compliance error"
        )

    blackout_raw = cfg.get("debit_blackout", [])
    if isinstance(blackout_raw, str):
        blackout_raw = [blackout_raw]
    if not isinstance(blackout_raw, list):
        raise PolicyError("regulatory.debit_blackout must be a list of windows")

    try:
        blackout = tuple(parse_window(str(w)) for w in blackout_raw)
        contact = parse_window(str(cfg["contact_window"])) if cfg.get("contact_window") else None
    except ValueError as exc:
        raise PolicyError(str(exc)) from exc

    return RegulatoryPolicy(
        npci_mandate_retries=_as_int(cfg.get("npci_mandate_retries", 3), "npci_mandate_retries"),
        debit_blackout=blackout,
        contact_window=contact,
        afa_threshold=_as_opt_int(cfg.get("afa_threshold", 1_500_000), "afa_threshold"),
        pre_debit_notice_seconds=_as_int(
            cfg.get("pre_debit_notice_seconds", 86_400), "pre_debit_notice_seconds"
        ),
    )


# ---------------------------------------------------------------- coercion helpers
def _reject_unknown(mapping: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = set(mapping) - allowed
    if unknown:
        raise PolicyError(f"unknown key(s) in {where}: {sorted(unknown)}")


def _as_mapping(value: object, where: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PolicyError(f"{where} must be a mapping, got {type(value).__name__}")
    return value


def _as_bool(value: object, where: str) -> bool:
    if not isinstance(value, bool):
        raise PolicyError(f"{where} must be true or false, got {value!r}")
    return value


def _as_int(value: object, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PolicyError(f"{where} must be an integer, got {value!r}")
    return value


def _as_opt_int(value: object, where: str) -> int | None:
    return None if value is None else _as_int(value, where)
