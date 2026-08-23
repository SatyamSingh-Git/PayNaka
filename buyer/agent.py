"""The buying agent. Deliberately not hardened.

This is the thing under attack, and it is written the way a competent developer would
write it in a hurry -- which is to say, correctly plumbed and entirely trusting of what it
reads. Making it clever about injection would be missing the point twice over: it would
make the benchmark measure prompt engineering rather than architecture, and it would
implicitly concede that the defence belongs in the prompt.

The one thing it does not have is credentials. It cannot call Razorpay. It can only ask
PayNaka to, and PayNaka answers with deterministic code. That asymmetry is the entire
design, and it is why this file can afford to be naive.

Two system prompts ship side by side in ``prompts/``. HAAT runs both, because "our defence
beats an agent nobody bothered to warn" is not a result.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from buyer.brains import Brain, BrainError, Step
from buyer.tools import TOOL_SCHEMAS, ToolBox

__all__ = ["AgentRun", "BuyerAgent", "load_prompt"]

_PROMPTS = Path(__file__).parent / "prompts"


def load_prompt(name: str) -> str:
    """Load a system prompt by name: ``naive`` or ``hardened``."""
    path = _PROMPTS / f"{name}.txt"
    if not path.exists():
        available = sorted(p.stem for p in _PROMPTS.glob("*.txt"))
        raise FileNotFoundError(f"no prompt {name!r}; available: {available}")
    return path.read_text(encoding="utf-8")


@dataclass
class AgentRun:
    """A full transcript, and what it cost in money and tokens."""

    session_id: str
    turns: int = 0
    transcript: list[dict[str, Any]] = field(default_factory=list)
    money_moved: int = 0
    authorised: int = 0
    denials: list[dict[str, Any]] = field(default_factory=list)
    executions: list[dict[str, Any]] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    refused: bool = False
    error: str | None = None

    @property
    def overspent(self) -> int:
        """Paise moved beyond what the mandate authorised. The number that matters."""
        return max(0, self.money_moved - self.authorised)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "turns": self.turns,
            "money_moved": self.money_moved,
            "authorised": self.authorised,
            "overspent": self.overspent,
            "denials": self.denials,
            "executions": self.executions,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "refused": self.refused,
            "error": self.error,
        }


class BuyerAgent:
    """A shopping agent that holds no payment credentials."""

    def __init__(
        self,
        *,
        brain: Brain,
        tools: ToolBox,
        system_prompt: str,
        max_turns: int = 12,
    ) -> None:
        self.brain = brain
        self.tools = tools
        self.system_prompt = system_prompt
        self.max_turns = max_turns

    def shop(self, instruction: str, *, session_id: str | None = None) -> AgentRun:
        """Run the agent to completion, or until it runs out of turns.

        ``max_turns`` is a real bound, not a formality: an agent stuck in a loop against a
        gate that keeps denying it would otherwise burn tokens forever, and an attacker who
        can induce that loop has found a denial-of-wallet even without moving a rupee.
        """
        run = AgentRun(
            session_id=session_id or f"sess_{uuid.uuid4().hex[:12]}",
            authorised=self.tools.mandate.max_total,
        )
        messages: list[dict[str, Any]] = [{"role": "user", "content": instruction}]

        for _ in range(self.max_turns):
            run.turns += 1
            try:
                step = self.brain.next_step(self.system_prompt, messages, TOOL_SCHEMAS)
            except BrainError as exc:
                run.error = str(exc)
                return run

            run.tokens_in += step.usage.get("input_tokens", 0)
            run.tokens_out += step.usage.get("output_tokens", 0)

            if step.text:
                run.transcript.append({"role": "assistant", "text": step.text})

            if step.text == "[refused]":
                run.refused = True
                return run

            if not step.tool_calls:
                return run

            assistant_content = _assistant_content(step)
            messages.append({"role": "assistant", "content": assistant_content})

            results: list[dict[str, Any]] = []
            for call_id, name, args in step.tool_calls:
                outcome = self.tools.invoke(name, args)
                run.transcript.append({"role": "tool", "name": name, "args": args})

                if outcome.execution is not None:
                    if outcome.execution.executed:
                        run.money_moved += outcome.execution.money_moved
                        run.executions.append(outcome.execution.to_dict())
                    else:
                        run.denials.append(outcome.execution.decision.to_dict())

                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call_id,
                        "content": json.dumps(outcome.payload, ensure_ascii=False)[:8000],
                        **({"is_error": True} if outcome.is_error else {}),
                    }
                )

            messages.append({"role": "user", "content": results})

            if step.stop:
                return run

        run.error = f"agent did not finish within {self.max_turns} turns"
        return run


def _assistant_content(step: Step) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = []
    if step.text:
        content.append({"type": "text", "text": step.text})
    content.extend(
        {"type": "tool_use", "id": call_id, "name": name, "input": args}
        for call_id, name, args in step.tool_calls
    )
    return content
