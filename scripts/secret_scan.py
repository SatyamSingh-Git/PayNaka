"""Scan the working tree and full history for leaked credentials.

A one-line shell recipe used to do this: ``command -v gitleaks && gitleaks detect || echo``.
It worked on bash and printed *"The system cannot find the path specified"* on Windows,
because ``command -v`` is a POSIX builtin that cmd.exe does not have. The scan then reported
success — on a check whose entire job is to fail loudly.

That is the worst failure mode available to a security check: absent, and quiet about it.
So the logic moved into Python, where finding an executable means the same thing everywhere.

`gitleaks` is deliberately not a dependency of the test suite. It is a Go binary, and making
the Python suite need it would mean nobody runs the suite. When it is missing this says so
and exits 0 — a scan that cannot run is not a failure to report, but it must never look like
a scan that passed.
"""

from __future__ import annotations

import shutil
import subprocess
import sys

#: Working tree *and* history. A credential removed in the latest commit is still a
#: credential, and it is still in the clone somebody made yesterday.
ARGS = ["detect", "--no-banner", "--redact", "-v"]


def main() -> int:
    binary = shutil.which("gitleaks")
    if binary is None:
        print("gitleaks not installed - skipping. See docs/SECURITY.md to install it.")
        print("NOTE: no scan ran. This is not the same as a clean scan.")
        return 0

    completed = subprocess.run([binary, *ARGS], check=False)  # noqa: S603
    if completed.returncode != 0:
        print(
            "gitleaks found something, or could not complete. Do not ignore this.", file=sys.stderr
        )
    return completed.returncode


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
