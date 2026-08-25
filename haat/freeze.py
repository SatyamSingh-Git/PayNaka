"""Does *this* repository carry the freeze tag?

Two published claims rest on `v1.0-freeze`. The sealed families were held out of
development entirely and scored once, after the tag was cut; the gap between 92.1% on the
visible families and 64.4% on the sealed ones is the project's only honest measure of
generalisation. Both `python -m haat.runner --corpus sealed` and
`python -m haat.sentinel_eval --include-sealed` refuse to run until the tag exists, so that
the held-out families cannot quietly become a development set.

Each of those two guards used to shell out to `git rev-parse v1.0-freeze` on its own, in
whatever directory the reader happened to be standing in. That is not a question about this
project. A reviewer downloaded the GitHub ZIP, which contains no `.git`, and ran the suite
from `Downloads/PayNaka-main/PayNaka-main`; git walked up the tree and answered from their
*home directory*, which happened to be a repository of their own. The guard was reading a
stranger's tags. Had that repo held a tag of this name, the sealed corpus would have opened
on a copy of the project that has no history at all -- the exact failure the guard exists to
prevent, arrived at by asking the wrong repository.

So the question is asked once, here, pinned to the directory this file lives in, and every
way of not knowing the answer resolves to "not frozen":

- no `git` on PATH, or git errors, or git hangs
- no work tree, or a work tree whose root is not this project
- the tag absent, or a *branch* or *file* of the same name standing in for it

That last one matters: `git rev-parse v1.0-freeze` resolves a branch named `v1.0-freeze`
just as happily as the tag, so the lookup below names `refs/tags/` explicitly.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

__all__ = ["FREEZE_TAG", "PROJECT_ROOT", "freeze_tag_exists", "under_version_control"]

#: The tag cut when the sealed families were closed and the held-out score was taken.
FREEZE_TAG = "v1.0-freeze"

#: The project, located from this file rather than from the process's working directory --
#: which is the whole point.
PROJECT_ROOT = Path(__file__).resolve().parents[1]

_TIMEOUT_SECONDS = 10.0


def _git(*arguments: str, root: Path) -> subprocess.CompletedProcess[str] | None:
    """Run git inside `root`, or return None if it could not be run at all.

    Never raises. A guard that crashes when git is missing is a guard that turns an
    unanswerable question into a stack trace, and the answer to an unanswerable question
    here is always "not frozen".
    """
    try:
        return subprocess.run(  # noqa: S603 - a fixed vector, no shell, no untrusted input
            ["git", *arguments],  # noqa: S607 - git is resolved from PATH, by design
            cwd=root,
            capture_output=True,
            check=False,
            text=True,
            timeout=_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _same_path(left: str, right: Path) -> bool:
    """Compare paths the way the filesystem does.

    git prints `E:/RazorPay` with forward slashes and whatever drive-letter case it was
    given; `Path.resolve()` preserves that case on Windows. Comparing the strings directly
    makes `e:` and `E:` two different projects, which would send every Windows reader down
    the "not this repository" branch and skip the check silently.

    The empty check is load-bearing and was found by the test rather than by reading:
    `Path("").resolve()` is the *current working directory*, so a git that exits 0 while
    printing nothing would have this function answer "yes, that is the project" for any
    reader whose shell happened to be sitting in it.
    """
    trimmed = left.strip()
    if not trimmed:
        return False
    try:
        here = Path(trimmed).resolve()
    except (OSError, ValueError):
        return False
    return os.path.normcase(str(here)) == os.path.normcase(str(right))


def under_version_control(*, root: Path = PROJECT_ROOT) -> bool:
    """Whether `root` is itself the top of a git work tree.

    Not "whether git can find *a* repository from here" -- that question is answered `yes`
    for an unpacked ZIP sitting anywhere beneath a repository, and the answer is about the
    wrong project.
    """
    completed = _git("rev-parse", "--show-toplevel", root=root)
    if completed is None or completed.returncode != 0:
        return False
    return _same_path(completed.stdout, root)


def freeze_tag_exists(*, root: Path = PROJECT_ROOT, tag: str = FREEZE_TAG) -> bool:
    """Whether this project's history carries the freeze tag. Fails closed."""
    if not under_version_control(root=root):
        return False
    completed = _git("rev-parse", "--verify", "--quiet", f"refs/tags/{tag}", root=root)
    return completed is not None and completed.returncode == 0
