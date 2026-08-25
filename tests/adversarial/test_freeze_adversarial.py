"""Every way the freeze check can be told the wrong thing, or nothing at all.

The guard is a refusal, so its only dangerous failure is the one that *opens*. This file is
mostly about that direction: a tree with no history answering `yes` because something above
it said so, a branch or a file wearing the tag's name, git replying in a shape the parser
did not expect. Each must resolve to "not frozen", which is the same rule the money paths
follow -- ambiguity is a DENY.

The first test is a reproduction, not a hypothetical. A reviewer unpacked the GitHub ZIP
into `Downloads/PayNaka-main/PayNaka-main`, which contains no `.git`, and ran the suite.
git walked up the directory tree and answered from their home directory, which was a
repository of their own. The guard was reading somebody else's tags.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from haat import freeze
from haat.freeze import FREEZE_TAG, freeze_tag_exists, under_version_control
from tests.unit.test_freeze import make_repo

pytestmark = [
    pytest.mark.adversarial,
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH"),
]


class TestItAsksThisRepositoryAndNoOther:
    def test_an_unpacked_zip_inside_a_repository_is_not_frozen(self, tmp_path: Path) -> None:
        """The incident. `outer` is a tagged repository -- stand in for a home directory
        somebody ran `git init` in years ago. `outer/inner` is an extracted archive with no
        history whatsoever. It must not inherit the tag above it."""
        outer = make_repo(tmp_path / "outer", tag=FREEZE_TAG)
        unpacked = outer / "inner"
        unpacked.mkdir()
        (unpacked / "pyproject.toml").write_text("# a source download\n", encoding="utf-8")

        assert not (unpacked / ".git").exists()
        assert not under_version_control(root=unpacked), (
            "a directory with no history reported itself as version controlled because a "
            "parent directory happened to be a repository"
        )
        assert not freeze_tag_exists(root=unpacked), (
            "the sealed corpus would have opened on a copy with no history, on the strength "
            "of a tag belonging to an unrelated repository"
        )

    def test_a_nested_repository_answers_for_itself(self, tmp_path: Path) -> None:
        """The other half: a real repository inside a tagged one is still its own project,
        and an untagged one stays closed."""
        outer = make_repo(tmp_path / "outer2", tag=FREEZE_TAG)
        inner = make_repo(outer / "nested")
        assert under_version_control(root=inner)
        assert not freeze_tag_exists(root=inner)

    def test_a_directory_with_no_repository_anywhere_above_it(self, tmp_path: Path) -> None:
        bare = tmp_path / "loose"
        bare.mkdir()
        assert not under_version_control(root=bare)
        assert not freeze_tag_exists(root=bare)

    def test_a_missing_directory_does_not_raise(self, tmp_path: Path) -> None:
        """git is handed a `cwd` that does not exist, which raises before it ever starts."""
        assert not freeze_tag_exists(root=tmp_path / "nowhere")


class TestNothingElseMayWearTheTagsName:
    def test_a_branch_of_the_same_name_does_not_count(self, tmp_path: Path) -> None:
        """`git rev-parse v1.0-freeze` resolves a *branch* of that name just as happily as
        a tag, and a branch is a thing anyone can create after the fact. The lookup names
        `refs/tags/` explicitly; this is the test that keeps it doing so."""
        repo = make_repo(tmp_path / "branchy")
        subprocess.run(["git", "branch", FREEZE_TAG], cwd=repo, capture_output=True, check=True)
        assert not freeze_tag_exists(root=repo), (
            "a branch named v1.0-freeze satisfied a guard that is supposed to require a tag"
        )

    def test_a_file_of_the_same_name_does_not_count(self, tmp_path: Path) -> None:
        """The reviewer's transcript showed git printing `v1.0-freeze` on stdout while
        failing -- it was reading the argument as a path. A file of that name must not be
        mistaken for the tag."""
        repo = make_repo(tmp_path / "filey")
        (repo / FREEZE_TAG).write_text("not a tag\n", encoding="utf-8")
        assert not freeze_tag_exists(root=repo)

    def test_an_annotated_tag_does_count(self, tmp_path: Path) -> None:
        """The real tag may be annotated or lightweight; refusing one of the two would be an
        accident nobody would find until a submission."""
        repo = make_repo(tmp_path / "annotated")
        subprocess.run(
            [
                "git",
                "-c",
                "user.email=t@example.com",
                "-c",
                "user.name=t",
                "tag",
                "-a",
                FREEZE_TAG,
                "-m",
                "the freeze",
            ],
            cwd=repo,
            capture_output=True,
            check=True,
        )
        assert freeze_tag_exists(root=repo)


class TestWhenGitCannotAnswer:
    """Every unanswerable question resolves to `not frozen`, and none of them raise. A guard
    that crashes is a guard that turns a refusal into a stack trace in a reviewer's terminal.
    """

    @pytest.mark.parametrize(
        "failure",
        [
            FileNotFoundError("git"),
            PermissionError("git"),
            OSError("something else"),
            subprocess.TimeoutExpired(cmd="git", timeout=10),
            subprocess.SubprocessError("broken pipe"),
        ],
        ids=["no-git", "permission", "os-error", "timeout", "subprocess-error"],
    )
    def test_a_failing_git_is_not_a_freeze(
        self, monkeypatch: pytest.MonkeyPatch, failure: Exception
    ) -> None:
        def explode(*args: object, **kwargs: object) -> object:
            raise failure

        monkeypatch.setattr(freeze.subprocess, "run", explode)
        assert not under_version_control()
        assert not freeze_tag_exists()

    @pytest.mark.parametrize(
        "stdout",
        ["", "   \n", "not/a/real/path", "E:/SomeOtherProject"],
        ids=["empty", "whitespace", "wrong-path", "different-project"],
    )
    def test_a_toplevel_that_is_not_this_project_is_not_this_project(
        self, monkeypatch: pytest.MonkeyPatch, stdout: str
    ) -> None:
        """git exits 0 and names some other directory. That is the ZIP case in miniature,
        and it must not pass."""

        def reply(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args=["git"], returncode=0, stdout=stdout, stderr="")

        monkeypatch.setattr(freeze.subprocess, "run", reply)
        assert not under_version_control()
        assert not freeze_tag_exists()

    def test_a_nul_byte_in_the_path_is_refused_not_raised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`Path(...).resolve()` raises `ValueError` on an embedded NUL rather than
        returning anything, and an exception here would take the whole run down."""

        def reply(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=["git"], returncode=0, stdout="E:/Razor\x00Pay\n", stderr=""
            )

        monkeypatch.setattr(freeze.subprocess, "run", reply)
        assert not under_version_control()

    def test_the_drive_letter_case_does_not_change_the_answer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """git prints the path with whatever drive-letter case it was given, and Windows
        treats `e:` and `E:` as one drive. A naive string comparison sends every Windows
        reader down the "not this repository" branch, where the check silently stops
        happening."""
        flipped = str(freeze.PROJECT_ROOT).replace("\\", "/")
        flipped = flipped[0].swapcase() + flipped[1:]

        def reply(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=["git"], returncode=0, stdout=flipped + "\n", stderr=""
            )

        monkeypatch.setattr(freeze.subprocess, "run", reply)
        assert under_version_control()


class TestBothConsumersStayClosed:
    """One shared answer, two refusals. Neither may drift into its own copy again."""

    def test_the_sentinel_refuses_the_sealed_families_when_not_frozen(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from haat import sentinel_eval

        monkeypatch.setattr(sentinel_eval, "freeze_tag_exists", lambda: False)
        assert sentinel_eval.main(["--include-sealed"]) != 0
        assert "REFUSED" in capsys.readouterr().out

    def test_neither_module_shells_out_to_git_on_its_own(self) -> None:
        """The duplication that caused the incident: two guards asking git separately, in
        the reader's working directory, and one of them answering from a stranger's repo."""
        for name in ("haat/runner.py", "haat/sentinel_eval.py"):
            source = (freeze.PROJECT_ROOT / name).read_text(encoding="utf-8")
            assert "rev-parse" not in source, f"{name} has grown its own freeze check again"
