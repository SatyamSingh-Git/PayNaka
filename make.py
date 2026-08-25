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
import threading
import time
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

MAKEFILE = Path(__file__).with_name("Makefile")

#: How long to wait before saying a command is still running. Long enough that nothing
#: short ever prints a tick, short enough to answer "is this frozen" before a reader
#: gives up on it.
HEARTBEAT_SECONDS = 15.0

#: The gap doubles after each tick, up to this. The question a tick answers -- *is this
#: stuck* -- is asked hardest in the first minute and barely at all after that, and a
#: command that prints its own progress (pytest's dots) needs no help from us at all. The
#: runner cannot portably tell those apart, so it says its piece and then goes quiet.
HEARTBEAT_CAP_SECONDS = 60.0

#: Commands quicker than this finish before anyone wonders, and their timing is noise.
SLOW_SECONDS = 5.0

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


def format_seconds(seconds: float) -> str:
    """`4.3s`, `1m19s`. Short enough to sit at the end of a line without explaining itself."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes}m{rest:02d}s"


def tick_gaps(
    first: float = HEARTBEAT_SECONDS, cap: float = HEARTBEAT_CAP_SECONDS
) -> Iterator[float]:
    """How long to wait before each successive tick: `first`, then doubling up to `cap`.

    A separate generator because the alternative is a test that waits real seconds to prove
    a backoff, and because the sequence is the whole of the behaviour worth arguing about.
    """
    gap = first
    while True:
        yield gap
        gap = min(gap * 2, cap)


@contextmanager
def heartbeat(*, enabled: bool, interval: float = HEARTBEAT_SECONDS) -> Generator[None]:
    """Say the command is still alive, while it is saying nothing itself.

    `mypy` prints one line, at the end. On a warm cache that is four seconds and nobody
    notices; on the cold cache a reviewer has after cloning, it is eighty seconds of an
    apparently frozen terminal after the echoed command. A reader reported exactly that,
    and reading it as a hang is the correct reading of the evidence they were given.

    Ticks go to stderr, and only when stderr is a terminal: a redirected log gets the
    commands and their output and none of this.

    The gap doubles after each tick, because a command that talks for itself needs no help
    and the runner cannot tell which one it is holding. `pytest` prints a dot per test, and
    a fixed fifteen-second tick landed in the middle of that stream seven times in one run
    -- noise beside output that was already proving it was alive. Backing off leaves the
    first minute densely reported, which is when "is this stuck" is actually being asked,
    and quiets down afterwards.

    A daemon thread woken by an `Event` rather than a sleep loop, so it stops the moment
    the command returns instead of up to `interval` later -- including when the command
    fails, which is when a stray thread printing over the error would be least welcome.
    """
    if not enabled:
        yield
        return

    stop = threading.Event()
    started = time.monotonic()

    def tick() -> None:
        for gap in tick_gaps(interval):
            if stop.wait(gap):
                return
            waited = format_seconds(time.monotonic() - started)
            print(f"  ... still running ({waited})", file=sys.stderr, flush=True)

    thread = threading.Thread(target=tick, daemon=True, name="make.py-heartbeat")
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=1.0)


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

        # `flush` is not cosmetic. Python block-buffers stdout when it is a pipe, while the
        # child process writes to that pipe directly -- so in any redirected log, every
        # echoed command came out *after* the output it introduced, and a failure appeared
        # underneath the wrong task. On a terminal it looked fine, which is why it survived.
        if not quiet:
            print(f"$ {command}", flush=True)

        # Through a shell, because the recipes use `&&`, `||` and pipes. On Windows that is
        # cmd.exe, which handles those three the same way -- the recipes here stay inside
        # that intersection deliberately.
        started = time.monotonic()
        with heartbeat(enabled=sys.stderr.isatty()):
            completed = subprocess.run(command, shell=True, check=False)  # noqa: S602
        waited = time.monotonic() - started

        # Only for commands slow enough that somebody wondered. It is also the answer to
        # "why was that so long": a cold mypy cache is eighty seconds and a warm one four,
        # and seeing both numbers once is what makes the second run unsurprising.
        if waited >= SLOW_SECONDS and not quiet:
            print(f"  {format_seconds(waited)}", flush=True)

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
    # Exists so the runner can be tested against a Makefile written for the test, rather
    # than against this project's real one -- which would make every test of the runner
    # also a test of ruff, mypy and the network they might reach.
    parser.add_argument(
        "--file", type=Path, default=MAKEFILE, help=argparse.SUPPRESS, dest="makefile"
    )
    args = parser.parse_args(argv)

    if not args.makefile.exists():
        print(f"make.py: no Makefile at {args.makefile}", file=sys.stderr)
        return 2

    tasks, variables = parse(args.makefile)

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
