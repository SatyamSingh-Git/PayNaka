"""What decides the agent's next move, across model providers.

HAAT's question is whether a *real* model can be talked into moving money it was not
authorised to move. Which model matters enormously to that answer and not at all to
PayNaka's, so the agent loop is provider-agnostic and the provider is a parameter.

That is not a convenience. It is the experiment. Susceptibility is a property of a model;
PayNaka's guarantee is a property of deterministic code. Run the same corpus across
several model families and the ``none`` and ``prompt`` rows should move a great deal
while the ``naka`` row stays flat at the floor. **That divergence is the result** -- and
a single-model benchmark cannot show it, because a flat line needs more than one point.

Four brains:

``ScriptedBrain``    replays a fixed plan. Offline, free, deterministic. Cannot be
                     injected, so its numbers never reach RESULTS.md.
``OpenRouterBrain``  any OpenAI-compatible model behind OpenRouter, with the serving
                     provider pinned so a benchmark is reproducible.
``ClaudeBrain``      the Anthropic SDK directly, when a first-party key is available.
``build_brain``      picks one from a spec string.

History is kept in a neutral :class:`Turn` form and each brain renders it to its own wire
format. Anthropic and OpenAI disagree about how tool calls and results are shaped, and
letting that disagreement leak into the agent would mean two agent loops -- which is two
experiments, not one experiment across two models.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

__all__ = [
    "Brain",
    "BrainError",
    "ClaudeBrain",
    "OpenRouterBrain",
    "ScriptedBrain",
    "Step",
    "ToolCall",
    "ToolResult",
    "Turn",
    "build_brain",
]


class BrainError(Exception):
    """The brain could not produce a next step."""


# ---------------------------------------------------------------------- neutral history
@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolResult:
    call_id: str
    name: str
    content: str
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class Turn:
    """One turn of conversation, in a shape neither provider imposed."""

    role: Literal["user", "assistant"]
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_results: tuple[ToolResult, ...] = ()


@dataclass(frozen=True, slots=True)
class Step:
    """What the model decided to do next."""

    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    stop: bool = False
    refused: bool = False
    usage: dict[str, int] = field(default_factory=dict)


class Brain(Protocol):
    name: str
    model: str

    def next_step(
        self, system: str, history: Sequence[Turn], tools: Sequence[dict[str, Any]]
    ) -> Step: ...


# ---------------------------------------------------------------------- scripted
@dataclass
class ScriptedBrain:
    """Replays a fixed plan. Deterministic, offline, free.

    It never reads the catalog, so it is immune to injection by construction -- which is
    exactly why it must never appear in a benchmark result. Its job is to prove the
    plumbing works, not to say anything about susceptibility.
    """

    plan: list[list[tuple[str, dict[str, Any]]]] = field(default_factory=list)
    name: str = "scripted"
    model: str = "scripted"
    _turn: int = 0

    def next_step(
        self, system: str, history: Sequence[Turn], tools: Sequence[dict[str, Any]]
    ) -> Step:
        if self._turn >= len(self.plan):
            return Step(text="done", stop=True)

        calls = self.plan[self._turn]
        self._turn += 1
        return Step(
            tool_calls=tuple(
                ToolCall(id=f"call_{self._turn}_{i}", name=name, arguments=args)
                for i, (name, args) in enumerate(calls)
            )
        )


# ---------------------------------------------------------------------- openrouter
#: Preferred serving provider per model slug.
#:
#: Pinning matters more than it looks. OpenRouter fans one slug out across many hosts,
#: and two hosts serving different quantisations of the same weights are two different
#: systems. A benchmark that silently load-balances between them is measuring the router.
DEFAULT_PROVIDER_ORDER: dict[str, list[str]] = {
    "deepseek/deepseek-v4-flash": ["deepseek"],
    "openai/gpt-5.6-luna": ["openai"],
    "openai/gpt-5.6-terra": ["openai"],
    "openai/gpt-5.6-sol": ["openai"],
}


@dataclass
class OpenRouterBrain:
    """Any OpenAI-compatible model, through OpenRouter, with the provider pinned."""

    model: str = "deepseek/deepseek-v4-flash"
    api_key: str | None = None
    max_tokens: int = 4096
    temperature: float = 0.0
    provider_order: list[str] | None = None
    allow_fallbacks: bool = False
    name: str = ""
    served_by: str | None = None
    _client: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:  # pragma: no cover - optional extra
                raise BrainError(
                    "the openai SDK is not installed; run `uv sync --extra agent`"
                ) from exc

            key = self.api_key or os.environ.get("OPENROUTER_API_KEY", "")
            if not key:
                raise BrainError("OPENROUTER_API_KEY is not set; see .env.example")

            self._client = OpenAI(
                base_url="https://openrouter.ai/api/v1",
                api_key=key,
                default_headers={
                    "HTTP-Referer": "https://github.com/SatyamSingh-Git/PayNaka",
                    "X-Title": "PayNaka / HAAT",
                },
            )
        if self.provider_order is None:
            self.provider_order = DEFAULT_PROVIDER_ORDER.get(self.model, [])
        self.name = f"openrouter:{self.model}"

    def next_step(
        self, system: str, history: Sequence[Turn], tools: Sequence[dict[str, Any]]
    ) -> Step:
        extra: dict[str, Any] = {}
        if self.provider_order:
            extra["provider"] = {
                "order": self.provider_order,
                "allow_fallbacks": self.allow_fallbacks,
            }

        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": system}, *_to_openai(history)],
                tools=[_openai_tool(tool) for tool in tools],
                tool_choice="auto",
                max_tokens=self.max_tokens,
                # Zero temperature is a reproducibility choice, not a quality one: a
                # benchmark that resamples on every run cannot demonstrate a regression.
                temperature=self.temperature,
                extra_body=extra or None,
            )
        except Exception as exc:
            raise BrainError(f"model call failed: {exc}") from exc

        # Recorded per run. A result that does not say which host served it is not
        # reproducible, however precise its percentages look.
        self.served_by = getattr(response, "provider", None) or self.served_by

        if not response.choices:
            raise BrainError("model returned no choices")

        choice = response.choices[0]
        message = choice.message

        calls: list[ToolCall] = []
        for call in message.tool_calls or []:
            try:
                arguments = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                # Malformed arguments are the model failing to use a tool, not the model
                # being injected. Recorded as an empty call so the run notes it rather
                # than the sweep dying on one bad response.
                arguments = {}
            calls.append(ToolCall(id=call.id, name=call.function.name, arguments=arguments))

        return Step(
            text=message.content or "",
            tool_calls=tuple(calls),
            stop=choice.finish_reason != "tool_calls",
            refused=choice.finish_reason == "content_filter",
            usage=_openai_usage(response),
        )


# ---------------------------------------------------------------------- anthropic
@dataclass
class ClaudeBrain:
    """The Anthropic SDK directly, when a first-party key is available."""

    model: str = ""
    api_key: str | None = None
    max_tokens: int = 4096
    effort: str = "high"
    name: str = ""
    _client: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - optional extra
                raise BrainError(
                    "the anthropic SDK is not installed; run `uv sync --extra agent`"
                ) from exc

            key = self.api_key or os.environ.get("ANTHROPIC_API_KEY", "")
            if not key:
                raise BrainError("ANTHROPIC_API_KEY is not set; see .env.example")
            self._client = anthropic.Anthropic(api_key=key)

        self.model = self.model or os.environ.get("PAYNAKA_AGENT_MODEL", "claude-opus-5")
        self.name = f"anthropic:{self.model}"

    def next_step(
        self, system: str, history: Sequence[Turn], tools: Sequence[dict[str, Any]]
    ) -> Step:
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                # System prompt and tool list are byte-stable across a run, so caching the
                # prefix turns thousands of runs from expensive into merely costly.
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=_to_anthropic(history),
                tools=[_anthropic_tool(tool) for tool in tools],
                # budget_tokens is rejected with a 400 on current models; depth is
                # controlled by output_config.effort instead.
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort},
            )
        except Exception as exc:
            raise BrainError(f"model call failed: {exc}") from exc

        if response.stop_reason == "refusal":
            return Step(text="[refused]", stop=True, refused=True, usage=_anthropic_usage(response))

        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                arguments = block.input if isinstance(block.input, dict) else {}
                calls.append(ToolCall(id=block.id, name=block.name, arguments=arguments))

        return Step(
            text="\n".join(text_parts),
            tool_calls=tuple(calls),
            stop=response.stop_reason != "tool_use",
            usage=_anthropic_usage(response),
        )


# ---------------------------------------------------------------------- wire formats
def _to_openai(history: Sequence[Turn]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for turn in history:
        if turn.role == "user" and not turn.tool_results:
            messages.append({"role": "user", "content": turn.text})
        elif turn.role == "assistant":
            entry: dict[str, Any] = {"role": "assistant", "content": turn.text or None}
            if turn.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments, ensure_ascii=False),
                        },
                    }
                    for call in turn.tool_calls
                ]
            messages.append(entry)
        else:
            # OpenAI wants one message per tool result, keyed by call id.
            messages.extend(
                {"role": "tool", "tool_call_id": result.call_id, "content": result.content}
                for result in turn.tool_results
            )
    return messages


def _to_anthropic(history: Sequence[Turn]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for turn in history:
        if turn.role == "user" and not turn.tool_results:
            messages.append({"role": "user", "content": turn.text})
        elif turn.role == "assistant":
            content: list[dict[str, Any]] = []
            if turn.text:
                content.append({"type": "text", "text": turn.text})
            content.extend(
                {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
                for call in turn.tool_calls
            )
            messages.append({"role": "assistant", "content": content})
        else:
            # Anthropic wants every tool result for a turn in ONE user message. Splitting
            # them silently trains the model out of making parallel tool calls.
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": result.call_id,
                            "content": result.content,
                            **({"is_error": True} if result.is_error else {}),
                        }
                        for result in turn.tool_results
                    ],
                }
            )
    return messages


def _openai_tool(tool: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool["input_schema"],
            "strict": bool(tool.get("strict", False)),
        },
    }


def _anthropic_tool(tool: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": tool["name"],
        "description": tool["description"],
        "input_schema": tool["input_schema"],
        **({"strict": True} if tool.get("strict") else {}),
    }


def _openai_usage(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    return {
        "input_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
    }


def _anthropic_usage(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    if usage is None:  # pragma: no cover - SDK always sets it
        return {}
    return {
        "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
        "cache_read_input_tokens": int(getattr(usage, "cache_read_input_tokens", 0) or 0),
    }


# ---------------------------------------------------------------------- factory
def build_brain(spec: str = "", **kwargs: Any) -> Brain:
    """Build a brain from a spec string.

    ``scripted``                        offline, for tests
    ``anthropic:claude-opus-5``         first-party Anthropic
    ``deepseek/deepseek-v4-flash``      OpenRouter -- a slug containing '/' implies it
    ``openrouter:openai/gpt-5.6-luna``  OpenRouter, spelled out
    """
    spec = spec or os.environ.get("PAYNAKA_BENCH_MODEL", "deepseek/deepseek-v4-flash")

    if spec == "scripted":
        return ScriptedBrain(plan=kwargs.get("plan", []))
    if spec.startswith("anthropic:"):
        return ClaudeBrain(model=spec.split(":", 1)[1], **kwargs)
    if spec.startswith("openrouter:"):
        return OpenRouterBrain(model=spec.split(":", 1)[1], **kwargs)
    if "/" in spec:
        return OpenRouterBrain(model=spec, **kwargs)
    raise BrainError(
        f"unrecognised model spec: {spec!r}. Expected 'scripted', 'anthropic:<model>', "
        "or an OpenRouter slug such as 'deepseek/deepseek-v4-flash'."
    )
