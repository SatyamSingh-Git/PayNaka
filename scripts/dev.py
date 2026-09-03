"""Run the three dev processes together, on any operating system.

The Makefile recipe was `uvicorn merchant & uvicorn paynaka & cd console && npm run dev`.
`&` is POSIX backgrounding; in cmd.exe it is a *command separator*, so on Windows this
started the merchant, blocked on it forever, and never reached the other two. `make dev`
appeared to work -- something was clearly starting -- and the console it was supposed to
serve never came up.

That is the fourth POSIX-ism to reach a reader's PowerShell in this repository, and the
guard written for the previous three did not catch it: it looked for `|| true`,
`command -v` and `&& true`, which are the three that had already gone wrong. It looks for
backgrounding too now.

Ctrl+C stops all three. Children are terminated rather than left orphaned, because a
uvicorn holding :8002 after the terminal closed is a port conflict somebody debugs for
twenty minutes the next morning.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONSOLE = ROOT / "console"

#: `uv run` so each child gets the project environment, the same as every other recipe.
SERVICES: tuple[tuple[str, list[str], Path], ...] = (
    ("merchant :8001", ["uv", "run", "uvicorn", "merchant.app:app", "--port", "8001"], ROOT),
    ("paynaka  :8002", ["uv", "run", "uvicorn", "paynaka.app:app", "--port", "8002"], ROOT),
    ("console  :5173", ["npm", "run", "dev"], CONSOLE),
)


def main() -> int:
    if not (CONSOLE / "node_modules").is_dir():
        print(
            "console/node_modules is missing. Run `python make.py setup`, or "
            "`cd console && npm install`.",
            file=sys.stderr,
        )
        return 2

    running: list[tuple[str, subprocess.Popen[bytes]]] = []
    try:
        for label, command, cwd in SERVICES:
            print(f"starting {label}")
            running.append(
                (
                    label,
                    subprocess.Popen(  # noqa: S603 - fixed argument vectors, no shell
                        command,
                        cwd=cwd,
                        # npm on Windows is a .cmd shim, which CreateProcess will not run
                        # directly. This is the one place a shell is unavoidable.
                        shell=(os.name == "nt" and command[0] == "npm"),
                    ),
                )
            )
            # uvicorn binds before the console proxies to it. Not a race the console
            # cannot survive -- vite retries -- but the first page load is cleaner.
            time.sleep(0.6)

        print("\n  console  http://localhost:5173\n  Ctrl+C stops all three\n")
        while True:
            for label, process in running:
                code = process.poll()
                if code is not None:
                    print(f"{label} exited ({code}); stopping the rest", file=sys.stderr)
                    return code or 1
            time.sleep(0.4)
    except KeyboardInterrupt:
        print("\nstopping")
        return 0
    finally:
        for _, process in running:
            if process.poll() is None:
                process.terminate()
        deadline = time.monotonic() + 5
        for _, process in running:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                # A uvicorn still holding :8002 after this exits is a port conflict
                # somebody spends twenty minutes on tomorrow.
                process.kill()


if __name__ == "__main__":  # pragma: no cover - entry point
    signal.signal(signal.SIGINT, signal.default_int_handler)
    raise SystemExit(main())
