"""Interactive /agents menu, ported from pi-subagents' showAgentsMenu.

Driven by Tau's extension UI dialogs (`ui-dialogs` seam: async select /
confirm / input on `tau.context.ui`). Selecting a run opens its conversation
in the extension's own conversation viewer via the component seam (the
`SubagentUiController`); component-less hosts go straight to the action submenu
(steer / stop / view result). pi's create wizard and settings entries are
not ported yet.

Navigation matches pi: submenus loop back to their parent, escape backs out.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from tau_agent.messages import AgentMessage, AssistantMessage, UserMessage

if TYPE_CHECKING:
    from .extension import AgentRun, SubagentManager
    from .schedule import SubagentScheduler
    from .schedule_store import ScheduledSubagent
    from .ui import SubagentUiController

RESULT_PREVIEW_CHARS = 600
ACTIVE_STATUSES = ("running", "queued")


class DialogUi(Protocol):
    """The slice of tau's ExtensionUi the menu needs."""

    async def select(
        self, title: str, options: list[str], *, timeout: float | None = None
    ) -> str | None: ...

    async def confirm(
        self, title: str, message: str, *, timeout: float | None = None
    ) -> bool: ...

    async def input(
        self, title: str, placeholder: str = "", *, timeout: float | None = None
    ) -> str | None: ...

    def notify(self, message: str, level: str = "info") -> None: ...


def supports_menu(ui: object) -> bool:
    """True when the bound UI can drive the interactive menu.

    ``has_ui`` is a runtime signal, not feature detection: print mode's
    NullUiBridge reports False, and /agents falls back to the plain-text list.
    """
    return ui is not None and bool(getattr(ui, "has_ui", False))


async def show_agents_menu(
    manager: SubagentManager,
    ui: DialogUi,
    scheduler: SubagentScheduler | None = None,
    *,
    controller: SubagentUiController | None = None,
) -> None:
    """Top-level menu (pi's showAgentsMenu): runs, agent types, scheduled jobs."""
    while True:
        runs = list(manager.runs.values())
        definitions = manager.definitions()
        options: list[str] = []
        if runs:
            running = sum(1 for run in runs if run.status in ACTIVE_STATUSES)
            done = sum(1 for run in runs if run.status in ("completed", "steered"))
            options.append(
                f"Running agents ({len(runs)}) — {running} running, {done} done"
            )
        options.append(f"Agent types ({len(definitions)})")
        scheduler_active = scheduler is not None and scheduler.is_active()
        if scheduler_active:
            options.append(f"Scheduled jobs ({len(scheduler.list())})")
        choice = await ui.select("Agents", options)
        if choice is None:
            return
        if choice.startswith("Running agents ("):
            if await show_running_agents(manager, ui, controller=controller):
                return
        elif choice.startswith("Agent types ("):
            await show_agent_types(manager, ui)
        elif scheduler_active and choice.startswith("Scheduled jobs ("):
            await show_schedules_menu(scheduler, ui)
        else:
            return


async def show_running_agents(
    manager: SubagentManager,
    ui: DialogUi,
    *,
    controller: SubagentUiController | None = None,
) -> bool:
    """Run list (pi's showRunningAgents); True when the whole menu should close."""
    while True:
        runs = list(manager.runs.values())
        if not runs:
            ui.notify("No agents.", "info")
            return False
        options = [
            f"{run.agent_type} ({run.description}) · {run.tool_calls} tools"
            f" · {run.status} · {_format_duration(run)}"
            for run in runs
        ]
        choice = await ui.select("Running agents", options)
        if choice is None:
            return False
        index = options.index(choice)
        run = runs[index]
        outcome = view_run_conversation(run, controller)
        if outcome == "exit":
            return True
        if outcome == "actions":
            await show_run_actions(ui, run)


def view_run_conversation(
    run: AgentRun, controller: SubagentUiController | None
) -> str:
    """Open the run's conversation via the component seam; "exit" or "actions".

    With a component-capable host the extension opens its own conversation
    viewer (the widget seam that replaced tau's removed ``view_transcript``):
    on success the whole menu closes ("exit") so the user lands in the view.
    Component-less hosts (print mode) fall to the action submenu
    ("actions"), exactly as the old ``view_transcript``-missing branch did.
    """
    if controller is not None:
        try:
            if controller.open_conversation(run):
                return "exit"
        except Exception:  # noqa: BLE001 - degrade to the action submenu
            pass
    return "actions"


def run_snapshot_messages(run: AgentRun) -> tuple[AgentMessage, ...]:
    """Reconstruct a minimal transcript for a run whose session is gone."""
    body = run.error if run.status == "error" else run.result_text
    return (
        UserMessage(content=run.prompt),
        AssistantMessage(content=body or "(no output)"),
    )


