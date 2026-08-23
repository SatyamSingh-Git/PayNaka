"""What decides the agent's next move.

Two implementations, for two different jobs.

``ClaudeBrain`` is a real Claude tool-use loop. It is what HAAT measures, because the
whole question is whether a genuine language model can be talked into moving money it was
not authorised to move. A scripted stand-in cannot answer that -- it has no mind to change.

``ScriptedBrain`` replays a fixed sequence of tool calls. It exists so the test suite and
CI can exercise every path through the agent, the gate and the rails without an API key,
without network access, and without paying per run. It is never used to produce a number
that appears in RESULTS.md.

Keeping them behind one protocol is what lets the same agent code serve both.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, cast

if TYPE_CHECKING:  # pragma: no cover
    from anthropic.types import (
        MessageParam,
        OutputConfigParam,
        TextBlockParam,
        ThinkingConfigAdaptiveParam,
        ToolParam,
    )

__all__ = ["Brain", "BrainError", "ClaudeBrain", "ScriptedBrain", "Step", "build_brain"]


class BrainError(Exception):
    """The brain could not produce a next step."""


@dataclass(frozen=True, slots=True)
class Step:
    """One turn: either tool calls to run, or a final answer."""

    text: str = ""
    tool_calls: tuple[tuple[str, str, dict[str, Any]], ...] = ()  # (id, name, arguments)
    stop: bool = False
    usage: dict[str, int] = field(default_factory=dict)


class Brain(Protocol):
    name: str

    def next_step(
        self, system: str, messages: list[dict[str, Any]], tools: Sequence[dict[str, Any]]
    ) -> Step: ...


# ---------------------------------------------------------------------- scripted
@dataclass
class ScriptedBrain:
    """Replays a fixed plan. Deterministic, offline, free.

    Because it never reads the catalog, it is immune to injection by construction -- which
    is precisely why it must never appear in a benchmark result. Its job is to prove the
    plumbing works, not to prove anything about susceptibility.
    """

    plan: list[list[tuple[str, dict[str, Any]]]]
    name: str = "scripted"
    _turn: int = 0

    def next_step(
        self, system: str, messages: list[dict[str, Any]], tools: Sequence[dict[str, Any]]
    ) -> Step:
        if self._turn >= len(self.plan):
            return Step(text="done", stop=True)

        calls = self.plan[self._turn]
        self._turn += 1
        return Step(
            tool_calls=tuple(
                (f"call_{self._turn}_{i}", name, args) for i, (name, args) in enumerate(calls)
            )
        )


# ---------------------------------------------------------------------- claude
class ClaudeBrain:
    """A real Claude tool-use loop.

    Deliberately configured the way a competent developer would configure it, not the way
    that makes our numbers look best: adaptive thinking on, strict tool schemas, and a
    system prompt the caller chooses. HAAT's ``prompt`` defence row supplies a hardened
    prompt; the ``none`` row supplies an ordinary one. Beating a strawman would prove
    nothing, and a payments panel would spot it.
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        max_tokens: int = 4096,
        effort: str = "high",
    ) -> None:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - depends on install extras
            raise BrainError(
                "the anthropic SDK is not installed; run `uv sync --extra agent`"
            ) from exc

        key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            raise BrainError("ANTHROPIC_API_KEY is not set; see .env.example")

        self._client = anthropic.Anthropic(api_key=key)
        self.model = model or os.environ.get("PAYNAKA_AGENT_MODEL", "claude-opus-5")
        self.name = f"claude:{self.model}"
        self._max_tokens = max_tokens
        self._effort = effort

    def next_step(
        self, system: str, messages: list[dict[str, Any]], tools: Sequence[dict[str, Any]]
    ) -> Step:
        try:
            # The system prompt and tool list are byte-stable across a run, so caching
            # the prefix turns a benchmark of thousands of runs from expensive into merely
            # costly. Verify it is working by watching cache_read_input_tokens.
            system_blocks = cast(
                "list[TextBlockParam]",
                [
                    {
                        "type": "text",
                        "text": system,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            )
            response = self._client.messages.create(
                model=self.model,
                max_tokens=self._max_tokens,
                system=system_blocks,
                messages=cast("list[MessageParam]", messages),
                tools=cast("list[ToolParam]", list(tools)),
                # budget_tokens is rejected with a 400 on Opus 5; depth is controlled by
                # output_config.effort instead.
                thinking=cast("ThinkingConfigAdaptiveParam", {"type": "adaptive"}),
                output_config=cast("OutputConfigParam", {"effort": self._effort}),
            )
        except Exception as exc:
            raise BrainError(f"model call failed: {exc}") from exc

        if response.stop_reason == "refusal":
            # The model declined outright. Not a tool call and not an answer -- and for
            # HAAT it is neither an attack success nor a defence, so it is reported as
            # its own outcome rather than folded into either.
            return Step(text="[refused]", stop=True, usage=_usage(response))

        text_parts: list[str] = []
        calls: list[tuple[str, str, dict[str, Any]]] = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                args = (
                    block.input if isinstance(block.input, dict) else json.loads(str(block.input))
                )
                calls.append((block.id, block.name, args))

        return Step(
            text="\n".join(text_parts),
            tool_calls=tuple(calls),
            stop=response.stop_reason != "tool_use",
            usage=_usage(response),
        )


def _usage(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    if usage is None:  # pragma: no cover - SDK always sets it
        return {}
    return {
        "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
        "cache_read_input_tokens": int(getattr(usage, "cache_read_input_tokens", 0) or 0),
    }


def build_brain(kind: str = "claude", **kwargs: Any) -> Brain:
    if kind == "claude":
        return ClaudeBrain(**kwargs)
    if kind == "scripted":
        return ScriptedBrain(plan=kwargs.get("plan", []))
    raise BrainError(f"unknown brain: {kind!r}")
