"""File-backed store for scheduled subagents, ported from pi's schedule-store.ts.

Session-scoped: each Tau session owns its schedules at
``<cwd>/.tau/subagent-schedules/<session_id>.json``. pi stores these under the
project ``.pi`` directory; the Tau equivalent of that project dir is ``.tau``
(the same directory ``settings.py`` reads ``subagents.json`` from). ``/new``
starts a fresh empty store; ``/resume`` reloads the same session file.

The concurrency model mirrors pi: every mutation acquires a PID-based exclusion
lock, re-reads the latest state from disk, applies the change, atomic-writes via
temp file + rename, then releases the lock. A lock whose owning PID is no longer
alive is treated as stale and taken over.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

STORE_VERSION = 1
_LOCK_RETRY_SECONDS = 0.05
_LOCK_MAX_RETRIES = 100


@dataclass(frozen=True, slots=True)
class ScheduledSubagent:
    """A subagent spawn registered to fire on a schedule (snake_case of pi)."""

    id: str
    name: str
    description: str
    schedule: str
    schedule_type: str  # "cron" | "once" | "interval"
    subagent_type: str
    prompt: str
    enabled: bool
    created_at: str
    run_count: int = 0
    interval_ms: int | None = None
    model: str | None = None
    thinking: str | None = None
    max_turns: int | None = None
    isolation: str | None = None
    last_run: str | None = None
    last_status: str | None = None  # "success" | "error" | "running"
    next_run: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> ScheduledSubagent:
        fields = {
            "id",
            "name",
            "description",
            "schedule",
            "schedule_type",
            "subagent_type",
            "prompt",
            "enabled",
            "created_at",
            "run_count",
            "interval_ms",
            "model",
            "thinking",
            "max_turns",
            "isolation",
            "last_run",
            "last_status",
            "next_run",
        }
        return cls(**{key: value for key, value in data.items() if key in fields})


def resolve_store_path(cwd: Path, session_id: str) -> Path:
    """Storage path for a session-scoped schedule store."""
    return cwd / ".tau" / "subagent-schedules" / f"{session_id}.json"


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    except OSError:
        return False
    return True


def _acquire_lock(lock_path: Path) -> None:
    for _ in range(_LOCK_MAX_RETRIES):
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                pid = int(lock_path.read_text(encoding="utf-8").strip() or "0")
            except (OSError, ValueError):
                pid = 0
            if pid and not _process_alive(pid):
                # Stale lock — the owning process is gone. Take it over.
                with contextlib.suppress(OSError):
                    lock_path.unlink()
                continue
            time.sleep(_LOCK_RETRY_SECONDS)
            continue
        else:
            with os.fdopen(fd, "w") as handle:
                handle.write(str(os.getpid()))
            return
    raise RuntimeError(f"Failed to acquire schedule lock: {lock_path}")


def _release_lock(lock_path: Path) -> None:
    with contextlib.suppress(OSError):
        lock_path.unlink()


class ScheduleStore:
    """Session-scoped, PID-locked, atomic JSON store of scheduled subagents."""

    def __init__(self, file_path: Path) -> None:
        self._file_path = file_path
        self._lock_path = file_path.with_name(file_path.name + ".lock")
        self._jobs: dict[str, ScheduledSubagent] = {}
        self._load()

    def _ensure_dir(self) -> None:
        self._file_path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> None:
        if not self._file_path.exists():
            return
        try:
            data = json.loads(self._file_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return  # corrupt — start fresh; the next save rewrites it
        self._jobs.clear()
        for raw in data.get("jobs", []) if isinstance(data, dict) else []:
            if isinstance(raw, dict):
                try:
                    job = ScheduledSubagent.from_dict(raw)
                except TypeError:
                    continue
                self._jobs[job.id] = job

    def _save(self) -> None:
        payload = {
            "version": STORE_VERSION,
            "jobs": [job.to_dict() for job in self._jobs.values()],
        }
        tmp = self._file_path.with_name(self._file_path.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self._file_path)

    def _with_lock(self, mutate):  # noqa: ANN001, ANN202
        self._ensure_dir()
        _acquire_lock(self._lock_path)
        try:
            self._load()
            result = mutate()
            self._save()
            return result
        finally:
            _release_lock(self._lock_path)

    def list(self) -> list[ScheduledSubagent]:
        return list(self._jobs.values())

    def get(self, job_id: str) -> ScheduledSubagent | None:
        return self._jobs.get(job_id)

    def has_name(self, name: str, except_id: str | None = None) -> bool:
        return any(
            job.id != except_id and job.name == name for job in self._jobs.values()
        )

    def add(self, job: ScheduledSubagent) -> None:
        def mutate() -> None:
            self._jobs[job.id] = job

        self._with_lock(mutate)

    def update(self, job_id: str, **patch: object) -> ScheduledSubagent | None:
        if job_id not in self._jobs:
            return None  # no-op fast path — don't lock or touch disk

        def mutate() -> ScheduledSubagent | None:
            existing = self._jobs.get(job_id)
            if existing is None:
                return None
            updated = replace(existing, **patch)
            self._jobs[job_id] = updated
            return updated

        return self._with_lock(mutate)

    def remove(self, job_id: str) -> bool:
        if job_id not in self._jobs:
            return False

        def mutate() -> bool:
            return self._jobs.pop(job_id, None) is not None

        return self._with_lock(mutate)
