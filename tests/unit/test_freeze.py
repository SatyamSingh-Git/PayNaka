"""The freeze check, asked of repositories built for the purpose.

`haat.freeze` answers one question -- *does this project's history carry `v1.0-freeze`* --
and two refusals depend on the answer. The tests here build real git repositories in a
temporary directory rather than mocking `subprocess`, because every bug this module has had
was in what git actually replied, not in how the reply was parsed.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from haat.freeze import FREEZE_TAG, PROJECT_ROOT, freeze_tag_exists, under_version_control

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH")


def make_repo(root: Path, *, tag: str | None = None) -> Path:
    """A one-commit repository, optionally tagged. Identity is passed per-command so the
    test does not depend on the developer's global git config."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text("a repository\n", encoding="utf-8")
    run = lambda *args: subprocess.run(  # noqa: E731
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=root,
        capture_output=True,
        check=True,
    )
    run("init", "--quiet")
    run("add", "README.md")
    run("commit", "--quiet", "-m", "first")
    if tag is not None:
        run("tag", tag)
    return root


class TestTheProjectItself:
    """The live answer, in the repository this file is committed to."""

    def test_project_root_is_the_directory_holding_the_package(self) -> None:
        """Located from `__file__`, so it is the same answer wherever the reader's shell is."""
        assert (PROJECT_ROOT / "haat" / "freeze.py").is_file()
        assert (PROJECT_ROOT / "pyproject.toml").is_file()

    def test_the_project_reports_its_own_state(self) -> None:
        """Not asserting either value: a source download is a legitimate way to hold this
        code. What must hold is that the two answers agree -- a tag cannot be found in a
        tree that is not under version control."""
        if not under_version_control():
            assert not freeze_tag_exists()


class TestAgainstRealRepositories:
    def test_a_tagged_repository_is_frozen(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "tagged", tag=FREEZE_TAG)
        assert under_version_control(root=repo)
        assert freeze_tag_exists(root=repo)

    def test_an_untagged_repository_is_not(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "plain")
        assert under_version_control(root=repo)
        assert not freeze_tag_exists(root=repo)

    def test_some_other_tag_does_not_count(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "othertag", tag="v0.9")
        assert not freeze_tag_exists(root=repo)

    def test_the_tag_name_is_the_one_the_makefile_and_docs_quote(self) -> None:
        """Three files tell a reader the tag is `v1.0-freeze`. If the constant drifts, the
        guard refuses a repository the documentation says is ready."""
        assert FREEZE_TAG == "v1.0-freeze"
        makefile = (PROJECT_ROOT / "Makefile").read_text(encoding="utf-8")
        assert FREEZE_TAG in makefile
