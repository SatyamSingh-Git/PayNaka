"""The buying agent. Deliberately not hardened, and deliberately provider-agnostic.

This is the thing under attack, and it is written the way a competent developer would
write it in a hurry: correctly plumbed and entirely trusting of what it reads. Making it
clever about injection would miss the point twice -- it would turn the benchmark into a
measure of prompt engineering, and it would concede that the defence belongs in the
prompt.

The one thing it does not have is credentials. It cannot call Razorpay. It can only ask
PayNaka to, and PayNaka answers with deterministic code. That asymmetry is the entire
design, and it is why this file can afford to be naive.

The loop knows nothing about any model provider. History is a list of neutral ``Turn``s
and the brain renders it. That is what makes "the same agent, a different model" a true
statement rather than a claim about two similar programs.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from buyer.brains import Brain, BrainError, ToolCall, ToolResult, Turn
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
    model: str = ""
    served_by: str | None = None
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
            "model": self.model,
            "served_by": self.served_by,
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

        ``max_turns`` is a real bound, not a formality. An agent stuck in a loop against a
        gate that keeps denying it would otherwise burn tokens forever, and an attacker
        who can induce that loop has found a denial of wallet without moving a rupee.
        """
        run = AgentRun(
            session_id=session_id or f"sess_{uuid.uuid4().hex[:12]}",
            model=getattr(self.brain, "model", "unknown"),
            authorised=self.tools.mandate.max_total,
        )
        history: list[Turn] = [Turn(role="user", text=instruction)]

        for _ in range(self.max_turns):
            run.turns += 1
            try:
                step = self.brain.next_step(self.system_prompt, history, TOOL_SCHEMAS)
            except BrainError as exc:
                run.error = str(exc)
                break

            run.tokens_in += step.usage.get("input_tokens", 0)
            run.tokens_out += step.usage.get("output_tokens", 0)

            if step.text:
                run.transcript.append({"role": "assistant", "text": step.text})

            if step.refused:
                # Neither an attack success nor a defensive win: the model declined. It is
                # recorded on its own so it cannot be counted as either.
                run.refused = True
                break

            if not step.tool_calls:
                break

            history.append(Turn(role="assistant", text=step.text, tool_calls=step.tool_calls))
            history.append(Turn(role="user", tool_results=self._run_tools(step.tool_calls, run)))

            if step.stop:
                break
        else:
            run.error = f"agent did not finish within {self.max_turns} turns"

        run.served_by = getattr(self.brain, "served_by", None)
        return run

    def _run_tools(self, calls: tuple[ToolCall, ...], run: AgentRun) -> tuple[ToolResult, ...]:
        results: list[ToolResult] = []
        for call in calls:
            outcome = self.tools.invoke(call.name, call.arguments)
            run.transcript.append({"role": "tool", "name": call.name, "args": call.arguments})

            if outcome.execution is not None:
                if outcome.execution.executed:
                    run.money_moved += outcome.execution.money_moved
                    run.executions.append(outcome.execution.to_dict())
                else:
                    run.denials.append(outcome.execution.decision.to_dict())

            results.append(
                ToolResult(
                    call_id=call.id,
                    name=call.name,
                    # Bounded: a tool result is attacker-influenced text, and an unbounded
                    # one is a cheap way to blow out the context window.
                    content=json.dumps(outcome.payload, ensure_ascii=False)[:8000],
                    is_error=outcome.is_error,
                )
            )
        return tuple(results)
