"""The task runner that lets a reviewer on Windows run anything at all.

Every command in the README is a `make` target, and `make` is not on a default Windows box.
A reviewer who opens PowerShell, runs `uv sync`, types `make check` and reads *"the term
'make' is not recognized"* has met this project's first failure before seeing any of it work.
That happened, to a real reader, which is why `make.py` exists.

What matters about it is not that it runs commands — it is that it stays honest to the
Makefile. A task runner that drifts from the file it mirrors is worse than none, because the
commands in the README then work for some readers and not others, and nobody finds out until
it is a reviewer. So the tests below are mostly about the parse: every documented target must
be reachable, variables must expand, and the file must never become a second copy that rots
next to the first.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from make import MAKEFILE, expand, parse

MAKEFILE_TEXT = MAKEFILE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def parsed():  # type: ignore[no-untyped-def]
    return parse()


class TestItReadsTheRealMakefile:
    def test_every_documented_target_is_reachable(self, parsed) -> None:  # type: ignore[no-untyped-def]
        """A target with a `##` description appears in `make help`, so a reader will try it.
        Every one of those must run here too, or the runner is a partial mirror."""
        tasks, _ = parsed
        documented = {
            line.split(":", 1)[0]
            for line in MAKEFILE_TEXT.splitlines()
            if "## " in line and not line.startswith(("\t", "#", "."))
        }
        assert documented, "no documented targets found; the parser or the file changed shape"
        assert documented <= set(tasks), f"unreachable: {sorted(documented - set(tasks))}"

    @pytest.mark.parametrize(
        "name",
        ["check", "test", "lint", "types", "demo", "modelfree", "audit-verify", "replay-breaches"],
    )
    def test_the_targets_the_readme_tells_people_to_run(self, parsed, name: str) -> None:  # type: ignore[no-untyped-def]
        """Named individually because these are the ones in the Quickstart. If any of them
        stops resolving, a reader's first command fails."""
        tasks, _ = parsed
        assert name in tasks

    def test_recipes_are_captured_not_just_names(self, parsed) -> None:  # type: ignore[no-untyped-def]
        tasks, _ = parsed
        assert tasks["test"].lines, "a task with no recipe would silently do nothing"

    def test_dependencies_are_captured(self, parsed) -> None:  # type: ignore[no-untyped-def]
        """`check` runs four other tasks. Losing them would make it pass while checking
        nothing, which is the worst possible way for this to break."""
        tasks, _ = parsed
        assert set(tasks["check"].deps) == {"lint", "types", "test", "secrets"}

    def test_phony_declarations_are_not_mistaken_for_tasks(self, parsed) -> None:  # type: ignore[no-untyped-def]
        tasks, _ = parsed
        assert not any(name.startswith(".") for name in tasks)


class TestVariablesExpand:
    def test_the_python_runner_expands(self, parsed) -> None:  # type: ignore[no-untyped-def]
        """`$(PY)` is `uv run`. Unexpanded, every recipe would try to execute a literal
        dollar sign."""
        _, variables = parsed
        assert expand("$(PY) pytest", variables) == "uv run pytest"

    def test_an_unknown_variable_is_left_alone(self, parsed) -> None:  # type: ignore[no-untyped-def]
        """Better a visible `$(NOPE)` in the echoed command than a silently empty string
        that turns `rm -rf $(NOPE)/x` into something else entirely."""
        _, variables = parsed
        assert expand("echo $(NOPE)", variables) == "echo $(NOPE)"

    def test_expansion_terminates_on_a_self_referential_variable(self) -> None:
        assert expand("$(A)", {"A": "$(A)"}) == "$(A)"


