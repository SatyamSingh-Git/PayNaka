"""Run this project's tasks on any operating system.

Every command in the README is a `make` target, and `make` is not installed on a default
Windows box. A reviewer who opens PowerShell, runs `uv sync`, types `make check` and is told
*"the term 'make' is not recognized"* has met the project's first real failure before seeing
any of it work.

So this is `make`, in Python, for the subset of the syntax this project's Makefile uses:

    python make.py check          # anywhere -- Windows, macOS, Linux
    python make.py --list         # every task, with its one-line description

**It reads the Makefile rather than duplicating it.** A second copy of thirty-six commands
would drift from the first within a week, and the copy nobody runs is the one that rots. The
Makefile stays the single source of truth; this file is an interpreter for it, and a target
added there works here with no further edit.

What it deliberately does not do is implement `make`. There is no dependency graph, no
timestamp checking, no pattern rules. This project's Makefile is a list of named scripts, and
that is all this runs.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

MAKEFILE = Path(__file__).with_name("Makefile")

#: `SHELL := /bin/bash` and friends. Assignment forms this Makefile actually uses.
_ASSIGN = re.compile(r"^(\w+)\s*:?=\s*(.*)$")
#: `target: deps ## description`
_TARGET = re.compile(r"^([a-zA-Z0-9_-]+)\s*:([^=]*?)(?:##\s*(.*))?$")


@dataclass(slots=True)
class Task:
    name: str
    deps: list[str] = field(default_factory=list)
    help: str = ""
    lines: list[str] = field(default_factory=list)


def parse(path: Path = MAKEFILE) -> tuple[dict[str, Task], dict[str, str]]:
    """Read the Makefile into tasks and variables.

    Recipe lines are the tab-indented ones, joined across trailing backslashes the way make
    joins them. `.PHONY` is skipped: every target here is phony, so the declaration carries
    no information this runner needs.
    """
    tasks: dict[str, Task] = {}
    variables: dict[str, str] = {}
    current: Task | None = None
    pending = ""

    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.startswith("\t"):
            if current is None:
                continue
            line = raw[1:]
            if pending:
                line = pending + " " + line.strip()
                pending = ""
            if line.rstrip().endswith("\\"):
                pending = line.rstrip()[:-1].rstrip()
                continue
            current.lines.append(line)
            continue

        stripped = raw.strip()
        current = None
        if not stripped or stripped.startswith("#") or stripped.startswith(".PHONY"):
            continue

        assignment = _ASSIGN.match(stripped)
        if assignment and ":" not in assignment.group(1):
            variables[assignment.group(1)] = assignment.group(2).strip()
            continue

        target = _TARGET.match(stripped)
        if target:
            name, deps, description = target.groups()
            current = Task(
                name=name,
                deps=[d for d in (deps or "").split() if d],
                help=(description or "").strip(),
            )
            tasks[name] = current

    return tasks, variables


def expand(line: str, variables: dict[str, str]) -> str:
    """Substitute `$(NAME)` until nothing changes.

    Looped rather than done once because `$(PY)` expands to `uv run`, and a value that itself
    contains a variable would otherwise reach the shell unexpanded.
    """
    for _ in range(8):
        replaced = re.sub(r"\$\((\w+)\)", lambda m: variables.get(m.group(1), m.group(0)), line)
        if replaced == line:
            return replaced
        line = replaced
    return line


def run(name: str, tasks: dict[str, Task], variables: dict[str, str], seen: set[str]) -> int:
    """Run one task and everything it depends on, once each."""
    if name in seen:
        return 0
    seen.add(name)

    task = tasks.get(name)
    if task is None:
        print(f"make.py: no task named {name!r}. Try --list.", file=sys.stderr)
        return 2

    for dependency in task.deps:
        if dependency in tasks:
            code = run(dependency, tasks, variables, seen)
            if code != 0:
                return code

    for line in task.lines:
        command = expand(line, variables)

        # `@` suppresses echo, `-` ignores failure. make accepts them in either order and
        # in either combination, so they are peeled in a loop rather than in one guessed
        # sequence -- `-@cmd` otherwise leaves the `@` in the command that gets echoed.
        quiet = keep_going = False
        while command[:1] in "@-":
            quiet |= command[0] == "@"
            keep_going |= command[0] == "-"
            command = command[1:]
        command = command.strip()
        if not command:
            continue

        if not quiet:
            print(f"$ {command}")

        # Through a shell, because the recipes use `&&`, `||` and pipes. On Windows that is
        # cmd.exe, which handles those three the same way -- the recipes here stay inside
        # that intersection deliberately.
        completed = subprocess.run(command, shell=True, check=False)  # noqa: S602
        if completed.returncode != 0 and not keep_going:
            print(
                f"make.py: task {name!r} failed (exit {completed.returncode})",
                file=sys.stderr,
            )
            return completed.returncode

    return 0


def show(tasks: dict[str, Task]) -> None:
    print("\nPayNaka tasks — run any of these with `python make.py <task>`\n")
    for name, task in sorted(tasks.items()):
        if task.help:
            print(f"  {name:<20} {task.help}")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python make.py",
        description="Run this project's tasks on any OS. Reads the Makefile; does not copy it.",
    )
    parser.add_argument("tasks", nargs="*", help="task names, in order. Default: help")
    parser.add_argument("--list", action="store_true", help="list every task and exit")
    args = parser.parse_args(argv)

    if not MAKEFILE.exists():
        print(f"make.py: no Makefile beside {__file__}", file=sys.stderr)
        return 2

    tasks, variables = parse()

    if args.list or not args.tasks:
        show(tasks)
        if shutil.which("make") is None and os.name == "nt":
            print("  (`make` is not on your PATH — that is why this exists.)\n")
        return 0

    for name in args.tasks:
        code = run(name, tasks, variables, seen=set())
        if code != 0:
            return code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