async def show_run_actions(ui: DialogUi, run: AgentRun) -> None:
    """Action submenu for one run: steer / stop while active, else view result."""
    active = run.status in ACTIVE_STATUSES
    options: list[str] = []
    if not active:
        options.append("View result")
    else:
        options.extend(("Steer…", "Stop"))
    options.append("Back")
    choice = await ui.select(
        f"{run.agent_id} [{run.status}] {run.description}", options
    )
    if choice == "View result":
        body = run.error if run.status == "error" else run.result_text
        preview = (body or "(no output)")[:RESULT_PREVIEW_CHARS]
        ui.notify(
            f"{run.agent_id} [{run.status}]: {preview}\n"
            "(use get_subagent_result for full output)",
            "info",
        )
    elif choice == "Steer…":
        message = await ui.input("Steer agent", "New instruction for the agent")
        if message:
            steer_run(run, message)
            ui.notify(f"Steering message sent to {run.agent_id}.", "info")
    elif choice == "Stop":
        if await ui.confirm("Stop agent", f'Stop "{run.description}"?'):
            stop_run(run)
            ui.notify(f'Stopped "{run.description}".', "info")


async def show_agent_types(manager: SubagentManager, ui: DialogUi) -> None:
    """Agent-type list; selecting a type shows its details."""
    while True:
        definitions = manager.definitions()
        names = sorted(definitions)
        choice = await ui.select("Agent types", names)
        if choice is None or choice not in definitions:
            return
        definition = definitions[choice]
        lines = [f"{definition.name}: {definition.description}"]
        if definition.model:
            lines.append(f"model: {definition.model}")
        if definition.tools is not None:
            lines.append(f"tools: {', '.join(definition.tools)}")
        if definition.max_turns is not None:
            lines.append(f"max_turns: {definition.max_turns}")
        ui.notify("\n".join(lines), "info")


async def show_schedules_menu(scheduler: SubagentScheduler, ui: DialogUi) -> None:
    """List scheduled jobs; selecting one opens a cancel-confirm (pi's port)."""
    jobs = scheduler.list()
    if not jobs:
        ui.notify("No scheduled jobs.", "info")
        return
    labels = [_format_job(job, scheduler) for job in jobs]
    choice = await ui.select(
        f"Scheduled jobs ({len(jobs)}) — select to cancel", labels
    )
    if choice is None:
        return
    index = labels.index(choice)
    job = jobs[index]
    if await ui.confirm(f'Cancel "{job.name}"?', _format_job_details(job, scheduler)):
        scheduler.remove_job(job.id)
        ui.notify(f'Cancelled "{job.name}".', "info")


def _status_icon(job: ScheduledSubagent) -> str:
    if not job.enabled:
        return "x"
    if job.last_status == "error":
        return "!"
    if job.last_status == "running":
        return "~"
    return "ok"


def _format_job(job: ScheduledSubagent, scheduler: SubagentScheduler) -> str:
    return (
        f"{_status_icon(job)}  {job.name[:18]:<18}  {job.schedule[:14]:<14}"
        f"  [{job.subagent_type}]  next {_rel_time(scheduler.get_next_run(job.id))}"
        f"  last {_rel_time(job.last_run)}  runs {job.run_count}"
    )


def _format_job_details(job: ScheduledSubagent, scheduler: SubagentScheduler) -> str:
    prompt = job.prompt[:200] + ("..." if len(job.prompt) > 200 else "")
    return "\n".join(
        [
            f"name:      {job.name}",
            f"schedule:  {job.schedule} ({job.schedule_type})",
            f"agent:     {job.subagent_type}",
            f"prompt:    {prompt}",
            f"created:   {job.created_at}",
            f"last run:  {job.last_run or '-'} ({job.last_status or '-'})",
            f"next run:  {scheduler.get_next_run(job.id) or '-'}",
            f"runs:      {job.run_count}",
        ]
    )


def _rel_time(iso: str | None, now: datetime | None = None) -> str:
    """Format an ISO timestamp as relative time ("in 4h", "2d ago", "-")."""
    if not iso:
        return "-"
    try:
        moment = datetime.fromisoformat(iso)
    except ValueError:
        return "-"
    now = now or datetime.now()
    diff = (moment - now).total_seconds()
    future = diff > 0
    seconds = abs(diff)
    if seconds < 60:
        return "in <1m" if future else "<1m ago"
    if seconds < 3600:
        value = round(seconds / 60)
        return f"in {value}m" if future else f"{value}m ago"
    if seconds < 86400:
        value = round(seconds / 3600)
        return f"in {value}h" if future else f"{value}h ago"
    value = round(seconds / 86400)
    return f"in {value}d" if future else f"{value}d ago"


def steer_run(run: AgentRun, message: str) -> None:
    """Queue a steering message, mirroring the steer_subagent tool."""
    if run.session is None:
        run.pending_steers.append(message)
    else:
        run.session.queue_steering_message(message)


def stop_run(run: AgentRun) -> None:
    """Stop a queued or running agent, mirroring the turn-limit hard abort."""
    if run.status == "queued":
        run.status = "cancelled"
        run.completed_at = time.monotonic()
        return
    run.aborted = True
    if run.session is not None:
        run.session.cancel()


def _format_duration(run: AgentRun) -> str:
    if run.started_at is None:
        return "queued"
    end = run.completed_at if run.completed_at is not None else time.monotonic()
    return f"{max(0, end - run.started_at):.0f}s"
