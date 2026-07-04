"""Interactive /agents menu, ported from pi-subagents' showAgentsMenu.

Driven by Tau's extension UI dialogs (`ui-dialogs` seam: async select /
confirm / input on `tau.context.ui`). pi's menu also offers a create wizard,
settings, scheduled jobs, and a full conversation-viewer overlay
(`ctx.ui.custom`); Tau v1 has dialogs only, so runs get a select-driven
action submenu (view result / steer / stop) instead of the overlay, and the
wizard/settings entries are not ported yet.

Navigation matches pi: submenus loop back to their parent, escape backs out.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .extension import AgentRun, SubagentManager

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
    """True when the bound UI can drive the interactive menu."""
    return (
        ui is not None
        and bool(getattr(ui, "has_ui", False))
        and hasattr(ui, "select")
    )


async def show_agents_menu(manager: SubagentManager, ui: DialogUi) -> None:
    """Top-level menu (pi's showAgentsMenu): runs, then agent types."""
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
        choice = await ui.select("Agents", options)
        if choice is None:
            return
        if choice.startswith("Running agents ("):
            await show_running_agents(manager, ui)
        elif choice.startswith("Agent types ("):
            await show_agent_types(manager, ui)
        else:
            return


async def show_running_agents(manager: SubagentManager, ui: DialogUi) -> None:
    """Run list (pi's showRunningAgents), looping back after each action."""
    while True:
        runs = list(manager.runs.values())
        if not runs:
            ui.notify("No agents.", "info")
            return
        options = [
            f"{run.agent_type} ({run.description}) · {run.tool_calls} tools"
            f" · {run.status} · {_format_duration(run)}"
            for run in runs
        ]
        choice = await ui.select("Running agents", options)
        if choice is None:
            return
        index = options.index(choice)
        await show_run_actions(ui, runs[index])


async def show_run_actions(ui: DialogUi, run: AgentRun) -> None:
    """Dialog-based stand-in for pi's conversation-viewer overlay."""
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