class TestParsingEdgesThisMakefileActuallyContains:
    def test_a_continued_line_becomes_one_command(self, tmp_path: Path) -> None:
        """`secrets` and `bench-sealed` are written across several lines with backslashes.
        Split into separate commands they would run as three broken fragments."""
        makefile = tmp_path / "Makefile"
        makefile.write_text(
            "PY := uv run\n\nthing: ## does a thing\n\t@echo one \\\n\t  && echo two\n",
            encoding="utf-8",
        )
        tasks, _ = parse(makefile)
        assert len(tasks["thing"].lines) == 1
        assert "&&" in tasks["thing"].lines[0]

    def test_the_help_text_is_kept(self, tmp_path: Path) -> None:
        makefile = tmp_path / "Makefile"
        makefile.write_text("thing: ## a description\n\t@echo hi\n", encoding="utf-8")
        tasks, _ = parse(makefile)
        assert tasks["thing"].help == "a description"

    def test_comments_and_blank_lines_are_ignored(self, tmp_path: Path) -> None:
        makefile = tmp_path / "Makefile"
        makefile.write_text(
            "# a section\n\n.PHONY: thing\nthing: ## x\n\t@echo hi\n", encoding="utf-8"
        )
        tasks, _ = parse(makefile)
        assert set(tasks) == {"thing"}


class TestItDoesNotBecomeASecondCopy:
    def test_the_runner_hardcodes_no_recipe(self) -> None:
        """The whole design: `make.py` interprets the Makefile rather than duplicating it.
        The day somebody pastes a command in here, the two start drifting and the README
        works for some readers and not others."""
        source = Path("make.py").read_text(encoding="utf-8")
        for marker in ("uv run pytest", "ruff check .", "python -m haat", "mypy paynaka"):
            assert marker not in source, f"make.py has begun duplicating the Makefile: {marker!r}"

    def test_echo_recipes_carry_no_quotes(self) -> None:
        """cmd.exe prints the quotes in `echo "hi"`; bash does not. Unquoted reads correctly
        on both, and the difference is only ever noticed by the reader on the wrong OS."""
        offenders = [
            line.strip()
            for line in MAKEFILE_TEXT.splitlines()
            if line.startswith("\t") and line.lstrip("\t@").startswith('echo "')
        ]
        assert not offenders, f"quoted echo will print literal quotes on Windows: {offenders}"


class TestTheReadmeOnlyTellsPeopleToRunThingsThatWork:
    """A reader copies commands out of the README verbatim. Every one of them must work on
    the machine they are sitting at, which for a reviewer is as likely to be Windows as not.

    This exists because a reader followed the Quickstart on PowerShell and the very first
    command failed -- and then, after that was fixed, `make demo` further down failed the
    same way.
    """

    README = Path("README.md").read_text(encoding="utf-8")

    def test_no_bare_make_command_is_prescribed(self) -> None:
        """`make X` at the start of a line is a command a reader will paste. On Windows it
        is a command that does not exist."""
        offenders = [
            line.strip()
            for line in self.README.splitlines()
            if re.match(r"^\s*make\s+[a-z][a-z-]*", line)
        ]
        assert not offenders, f"README prescribes `make` directly: {offenders}"

    def test_every_prescribed_task_exists(self, parsed) -> None:  # type: ignore[no-untyped-def]
        """Catches the other direction: a command that is portable and names a task that was
        renamed or never existed."""
        tasks, _ = parsed
        named = set(re.findall(r"python make\.py ([a-z][a-z-]*)", self.README))
        assert named, "no portable commands found in the README"
        assert named <= set(tasks), f"README names missing tasks: {sorted(named - set(tasks))}"


class TestTheSecretScanIsHonestWhenItCannotRun:
    """The recipe used to be `command -v gitleaks && ... || echo`, which is POSIX. On Windows
    it printed "The system cannot find the path specified" and then reported success -- a
    security check that was absent and quiet about it, which is the worst state available."""

    def test_the_recipe_is_not_a_posix_shell_builtin(self) -> None:
        source = MAKEFILE_TEXT
        assert "command -v" not in source, (
            "`command -v` is a POSIX builtin; cmd.exe does not have it and fails silently"
        )

    def test_a_missing_scanner_says_no_scan_ran(self) -> None:
        """Not 'passed'. The distinction is the entire point."""
        from scripts import secret_scan

        source = Path(secret_scan.__file__).read_text(encoding="utf-8")
        assert "no scan ran" in source
