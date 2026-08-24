"""One sweep per output file, and the same sweep it was before.

Written after a run that produced 432 rows for a 252-case corpus. Two runners with
different `--defences` had been appending to one JSONL for an hour: `TaskStop` had killed a
wrapper process and not the Python underneath it, so a sweep everybody believed was dead
kept going. The resulting file looked coherent -- valid JSON, plausible ids, sensible
verdicts -- and was a silent mixture of two configurations. It also spent the day's entire
free-tier quota.

Nothing detected it. Model-keyed resume catches a *model* change; it cannot see two runs of
the same model with different defences, and neither can a reader. The file is the evidence,
so a file that can be quietly wrong is the worst failure mode this harness has.

Two guards, and both are about the same thing: a results file should only ever be written
by the run that started it.

**A lock.** Exclusive creation, holding the writer's PID. A second runner refuses to start
rather than interleaving. A lock whose process is gone is stale and gets taken over, with a
line saying so -- a crashed run must not require somebody to know about lock files at 2 a.m.

**A configuration stamp.** What corpus, defences, kind and model produced these rows.
Resuming with a different shape is refused, because appending to somebody else's evidence is
how a partial sweep becomes a table nobody can interpret.

Neither guard is clever, and that is deliberate: this protects a file that costs a day's
quota to regenerate, and the mechanism should be readable in one sitting.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

__all__ = ["RunLock", "RunStamp", "StaleLock", "SweepConflict"]


class SweepConflict(Exception):
    """Another run owns this output, or produced it with a different configuration."""


class StaleLock(Exception):
    """Internal: a lock exists and its owner does not. Never escapes this module."""


@dataclass(frozen=True, slots=True)
class RunStamp:
    """What produced a results file. Compared, not merely recorded."""

    corpus: str
    defences: tuple[str, ...]
    kind: str
    model: str

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "defences": list(self.defences)}

    @classmethod
    def from_dict(cls, raw: object) -> RunStamp | None:
        """Parse a stamp, or ``None`` if it cannot be read.

        Unreadable is treated as absent rather than as an error: a stamp is a guard on
        somebody's evidence, and refusing to run because the guard file is corrupt would
        make a cosmetic problem into a blocked sweep.
        """
        if not isinstance(raw, dict):
            return None
        try:
            return cls(
                corpus=str(raw["corpus"]),
                defences=tuple(str(d) for d in raw["defences"]),
                kind=str(raw["kind"]),
                model=str(raw["model"]),
            )
        except (KeyError, TypeError):
            return None

    def differs_from(self, other: RunStamp) -> list[str]:
        """The field names that disagree, so a refusal can say which."""
        return [
            name
            for name in ("corpus", "defences", "kind", "model")
            if getattr(self, name) != getattr(other, name)
        ]


class RunLock:
    """Exclusive ownership of one results file, plus the stamp that describes it.

    Used as a context manager around a whole sweep. Everything it refuses, it refuses
    *before* the first model call, because the point is to fail while the quota is intact.
    """

    __slots__ = ("_lock_path", "_owned", "_stamp", "_stamp_path")

    def __init__(self, results: Path, stamp: RunStamp) -> None:
        self._lock_path = results.with_suffix(results.suffix + ".lock")
        self._stamp_path = results.with_suffix(results.suffix + ".stamp")
        self._stamp = stamp
        self._owned = False

    # ---------------------------------------------------------------- acquire
    def acquire(self) -> None:
        self._check_stamp()
        try:
            self._take()
        except StaleLock:
            # The owner is gone. Taking over is right -- a crashed run should not need
            # somebody to know this file exists -- but it is said out loud, because a lock
            # silently reclaimed twice in a row is a process crashing in a loop.
            print(
                f"stale lock at {self._lock_path} (owner is gone); taking it over",
                flush=True,
            )
            self._lock_path.unlink(missing_ok=True)
            self._take()
        self._stamp_path.write_text(
            json.dumps(self._stamp.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _take(self) -> None:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            handle = os.open(self._lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            owner = self._owner_pid()
            if owner is None or not _alive(owner):
                # A lock that cannot name a live owner is not protecting a running sweep.
                # An unreadable one used to fall through to a conflict, which meant a
                # truncated lock file blocked every future run with no way to tell why.
                raise StaleLock from exc
            raise SweepConflict(
                f"another sweep owns {self._lock_path.stem} (pid {owner}). Two runners "
                f"appending to one results file produce a coherent-looking mixture of two "
                f"configurations, which is exactly the failure this lock exists to prevent. "
                f"Stop the other run first -- and check with the process list, because "
                f"killing a wrapper does not kill the python underneath it."
            ) from exc
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(str(os.getpid()))
        self._owned = True

    def _owner_pid(self) -> int | None:
        try:
            return int(self._lock_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    def _check_stamp(self) -> None:
        if not self._stamp_path.exists():
            return
        try:
            existing = RunStamp.from_dict(json.loads(self._stamp_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, ValueError):
            return
        if existing is None:
            return
        differences = existing.differs_from(self._stamp)
        if differences:
            raise SweepConflict(
                f"these results were produced by a different sweep: "
                f"{', '.join(differences)} differ. Appending to somebody else's evidence "
                f"turns a partial sweep into a table nobody can interpret. Delete "
                f"{self._stamp_path.parent}/ to start fresh, or re-run with the original "
                f"configuration: {existing.to_dict()}"
            )

    # ---------------------------------------------------------------- release
    def release(self) -> None:
        """Give up the lock. The stamp stays -- it describes the results, not the run."""
        if self._owned:
            self._lock_path.unlink(missing_ok=True)
            self._owned = False

    def __enter__(self) -> RunLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def _alive(pid: int) -> bool:
    """Whether a process exists. Errs towards *alive*, which is the safe direction.

    Being wrong that way costs somebody a confusing refusal they can resolve. Being wrong
    the other way silently permits the two-writer corruption this module exists to stop.

    Windows needs its own path. ``os.kill(pid, 0)`` is a POSIX idiom, and on Windows CPython
    raises ``OSError`` with winerror 87 for a process that does not exist rather than
    ``ProcessLookupError`` -- so the POSIX version reads every dead process as alive, and a
    crashed sweep would block its own retry for ever.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        return _alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists and belongs to somebody else
    except OSError:
        return True
    return True


def _alive_windows(pid: int) -> bool:
    """``OpenProcess`` plus ``GetExitCodeProcess``.

    A handle alone is not enough: Windows keeps the process object around while anything
    holds a handle to it, so a finished process still opens. ``STILL_ACTIVE`` is what
    distinguishes running from merely present.
    """
    import ctypes

    process_query_limited_information = 0x1000
    still_active = 259

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True  # cannot tell; the safe direction
        return bool(code.value == still_active)
    finally:
        kernel32.CloseHandle(handle)
