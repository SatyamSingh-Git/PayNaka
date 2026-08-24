"""Two writers, one results file -- the corruption that looks like data.

This module exists because of a real incident. Two runners with different ``--defences``
appended to one JSONL for an hour: a wrapper process had been killed and the Python beneath
it had not, so a sweep everybody believed was dead kept writing. The result was valid JSON,
plausible ids, sensible verdicts, and a silent mixture of two configurations -- and it spent
the day's entire provider quota.

The tests are arranged around the two ways that happens:

* **Two live writers.** The second must refuse before its first model call, not interleave.
* **One writer, a different question.** Resuming with a different corpus, defence set, kind
  or model is appending to somebody else's evidence.

And the case that must *not* be treated as either: a crashed run leaving a lock behind. A
guard that requires somebody to know about lock files at 2 a.m. is a guard that gets deleted.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from haat.runlock import RunLock, RunStamp, SweepConflict

pytestmark = pytest.mark.adversarial


def a_stamp(**overrides: object) -> RunStamp:
    fields: dict[str, object] = {
        "corpus": "visible",
        "defences": ("none",),
        "kind": "attacks",
        "model": "nvidia/nemotron-3.5-lightning:free",
    }
    fields.update(overrides)
    return RunStamp(**fields)  # type: ignore[arg-type]


@pytest.fixture
def results(tmp_path: Path) -> Path:
    return tmp_path / "visible.jsonl"


class TestOnlyOneRunnerOwnsAFile:
    def test_the_first_runner_acquires(self, results: Path) -> None:
        """The control. A lock that refuses everybody is not a lock."""
        with RunLock(results, a_stamp()):
            assert results.with_suffix(".jsonl.lock").exists()

    def test_a_second_runner_is_refused(self, results: Path) -> None:
        with RunLock(results, a_stamp()), pytest.raises(SweepConflict, match="another sweep owns"):
            RunLock(results, a_stamp()).acquire()

    def test_the_refusal_names_the_owning_process(self, results: Path) -> None:
        """So somebody can go and find it -- which is the step that was missed."""
        with RunLock(results, a_stamp()):
            try:
                RunLock(results, a_stamp()).acquire()
            except SweepConflict as exc:
                assert str(os.getpid()) in str(exc)
            else:  # pragma: no cover - the call above must raise
                pytest.fail("expected SweepConflict")

    def test_the_refusal_warns_that_killing_a_wrapper_is_not_enough(self, results: Path) -> None:
        """The exact mistake that caused the incident, written into the error."""
        with RunLock(results, a_stamp()):
            try:
                RunLock(results, a_stamp()).acquire()
            except SweepConflict as exc:
                assert "wrapper" in str(exc)
            else:  # pragma: no cover
                pytest.fail("expected SweepConflict")

    def test_releasing_lets_the_next_runner_in(self, results: Path) -> None:
        lock = RunLock(results, a_stamp())
        lock.acquire()
        lock.release()
        with RunLock(results, a_stamp()):
            pass  # acquired without complaint

    def test_the_lock_is_released_even_when_the_sweep_raises(self, results: Path) -> None:
        with pytest.raises(RuntimeError), RunLock(results, a_stamp()):
            raise RuntimeError("the sweep exploded")
        assert not results.with_suffix(".jsonl.lock").exists()

    def test_a_crashed_run_does_not_block_the_next_one(self, results: Path) -> None:
        """A lock whose owner is gone is stale. Requiring somebody to know about lock files
        at 2 a.m. is how a guard gets deleted."""
        lock_path = results.with_suffix(".jsonl.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("999999999", encoding="utf-8")  # a pid that cannot exist
        with RunLock(results, a_stamp()):
            pass

    @pytest.mark.parametrize("contents", ["", "   ", "not-a-pid", "-1", "0"], ids=repr)
    def test_an_unreadable_lock_is_treated_as_stale(self, results: Path, contents: str) -> None:
        lock_path = results.with_suffix(".jsonl.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(contents, encoding="utf-8")
        with RunLock(results, a_stamp()):
            pass


class TestResumingSomebodyElsesEvidenceIsRefused:
    @pytest.mark.parametrize(
        "changed",
        [
            {"defences": ("naka",)},
            {"defences": ("none", "prompt", "naka", "judge")},
            {"kind": "all"},
            {"kind": "benign"},
            {"corpus": "sealed"},
            {"model": "deepseek/deepseek-v4-flash"},
        ],
        ids=["one-defence", "all-defences", "kind-all", "kind-benign", "corpus", "model"],
    )
    def test_a_different_configuration_cannot_append(
        self, results: Path, changed: dict[str, object]
    ) -> None:
        with RunLock(results, a_stamp()):
            pass
        with pytest.raises(SweepConflict, match="different sweep"):
            RunLock(results, a_stamp(**changed)).acquire()

    def test_the_refusal_names_the_fields_that_differ(self, results: Path) -> None:
        with RunLock(results, a_stamp()):
            pass
        try:
            RunLock(results, a_stamp(kind="all", model="other/model")).acquire()
        except SweepConflict as exc:
            assert "kind" in str(exc)
            assert "model" in str(exc)
        else:  # pragma: no cover
            pytest.fail("expected SweepConflict")

    def test_the_same_configuration_resumes(self, results: Path) -> None:
        """Resumability is the whole reason the JSONL is append-only. The guard must not
        break the feature it protects."""
        with RunLock(results, a_stamp()):
            pass
        with RunLock(results, a_stamp()):
            pass

    def test_defence_order_does_not_count_as_a_different_sweep(self, results: Path) -> None:
        """A tuple compares by order and `--defences` is a set in spirit. This asserts the
        current, stricter behaviour rather than pretending otherwise: it is documented, and
        a spurious refusal is recoverable while a missed one is not."""
        with RunLock(results, a_stamp(defences=("none", "naka"))):
            pass
        with pytest.raises(SweepConflict):
            RunLock(results, a_stamp(defences=("naka", "none"))).acquire()

    def test_a_corrupt_stamp_does_not_block_a_run(self, results: Path) -> None:
        """A stamp guards somebody's evidence. Refusing to run because the guard file is
        unreadable turns a cosmetic problem into a blocked sweep."""
        stamp_path = results.with_suffix(".jsonl.stamp")
        stamp_path.parent.mkdir(parents=True, exist_ok=True)
        stamp_path.write_text("{not json", encoding="utf-8")
        with RunLock(results, a_stamp()):
            pass

    @pytest.mark.parametrize("payload", ["[]", '"a string"', "null", "{}", '{"corpus": 1}'])
    def test_a_stamp_of_the_wrong_shape_does_not_block_a_run(
        self, results: Path, payload: str
    ) -> None:
        stamp_path = results.with_suffix(".jsonl.stamp")
        stamp_path.parent.mkdir(parents=True, exist_ok=True)
        stamp_path.write_text(payload, encoding="utf-8")
        with RunLock(results, a_stamp()):
            pass

    def test_the_stamp_survives_the_run_because_it_describes_the_results(
        self, results: Path
    ) -> None:
        """The lock is about the run; the stamp is about the file. Only one of them is
        released at the end."""
        with RunLock(results, a_stamp()):
            pass
        stamp_path = results.with_suffix(".jsonl.stamp")
        assert stamp_path.exists()
        written = json.loads(stamp_path.read_text(encoding="utf-8"))
        assert written["kind"] == "attacks"
        assert written["defences"] == ["none"]


class TestTheStampItself:
    def test_it_reports_every_differing_field(self) -> None:
        differences = a_stamp().differs_from(a_stamp(kind="all", corpus="sealed"))
        assert set(differences) == {"kind", "corpus"}

    def test_an_identical_stamp_differs_in_nothing(self) -> None:
        assert a_stamp().differs_from(a_stamp()) == []

    def test_it_round_trips(self) -> None:
        original = a_stamp()
        assert RunStamp.from_dict(original.to_dict()) == original

    @pytest.mark.parametrize("raw", [None, [], "x", 42, {}, {"corpus": "visible"}], ids=repr)
    def test_an_unparseable_stamp_is_none_rather_than_an_error(self, raw: object) -> None:
        assert RunStamp.from_dict(raw) is None
