"""Tests for the subagent scheduler, cron matcher, and schedule store.

Run from a Tau checkout, e.g.:

    uv run --project /path/to/tau pytest tests/test_schedule.py
"""

import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from tau_ai import FakeProvider

# Reuse the runtime/provider fakes and helpers from the main test module.
from test_extension import (
    BlockingProvider,
    RecordingSession,
    ScriptedUi,
    _agent_tool,
    _extension_module,
    _load_runtime,
    _patch_provider_sequence,
    _submodule,
    _text_stream,
    _wait_for,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _cron_module():  # noqa: ANN202
    return _submodule("cron")


def _schedule_module():  # noqa: ANN202
    return _submodule("schedule")


def _store_module():  # noqa: ANN202
    return _submodule("schedule_store")


def _make_job(store_module, **overrides):  # noqa: ANN001, ANN202
    defaults = {
        "id": "job-1",
        "name": "nightly",
        "description": "nightly",
        "schedule": "0 9 * * 1",
        "schedule_type": "cron",
        "subagent_type": "general",
        "prompt": "do the thing",
        "enabled": True,
        "created_at": datetime.now().isoformat(),
        "run_count": 0,
    }
    defaults.update(overrides)
    return store_module.ScheduledSubagent(**defaults)


# ── cron matcher ─────────────────────────────────────────────────────────


def test_cron_star_matches_every_minute(tmp_path) -> None:  # noqa: ANN001
    _load_runtime(tmp_path)
    cron = _cron_module()
    expr = cron.CronExpression("* * * * *")
    assert expr.matches(datetime(2026, 7, 4, 13, 37))


def test_cron_specific_minute_hour(tmp_path) -> None:  # noqa: ANN001
    _load_runtime(tmp_path)
    cron = _cron_module()
    expr = cron.CronExpression("30 14 * * *")
    assert expr.matches(datetime(2026, 7, 4, 14, 30))
    assert not expr.matches(datetime(2026, 7, 4, 14, 31))
    assert not expr.matches(datetime(2026, 7, 4, 15, 30))


def test_cron_step_and_range_and_list(tmp_path) -> None:  # noqa: ANN001
    _load_runtime(tmp_path)
    cron = _cron_module()
    step = cron.CronExpression("*/15 * * * *")
    assert {m for m in range(60) if step.matches(datetime(2026, 1, 1, 0, m))} == {
        0,
        15,
        30,
        45,
    }
    rng = cron.CronExpression("0 9-11 * * *")
    assert rng.matches(datetime(2026, 1, 1, 10, 0))
    assert not rng.matches(datetime(2026, 1, 1, 12, 0))
    lst = cron.CronExpression("0,20,40 * * * *")
    assert lst.matches(datetime(2026, 1, 1, 5, 20))
    assert not lst.matches(datetime(2026, 1, 1, 5, 21))
    ranged_step = cron.CronExpression("0-30/10 * * * *")
    assert {m for m in range(60) if ranged_step.matches(datetime(2026, 1, 1, 0, m))} == {
        0,
        10,
        20,
        30,
    }


def test_cron_day_of_week(tmp_path) -> None:  # noqa: ANN001
    _load_runtime(tmp_path)
    cron = _cron_module()
    # 2026-07-06 is a Monday. dow 1 = Monday.
    monday = cron.CronExpression("0 0 * * 1")
    assert monday.matches(datetime(2026, 7, 6, 0, 0))
    assert not monday.matches(datetime(2026, 7, 7, 0, 0))
    # Sunday accepts both 0 and 7.
    sunday_zero = cron.CronExpression("0 0 * * 0")
    sunday_seven = cron.CronExpression("0 0 * * 7")
    assert sunday_zero.matches(datetime(2026, 7, 5, 0, 0))
    assert sunday_seven.matches(datetime(2026, 7, 5, 0, 0))


def test_cron_dom_dow_or_semantics(tmp_path) -> None:  # noqa: ANN001
    _load_runtime(tmp_path)
    cron = _cron_module()
    # Both restricted: matches on the 1st OR on Mondays.
    expr = cron.CronExpression("0 0 1 * 1")
    assert expr.matches(datetime(2026, 7, 1, 0, 0))  # 1st (a Wednesday)
    assert expr.matches(datetime(2026, 7, 6, 0, 0))  # a Monday
    assert not expr.matches(datetime(2026, 7, 7, 0, 0))  # neither


def test_cron_next_after(tmp_path) -> None:  # noqa: ANN001
    _load_runtime(tmp_path)
    cron = _cron_module()
    expr = cron.CronExpression("0 9 * * 1")  # 09:00 every Monday
    nxt = expr.next_after(datetime(2026, 7, 4, 12, 0))  # Sat
    assert nxt == datetime(2026, 7, 6, 9, 0)  # following Monday
    # Strictly after: a matching instant returns the next occurrence.
    after_match = expr.next_after(datetime(2026, 7, 6, 9, 0))
    assert after_match == datetime(2026, 7, 13, 9, 0)


def test_cron_validation(tmp_path) -> None:  # noqa: ANN001
    _load_runtime(tmp_path)
    cron = _cron_module()
    assert cron.validate_cron("*/5 * * * *")
    assert not cron.validate_cron("* * * *")  # 4 fields
    assert not cron.validate_cron("* * * * * *")  # 6 fields
    assert not cron.validate_cron("99 * * * *")  # out of range
    assert not cron.validate_cron("abc * * * *")


# ── detect_schedule ──────────────────────────────────────────────────────


def test_detect_schedule_forms(tmp_path) -> None:  # noqa: ANN001
    _load_runtime(tmp_path)
    sched = _schedule_module().SubagentScheduler
    assert sched.detect_schedule("5m")[0] == "interval"
    assert sched.detect_schedule("5m")[1] == 300_000
    assert sched.detect_schedule("+10m")[0] == "once"
    assert sched.detect_schedule("0 9 * * 1")[0] == "cron"
    future = (datetime.now() + timedelta(days=1)).replace(microsecond=0).isoformat()
    assert sched.detect_schedule(future)[0] == "once"
    with pytest.raises(ValueError, match="in the past"):
        sched.detect_schedule("2000-01-01T00:00:00")
    with pytest.raises(ValueError, match="Invalid schedule"):
        sched.detect_schedule("not a schedule")
    # Sub-minimum intervals are rejected: a zero interval would re-arm with
    # delay 0 and spawn agents in a tight loop.
    with pytest.raises(ValueError, match="too short"):
        sched.detect_schedule("0s")
    with pytest.raises(ValueError, match="too short"):
        sched.detect_schedule("4s")
    assert sched.detect_schedule("5s")[0] == "interval"


# ── store round-trip + stale lock ────────────────────────────────────────


def test_store_round_trip(tmp_path) -> None:  # noqa: ANN001
    _load_runtime(tmp_path)
    store_module = _store_module()
    path = store_module.resolve_store_path(tmp_path, "session-x")
    store = store_module.ScheduleStore(path)
    job = _make_job(store_module, model="fake", max_turns=7)
    store.add(job)
    assert path.exists()

    reloaded = store_module.ScheduleStore(path)
    got = reloaded.get("job-1")
    assert got is not None
    assert got.model == "fake"
    assert got.max_turns == 7
    assert got.schedule == "0 9 * * 1"

    updated = reloaded.update("job-1", run_count=3, last_status="success")
    assert updated is not None and updated.run_count == 3
    assert reloaded.remove("job-1") is True
    assert reloaded.get("job-1") is None
    assert reloaded.update("missing", run_count=1) is None


def test_store_takes_over_stale_lock(tmp_path) -> None:  # noqa: ANN001
    _load_runtime(tmp_path)
    store_module = _store_module()
    path = store_module.resolve_store_path(tmp_path, "session-y")
    store = store_module.ScheduleStore(path)
    store.add(_make_job(store_module))

    lock_path = path.with_name(path.name + ".lock")
    lock_path.write_text("999999")  # PID that is not alive → stale
    assert lock_path.exists()

    updated = store.update("job-1", run_count=9)
    assert updated is not None and updated.run_count == 9
    assert not lock_path.exists()  # stale lock taken over and released


# ── firing: bypasses a full concurrency queue ────────────────────────────


class _FakeApi:
    """Minimal ExtensionAPI stand-in for driving SubagentManager directly."""

    def __init__(self, cwd, session_id="session-1") -> None:  # noqa: ANN001
        self.context = SimpleNamespace(cwd=cwd, session_id=session_id)
        self.followed_up: list[str] = []
        self.notifications: list[str] = []
        self.custom_entries: list[tuple[str, dict]] = []

    def notify(self, message: str, level: str = "info") -> None:
        self.notifications.append(message)

    def send_user_message(self, content: str, *, deliver_as: str = "follow_up") -> None:
        self.followed_up.append(content)

    async def append_entry(self, namespace: str, data: dict) -> None:  # noqa: ANN001
        self.custom_entries.append((namespace, data))


async def test_job_fires_bypassing_full_queue(tmp_path) -> None:  # noqa: ANN001
    _load_runtime(tmp_path)
    module = _extension_module()
    store_module = _store_module()
    module.load_subagent_settings = (  # type: ignore[attr-defined]
        lambda cwd, home=None: module.SubagentSettings(
            max_concurrent=1, default_join_mode="async"
        )
    )
    release = asyncio.Event()
    _patch_provider_sequence(
        module,
        [
            BlockingProvider(release, "blocker running"),
            FakeProvider([_text_stream("scheduled work done")]),
        ],
    )

    api = _FakeApi(tmp_path)
    manager = module.SubagentManager(api)
    scheduler = _schedule_module().SubagentScheduler(manager)
    store = store_module.ScheduleStore(
        store_module.resolve_store_path(tmp_path, "session-1")
    )
    scheduler.start(store)

    definition = manager.definitions()["general"]
    # Fill the single concurrency slot with a blocked background run.
    manager.spawn(
        agent_type=definition,
        prompt="block",
        description="blocker",
        background=True,
    )
    await _wait_for(lambda: manager._running_background == 1)  # noqa: SLF001

    job = scheduler.add_job(
        name="scheduled",
        description="scheduled",
        schedule="10s",
        subagent_type="general",
        prompt="do work",
    )
    # Fire via the real timer callback path, then await the launched run.
    scheduler._on_fire(job.id)  # noqa: SLF001
    await _wait_for(lambda: bool(scheduler._tasks))  # noqa: SLF001
    await asyncio.gather(*list(scheduler._tasks))  # noqa: SLF001

    # The scheduled agent (agent-2) ran to completion even though the only slot
    # was occupied by the still-blocked agent-1 — proof it bypassed the queue.
    assert manager.runs["agent-1"].status == "running"
    assert manager.runs["agent-2"].status == "completed"
    finished = store.get(job.id)
    assert finished is not None
    assert finished.last_status == "success"
    assert finished.run_count == 1

    release.set()
    scheduler.stop()
    await manager.shutdown()


async def test_add_and_cancel_job(tmp_path) -> None:  # noqa: ANN001
    _load_runtime(tmp_path)
    module = _extension_module()
    store_module = _store_module()
    api = _FakeApi(tmp_path)
    manager = module.SubagentManager(api)
    scheduler = _schedule_module().SubagentScheduler(manager)
    store = store_module.ScheduleStore(
        store_module.resolve_store_path(tmp_path, "session-c")
    )
    scheduler.start(store)

    job = scheduler.add_job(
        name="job a",
        description="job a",
        schedule="1h",
        subagent_type="general",
        prompt="p",
    )
    assert len(scheduler.list()) == 1
    assert job.id in scheduler._timers  # noqa: SLF001 - armed
    assert scheduler.remove_job(job.id) is True
    assert scheduler.list() == []
    assert job.id not in scheduler._timers  # noqa: SLF001 - disarmed
    assert scheduler.remove_job("missing") is False
    scheduler.stop()


async def test_past_oneshot_is_disabled_on_arm(tmp_path) -> None:  # noqa: ANN001
    _load_runtime(tmp_path)
    module = _extension_module()
    store_module = _store_module()
    api = _FakeApi(tmp_path)
    manager = module.SubagentManager(api)
    scheduler = _schedule_module().SubagentScheduler(manager)
    store = store_module.ScheduleStore(
        store_module.resolve_store_path(tmp_path, "session-p")
    )
    # A one-shot whose time has already passed (e.g. missed while offline).
    past = (datetime.now() - timedelta(hours=1)).isoformat()
    store.add(
        _make_job(
            store_module,
            id="job-1",
            schedule=past,
            schedule_type="once",
        )
    )
    scheduler.start(store)
    reloaded = store.get("job-1")
    assert reloaded is not None
    assert reloaded.enabled is False
    assert reloaded.last_status == "error"
    scheduler.stop()


# ── agent-tool guards ────────────────────────────────────────────────────


async def test_schedule_resume_guard(tmp_path) -> None:  # noqa: ANN001
    runtime = _load_runtime(tmp_path)
    runtime.bind(RecordingSession(tmp_path))
    result = await _agent_tool(runtime).execute(
        "call-1",
        {"prompt": "p", "description": "d", "schedule": "5m", "resume": "agent-1"}
    )
    assert "Cannot combine `schedule` with `resume`" in result.text


async def test_schedule_inherit_context_guard(tmp_path) -> None:  # noqa: ANN001
    runtime = _load_runtime(tmp_path)
    runtime.bind(RecordingSession(tmp_path))
    result = await _agent_tool(runtime).execute(
        "call-1",
        {
            "prompt": "p",
            "description": "d",
            "schedule": "5m",
            "inherit_context": True,
        }
    )
    assert "Cannot combine `schedule` with `inherit_context`" in result.text


async def test_schedule_foreground_guard(tmp_path) -> None:  # noqa: ANN001
    runtime = _load_runtime(tmp_path)
    runtime.bind(RecordingSession(tmp_path))
    result = await _agent_tool(runtime).execute(
        "call-1",
        {
            "prompt": "p",
            "description": "d",
            "schedule": "5m",
            "run_in_background": False,
        }
    )
    assert "run_in_background: false" in result.text


async def test_schedule_via_tool_creates_and_persists_job(tmp_path) -> None:  # noqa: ANN001
    runtime = _load_runtime(tmp_path)
    session = RecordingSession(tmp_path)
    runtime.bind(session)
    await runtime.emit_session_start("startup")

    result = await _agent_tool(runtime).execute(
        "call-1",
        {"prompt": "check the deploy", "description": "deploy watch", "schedule": "5m"}
    )
    assert "Scheduled" in result.text
    assert "job-1" in result.text

    store_module = _store_module()
    path = store_module.resolve_store_path(tmp_path, "session-1")
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["jobs"][0]["name"] == "deploy watch"
    assert data["jobs"][0]["schedule_type"] == "interval"

    await runtime.emit_session_shutdown("quit")


# ── /agents menu: list + cancel ──────────────────────────────────────────


class _FakeScheduler:
    def __init__(self, jobs) -> None:  # noqa: ANN001
        self._jobs = jobs
        self.removed: list[str] = []

    def is_active(self) -> bool:
        return True

    def list(self):  # noqa: ANN202
        return self._jobs

    def get_next_run(self, job_id: str):  # noqa: ANN202
        return (datetime.now() + timedelta(minutes=5)).isoformat()

    def remove_job(self, job_id: str) -> bool:
        self.removed.append(job_id)
        return True


async def test_menu_lists_and_cancels_scheduled_job(tmp_path) -> None:  # noqa: ANN001
    _load_runtime(tmp_path)
    menu = _submodule("agents_menu")
    store_module = _store_module()
    job = _make_job(store_module, id="job-1", name="deploy watch", schedule="5m")
    scheduler = _FakeScheduler([job])
    manager = SimpleNamespace(runs={}, definitions=dict)
    ui = ScriptedUi(
        selects=[
            lambda options: next(o for o in options if o.startswith("Scheduled jobs")),
            lambda options: options[0],  # the only job row
            None,  # leave top menu after the cancel returns
        ],
        confirms=[True],
    )

    await menu.show_agents_menu(manager, ui, scheduler)

    assert scheduler.removed == ["job-1"]
    assert any("Cancelled" in note for note in ui.notifications)
    # The scheduled-jobs entry is only offered while the scheduler is active.
    titles = [options for _title, options in ui.select_calls]
    assert any(
        any(opt.startswith("Scheduled jobs (1)") for opt in opts) for opts in titles
    )
