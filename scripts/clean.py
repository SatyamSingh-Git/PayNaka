"""Remove generated artifacts. Never source, never evidence, never credentials.

The recipe this replaces was `rm -rf ... && find ... -exec rm -rf {} +`. Neither `rm` nor
`find` exists on Windows, so `make clean` was one of several tasks that simply could not run
there — and `clean` is the one task where failing halfway is worst, because a reader cannot
tell what it managed to delete before it stopped.

Two things it deliberately does **not** remove, both of which the old `rm -rf var` did:

``var/fixtures/``   the committed audit chains, one intact and one tampered. They are
                    evidence, they are in git, and regenerating them changes their hashes.
``var/evidence/``   the raw Razorpay test-mode responses. Same reason, and they cannot be
                    regenerated at all without live keys.

`.env` is not touched either. It holds credentials that exist nowhere else.
"""

from __future__ import annotations

import pathlib
import shutil

#: Directories and files that are pure build output.
DISPOSABLE = (
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".hypothesis",
    "htmlcov",
    ".coverage",
    "console/dist",
)

#: Everything under `var/` except these. They are committed evidence.
KEEP_UNDER_VAR = {"fixtures", "evidence"}


def _remove(path: pathlib.Path) -> bool | None:
    """Delete ``path``. ``True`` if it went, ``False`` if absent, ``None`` if it is in use.

    In use is not an error. On Windows a running service holds an open handle to its
    database and the delete fails with WinError 32 — which is the correct outcome, not a
    crash. Cleaning is best-effort by nature, and a clean that dies halfway leaves a reader
    unable to tell what it managed to remove before it stopped.
    """
    if not path.exists():
        return False
    try:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    except (PermissionError, OSError):
        return None
    return True


def main() -> int:
    removed = 0

    skipped: list[str] = []

    for name in DISPOSABLE:
        result = _remove(pathlib.Path(name))
        if result:
            print(f"  removed {name}")
            removed += 1
        elif result is None:
            skipped.append(name)

    var = pathlib.Path("var")
    if var.is_dir():
        for child in var.iterdir():
            if child.name in KEEP_UNDER_VAR:
                continue
            result = _remove(child)
            if result:
                print(f"  removed var/{child.name}")
                removed += 1
            elif result is None:
                skipped.append(f"var/{child.name}")

    # __pycache__ anywhere except inside the virtualenv or git's own directory.
    for cache in pathlib.Path().rglob("__pycache__"):
        if any(part in {".git", ".venv", "node_modules"} for part in cache.parts):
            continue
        if _remove(cache):
            removed += 1

    print(f"clean: removed {removed} item(s). Kept var/fixtures, var/evidence and .env.")
    if skipped:
        print(f"  in use, left alone: {', '.join(skipped)}")
        print("  (a running service holds these. Stop it and run again if you need them gone.)")
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
