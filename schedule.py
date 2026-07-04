"""Timer-driven dispatcher of scheduled subagents, ported from pi's schedule.ts.

``SubagentScheduler`` owns per-job asyncio timers and a session-scoped
``ScheduleStore``. When a job fires it spawns a background subagent through the
manager with ``bypass_queue=True`` so a short interval can't be deferred behind
a full concurrency queue. Result delivery is implicit: the spawn goes through
the manager's normal background-completion path, which reuses the existing
``<task-notification>`` follow-up — no new delivery code.

Differences vs pi:
  * Cron is 5-field (see ``cron.py``); pi's croner uses 6 fields.
  * No croner / nanoid dependency — the cron matcher is vendored and job ids
    use the extension's ``<prefix>-<n>`` counter style (``job-1``, ``job-2``).
  * pi emits ``pi.events`` change notifications for cross-extension consumers;
    Tau has no such consumer, so those emits are dropped.
  * Intervals shorter than ``MIN_INTERVAL_MS`` are rejected (Node clamps
    setInterval to ~1ms; Python ``call_later(0)`` is a genuine hot loop).
  * All datetimes are naive local time, unlike timezone-aware croner: across
    a DST transition a fire can land up to an hour off (self-correcting at
    the next occurrence), and a cron target in the spring-forward gap still
    fires at the shifted wall-clock instant.

Schedule formats accepted by ``detect_schedule``:
  * 5-field cron   — recurring, e.g. ``0 9 * * 1`` (09:00 every Monday)
  * interval       — ``10s`` / ``5m`` / ``1h`` / ``2d`` (recurring)
  * relative       — ``+10m`` / ``+2h`` (one-shot, relative to now)
  * ISO timestamp  — ``2026-07-04T09:00:00`` (one-shot; past times rejected)

Lifecycle: created -> (fired -> done/error)* -> cancelled/disabled. One-shot
jobs auto-disable after firing. On restart missed fires are skipped (no
catch-up): recurring jobs arm to their next future fire, and one-shots whose
time has already passed are disabled and marked errored.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from .cron import CronExpression, validate_cron
from .schedule_store import ScheduledSubagent, ScheduleStore

if TYPE_CHECKING:
    from .extension import SubagentManager

_UNIT_MS = {"s": 1000, "m": 60_000, "h": 3_600_000, "d": 86_400_000}
_RELATIVE_RE = re.compile(r"^\+(\d+)(s|m|h|d)$")
_INTERVAL_RE = re.compile(r"^(\d+)(s|m|h|d)$")
_ISO_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T")

_TERMINAL_FAILURE = ("error", "aborted", "cancelled", "stopped")

MIN_INTERVAL_MS = 5_000


def _now_iso() -> str:
    return datetime.now().isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class SubagentScheduler:
    """Arms per-job timers and fires scheduled subagents through the manager."""

    def __init__(self, manager: SubagentManager) -> None:
        self._manager = manager
        self._store: ScheduleStore | None = None
        self._timers: dict[str, asyncio.TimerHandle] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._id_counter = 0

    # -- lifecycle ----------------------------------------------------------

    def start(self, store: ScheduleStore) -> None:
        """Bind a session's store and arm every enabled job."""
        self._store = store
        max_n = 0
        for job in store.list():
            if job.id.startswith("job-"):
                with contextlib.suppress(ValueError):
                    max_n = max(max_n, int(job.id[4:]))
        self._id_counter = max_n
        for job in store.list():
            if job.enabled:
                self._arm(job)

    def stop(self) -> None:
        """Cancel all timers and drop the store. Safe to call repeatedly."""
        for handle in self._timers.values():
            handle.cancel()
        self._timers.clear()
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        self._store = None

    def is_active(self) -> bool:
        return self._store is not None

    def list(self) -> list[ScheduledSubagent]:
        return self._store.list() if self._store is not None else []

    # -- job management -----------------------------------------------------

    def add_job(
        self,
        *,
        name: str,
        description: str,
        schedule: str,
        subagent_type: str,
        prompt: str,
        model: str | None = None,
        thinking: str | None = None,
        max_turns: int | None = None,
        isolation: str | None = None,
    ) -> ScheduledSubagent:
        """Validate, persist, and arm a new job. Returns the stored job."""
        store = self._require_store()
        if store.has_name(name):
            raise ValueError(f'A scheduled job named "{name}" already exists.')
        schedule_type, interval_ms, normalized = self.detect_schedule(schedule)
        job = ScheduledSubagent(
            id=self._next_job_id(),
            name=name,
            description=description,
            schedule=normalized,
            schedule_type=schedule_type,
            interval_ms=interval_ms,
            subagent_type=subagent_type,
            prompt=prompt,
            model=model,
            thinking=thinking,
            max_turns=max_turns,
            isolation=isolation,
            enabled=True,
            created_at=_now_iso(),
            run_count=0,
        )
        store.add(job)
        self._arm(job)
        return job

    def remove_job(self, job_id: str) -> bool:
        store = self._require_store()
        if store.get(job_id) is None:
            return False
        self._disarm(job_id)
        return store.remove(job_id)

    def get_next_run(self, job_id: str) -> str | None:
        """Next-run time as ISO, or None if not currently armed."""
        store = self._store
        if store is None:
            return None
        job = store.get(job_id)
        if job is None or not job.enabled:
            return None
        if job.schedule_type == "once":
            return job.schedule
        if job.schedule_type == "interval" and job.interval_ms:
            # Before the first fire there is no last_run, so fall back to now —
            # accurate at create time and within interval_ms otherwise.
            base = _parse_iso(job.last_run) or datetime.now()
            return (base + timedelta(milliseconds=job.interval_ms)).isoformat()
        if job.schedule_type == "cron":
            nxt = CronExpression(job.schedule).next_after(datetime.now())
            return nxt.isoformat() if nxt else None
        return None

    # -- timers -------------------------------------------------------------

    def _arm(self, job: ScheduledSubagent) -> None:
        store = self._store
        if store is None or not job.enabled:
            return
        loop = asyncio.get_running_loop()
        now = datetime.now()
        if job.schedule_type == "interval":
            if not job.interval_ms or job.interval_ms < MIN_INTERVAL_MS:
                # A corrupt/migrated store entry must not become a hot loop.
                store.update(job.id, enabled=False, last_status="error")
                return
            delay = job.interval_ms / 1000
        elif job.schedule_type == "once":
            target = _parse_iso(job.schedule)
            if target is None:
                store.update(job.id, enabled=False, last_status="error")
                return
            delay = (target - now).total_seconds()
            if delay <= 0:
                # Past one-shot (e.g. missed while offline) — never fire.
                store.update(job.id, enabled=False, last_status="error")
                return
        else:  # cron
            try:
                nxt = CronExpression(job.schedule).next_after(now)
            except ValueError:
                store.update(job.id, enabled=False, last_status="error")
                return
            if nxt is None:
                store.update(job.id, enabled=False, last_status="error")
                return
            delay = (nxt - now).total_seconds()
        store.update(job.id, next_run=self.get_next_run(job.id))
        handle = loop.call_later(max(0.0, delay), self._on_fire, job.id)
        self._timers[job.id] = handle

    def _disarm(self, job_id: str) -> None:
        handle = self._timers.pop(job_id, None)
        if handle is not None:
            handle.cancel()

    def _on_fire(self, job_id: str) -> None:
        """Timer callback (sync): re-arm recurring jobs, then launch execution."""
        self._timers.pop(job_id, None)
        store = self._store
        if store is None:
            return
        job = store.get(job_id)
        if job is None or not job.enabled:
            return
        if job.schedule_type == "once":
            store.update(job_id, enabled=False)
        else:
            self._arm(job)  # re-arm for the next interval / cron occurrence
        task = asyncio.get_running_loop().create_task(self._execute_job(job_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _execute_job(self, job_id: str) -> None:
        store = self._store
        if store is None:
            return
        job = store.get(job_id)
        if job is None:
            return
        store.update(job_id, last_status="running")
        definition = self._manager.definitions().get(job.subagent_type)
        if definition is None:
            store.update(job_id, last_run=_now_iso(), last_status="error")
            return
        try:
            run = self._manager.spawn(
                agent_type=definition,
                prompt=job.prompt,
                description=job.description,
                background=True,
                bypass_queue=True,
                max_turns=job.max_turns,
                isolation=job.isolation,
                model=job.model,
                thinking=job.thinking,
            )
        except Exception:  # noqa: BLE001 - a bad spawn must not kill the scheduler
            store.update(job_id, last_run=_now_iso(), last_status="error")
            return
        task = run.task
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                return  # session shutting down — leave state as-is
            except Exception:  # noqa: BLE001 - failures are read from run.status
                pass
        final = self._manager.runs.get(run.agent_id) or run
        failed = final.status in _TERMINAL_FAILURE
        self._finalize(job_id, "error" if failed else "success")

    def _finalize(self, job_id: str, status: str) -> None:
        store = self._store
        if store is None:
            return
        job = store.get(job_id)
        if job is None:
            return
        store.update(
            job_id,
            last_run=_now_iso(),
            last_status=status,
            run_count=job.run_count + 1,
            next_run=self.get_next_run(job_id),
        )

    # -- helpers ------------------------------------------------------------

    def _next_job_id(self) -> str:
        self._id_counter += 1
        candidate = f"job-{self._id_counter}"
        while self._store is not None and self._store.get(candidate) is not None:
            self._id_counter += 1
            candidate = f"job-{self._id_counter}"
        return candidate

    def _require_store(self) -> ScheduleStore:
        if self._store is None:
            raise RuntimeError("Scheduler not started — no active session.")
        return self._store

    # -- format detection (static, pure) ------------------------------------

    @staticmethod
    def detect_schedule(text: str) -> tuple[str, int | None, str]:
        """Sniff a schedule string. Returns (type, interval_ms, normalized).

        Raises ValueError on invalid input. Order matters: relative (``+10m``)
        and interval (``5m``) both match digit+unit, so relative requires the
        leading ``+`` to disambiguate.
        """
        trimmed = text.strip()
        relative = SubagentScheduler.parse_relative_time(trimmed)
        if relative is not None:
            return ("once", None, relative)
        interval = SubagentScheduler.parse_interval(trimmed)
        if interval is not None:
            if interval < MIN_INTERVAL_MS:
                raise ValueError(
                    f'Interval "{trimmed}" is too short — the minimum is'
                    f" {MIN_INTERVAL_MS // 1000}s (a zero interval would spawn"
                    " agents in a tight loop)."
                )
            return ("interval", interval, trimmed)
        if _ISO_PREFIX_RE.match(trimmed):
            target = _parse_iso(trimmed)
            if target is not None:
                if target <= datetime.now():
                    raise ValueError(
                        f"Scheduled time {target.isoformat()} is in the past."
                    )
                return ("once", None, target.isoformat())
        if validate_cron(trimmed):
            return ("cron", None, trimmed)
        raise ValueError(
            f'Invalid schedule "{text}". Use 5-field cron (e.g. "0 9 * * 1" — '
            '9am every Monday), interval ("5m"/"1h"), or one-shot ("+10m" / ISO).'
        )

    @staticmethod
    def parse_relative_time(text: str) -> str | None:
        """``+10s``/``+5m``/``+1h``/``+2d`` -> ISO timestamp."""
        match = _RELATIVE_RE.match(text)
        if not match:
            return None
        ms = int(match.group(1)) * _UNIT_MS[match.group(2)]
        return (datetime.now() + timedelta(milliseconds=ms)).isoformat()

    @staticmethod
    def parse_interval(text: str) -> int | None:
        """``10s``/``5m``/``1h``/``2d`` -> milliseconds."""
        match = _INTERVAL_RE.match(text)
        if not match:
            return None
        return int(match.group(1)) * _UNIT_MS[match.group(2)]
