"""Show that the gate imports no model, on any operating system.

The claim this project leads with is that money decisions are made by arithmetic, not by a
model, and the honest form of that claim is *go and look*. The pitch script had a reviewer
looking with `grep -c "openai\\|anthropic" paynaka/gate.py`, which is fine on a Mac and is
`The term 'grep' is not recognized` on Windows -- typed live, on camera, on the one beat
whose whole point is that you can verify it yourself.

So the check is a task instead. It parses the module rather than searching its text, which
is the stronger reading anyway: `grep` counts occurrences of a string and would be fooled by
the word appearing in a comment, and would miss `import openai as o`. The AST knows what was
actually imported.

It prints the import block in full, because the count on its own asks to be trusted and the
imports do not.

`tests/adversarial/test_gate_adversarial.py::TestNoModelInTheDecisionPath` imports the same
set from here, so the demonstration and the test cannot drift apart -- and CI fails the
moment the invariant does.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

from paynaka.tty import BOLD, DIM, GREEN, OFF, RED, say

__all__ = ["FORBIDDEN", "GATE", "imports_of", "main"]

GATE = Path("paynaka/gate.py")

#: Model SDKs and the network clients that would reach one. `mcp` is here because the proxy
#: is a separate module by design: the gate must not know that a protocol exists, only that
#: an amount does.
FORBIDDEN = frozenset(
    {
        "anthropic",
        "openai",
        "langchain",
        "langgraph",
        "llama_index",
        "transformers",
        "litellm",
        "mcp",
        "requests",
        "httpx",
    }
)


def imports_of(path: Path) -> set[str]:
    """Top-level package name of everything the module imports.

    Parsed, not matched. `import openai as o` and a mention of "openai" in a docstring look
    identical to a text search and are not the same fact.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
    return found


def import_lines(path: Path) -> list[str]:
    """The import statements as written, for a reader who would rather see than count."""
    lines = path.read_text(encoding="utf-8").splitlines()
    tree = ast.parse("\n".join(lines))
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom) and node.col_offset == 0:
            end = node.end_lineno or node.lineno
            out.append(" ".join(line.strip() for line in lines[node.lineno - 1 : end]))
    return sorted(set(out))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.no_model", description=__doc__)
    parser.add_argument("--path", type=Path, default=GATE)
    args = parser.parse_args(argv)

    if not args.path.is_file():
        print(f"no such file: {args.path}", file=sys.stderr)
        return 2

    say()
    say(f"{BOLD}{args.path} — every import, in full{OFF}")
    say()
    for line in import_lines(args.path):
        say(f"  {line}")

    offenders = sorted(imports_of(args.path) & FORBIDDEN)
    say()
    say(f"{DIM}Searched for {len(FORBIDDEN)} model and network SDKs:{OFF}")
    say(f"{DIM}  {', '.join(sorted(FORBIDDEN))}{OFF}")
    say()

    if offenders:
        say(f"  found: {RED}{len(offenders)}{OFF}   {', '.join(offenders)}")
        say()
        say(f"{RED}The gate must decide with deterministic code and must not reach a model.{OFF}")
        return 1

    say(f"  found: {GREEN}0{OFF}")
    say()
    say("The gate decides with arithmetic. Enforced by")
    say(f"{DIM}  tests/adversarial/test_gate_adversarial.py::TestNoModelInTheDecisionPath{OFF}")
    say("so it cannot regress quietly.")
    say()
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
