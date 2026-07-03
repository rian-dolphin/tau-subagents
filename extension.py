"""Subagents extension for Tau, ported from tintinweb/pi-subagents.

Registers an `agent` tool that spawns autonomous subagents in-process (a
scoped `CodingSession` with its own tools and system prompt), a
`get_subagent_result` tool for background runs, a `steer_subagent` tool for
redirecting live runs, and an `/agents` command.

Foreground agents block and return their final assistant text. Background
agents return an id immediately and deliver a `<task-notification>` back into
the parent conversation when they finish. Only background agents count toward
the `maxConcurrent` limit; excess background spawns are queued FIFO and started
as slots free up.

Install by copying this directory into `~/.tau/extensions/subagents/`, or run:

    tau -x examples/extensions/subagents
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from tau_agent.session import SessionEntry
from tau_agent.tools import AgentTool, AgentToolResult
from tau_coding import (
    CodingSession,
    CodingSessionConfig,
    load_provider_settings,
    resolve_provider_selection,
)
from tau_coding.extensions import ExtensionAPI, SessionShutdownEvent
from tau_coding.provider_runtime import create_model_provider
from tau_coding.thinking import DEFAULT_THINKING_LEVEL, THINKING_LEVELS

from .agents import AgentDefinition, format_agent_type_list, load_agent_definitions
from .group_join import DEFAULT_TIMEOUT, STRAGGLER_TIMEOUT, GroupJoinManager
from .memory import prepare_memory
from .output_file import OutputFileWriter, output_file_path
from .prompts import (
    build_child_system_prompt,
    detect_environment,
    inherited_resource_paths,
    resolve_skill_blocks,
)
from .settings import SubagentSettings, load_subagent_settings
from .worktree import (
    WORKTREE_ERROR_MESSAGE,
    Worktree,
    cleanup_worktree,
    create_worktree,
    prune_worktrees,
)

if TYPE_CHECKING:
    from tau_agent.events import AgentEvent

INDIVIDUAL_RESULT_CHARS = 500
GROUP_RESULT_CHARS = 300
RECORD_RESULT_CHARS = 4_000
TRUNCATION_SUFFIX = "\n...(truncated, use get_subagent_result for full output)"
BATCH_DEBOUNCE_SECONDS = 0.1
GROUP_TIMEOUT_SECONDS = DEFAULT_TIMEOUT
STRAGGLER_TIMEOUT_SECONDS = STRAGGLER_TIMEOUT
STALE_AFTER_SECONDS = 600.0
TERMINAL_STATUSES = ("completed", "steered", "aborted", "error", "cancelled")
SOFT_LIMIT_MESSAGE = (
    "You have reached your turn limit. Wrap up immediately — provide your"
    " final answer now."
)


class _MemoryStorage:
    """Append-only in-memory storage for subagent sessions."""

    def __init__(self) -> None:
        self.entries: list[SessionEntry] = []

    async def append(self, entry: SessionEntry) -> None:
        self.entries.append(entry)

    async def read_all(self) -> list[SessionEntry]:
        return list(self.entries)


@dataclass(slots=True)
class AgentRun:
    """State of one spawned subagent."""

    agent_id: str
    agent_type: str
    description: str
    prompt: str
    background: bool
    status: str = "running"
    result_text: str = ""
    error: str | None = None
    turns: int = 0
    tool_calls: int = 0
    task: asyncio.Task[None] | None = None
    session: CodingSession | None = None
    provider: object | None = None
    result_consumed: bool = False
    completed_at: float | None = None
    requested_model: str | None = None
    requested_thinking: str | None = None
    requested_max_turns: int | None = None
    max_turns: int | None = None
    grace_turns: int = 5
    soft_limit_reached: bool = False
    aborted: bool = False
    pending_steers: list[str] = field(default_factory=list)
    join_mode: str | None = None
    requested_isolation: str | None = None
    worktree: Worktree | None = None
    used_worktree: bool = False
    output_writer: OutputFileWriter | None = None
    output_file: str | None = None


class SubagentManager:
    """Spawns and tracks subagent runs for one Tau session."""

    def __init__(self, api: ExtensionAPI) -> None:
        self._api = api
        self._runs: dict[str, AgentRun] = {}
        self._counter = 0
        self._settings: SubagentSettings | None = None
        self._running_background = 0
        self._queue: list[tuple[AgentRun, AgentDefinition]] = []
        self._shutting_down = False
        self._batch: list[AgentRun] = []
        self._batch_timer: asyncio.TimerHandle | None = None
        self._batch_counter = 0
        self._group_join: GroupJoinManager | None = None
        self._worktree_repos: set[str] = set()

    @property
    def runs(self) -> dict[str, AgentRun]:
        return self._runs

    @property
    def max_concurrent(self) -> int:
        return self._get_settings().max_concurrent

    def definitions(self) -> dict[str, AgentDefinition]:
        return load_agent_definitions(self._api.context.cwd)

    def _get_settings(self) -> SubagentSettings:
        if self._settings is None:
            self._settings = load_subagent_settings(self._api.context.cwd)
        return self._settings

    def spawn(
        self,
        *,
        agent_type: AgentDefinition,
        prompt: str,
        description: str,
        background: bool,
        max_turns: int | None = None,
        isolation: str | None = None,
        model: str | None = None,
        thinking: str | None = None,
    ) -> AgentRun:
        self._counter += 1
        run = AgentRun(
            agent_id=f"agent-{self._counter}",
            agent_type=agent_type.name,
            description=description,
            prompt=prompt,
            background=background,
            requested_model=model,
            requested_thinking=thinking,
            requested_max_turns=max_turns,
            requested_isolation=isolation,
        )
        run.output_writer = OutputFileWriter(
            output_file_path(
                self._api.context.cwd,
                self._api.context.session_id,
                run.agent_id,
            ),
            run.agent_id,
            self._api.context.cwd,
        )
        run.output_file = str(run.output_writer.path)
        self._runs[run.agent_id] = run
        if background:
            run.join_mode = self._get_settings().default_join_mode
            if run.join_mode in ("smart", "group"):
                self._batch.append(run)
                self._schedule_batch_finalize()
        if background and self._running_background >= self.max_concurrent:
            run.status = "queued"
            self._queue.append((run, agent_type))
            return run
        self._start(run, agent_type)
        return run

    def _get_group_join(self) -> GroupJoinManager:
        if self._group_join is None:
            self._group_join = GroupJoinManager(
                self._deliver_group_notification,
                group_timeout=GROUP_TIMEOUT_SECONDS,
                straggler_timeout=STRAGGLER_TIMEOUT_SECONDS,
            )
        return self._group_join

    def _schedule_batch_finalize(self) -> None:
        if self._batch_timer is not None:
            self._batch_timer.cancel()
        self._batch_timer = asyncio.get_running_loop().call_later(
            BATCH_DEBOUNCE_SECONDS, self._finalize_batch
        )

    def _finalize_batch(self) -> None:
        self._batch_timer = None
        batch = self._batch
        self._batch = []
        if len(batch) >= 2:
            self._batch_counter += 1
            self._get_group_join().register_group(
                f"batch-{self._batch_counter}", [run.agent_id for run in batch]
            )
            for run in batch:
                if run.status in TERMINAL_STATUSES and run.status != "cancelled":
                    self._get_group_join().on_agent_complete(run)
            return
        for run in batch:
            if run.status in TERMINAL_STATUSES and run.status != "cancelled":
                self._deliver_background_result(run)

    def _start(self, run: AgentRun, definition: AgentDefinition) -> None:
        run.status = "running"
        if run.background:
            self._running_background += 1
        run.task = asyncio.get_running_loop().create_task(
            self._run_agent(run, definition)
        )

    def _drain_queue(self) -> None:
        if self._shutting_down:
            return
        while self._queue and self._running_background < self.max_concurrent:
            run, definition = self._queue.pop(0)
            if run.status != "queued":
                continue
            self._start(run, definition)

    async def resume(self, run: AgentRun, prompt: str) -> None:
        """Resume a finished run's live session with a follow-up prompt."""
        session = run.session
        assert session is not None
        run.status = "running"
        run.error = None
        run.result_text = ""
        run.aborted = False
        run.soft_limit_reached = False
        run.completed_at = None
        final_text: list[str] = []
        try:
            async for event in session.prompt(prompt):
                self._observe(run, event, final_text)
            run.result_text = final_text[-1] if final_text else ""
            if run.status == "running":
                run.status = "completed"
        except Exception as exc:  # noqa: BLE001 - report subagent failures as results
            run.status = "error"
            run.error = str(exc)
        finally:
            await self._persist_record(run)
            run.completed_at = time.monotonic()

    async def shutdown(self) -> None:
        self._shutting_down = True
        try:
            if self._batch_timer is not None:
                self._batch_timer.cancel()
                self._batch_timer = None
            self._batch.clear()
            if self._group_join is not None:
                self._group_join.cancel_all()
            for run, _definition in self._queue:
                if run.status == "queued":
                    run.status = "cancelled"
                    run.completed_at = time.monotonic()
            self._queue.clear()
            pending: list[asyncio.Task[None]] = []
            for run in self._runs.values():
                if run.task is not None and not run.task.done():
                    if run.session is not None:
                        run.session.cancel()
                    run.task.cancel()
                    pending.append(run.task)
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            for run in self._runs.values():
                await self.close_run(run)
            for repo in self._worktree_repos:
                with contextlib.suppress(Exception):
                    await prune_worktrees(Path(repo))
            self._worktree_repos.clear()
        finally:
            self._shutting_down = False

    async def evict_stale(self) -> None:
        """Close sessions of consumed terminal runs older than the staleness cap."""
        now = time.monotonic()
        for run in self._runs.values():
            if run.session is None and run.provider is None:
                continue
            if run.status not in TERMINAL_STATUSES or not run.result_consumed:
                continue
            if run.completed_at is None or now - run.completed_at < STALE_AFTER_SECONDS:
                continue
            await self.close_run(run)

    async def close_run(self, run: AgentRun) -> None:
        session = run.session
        if session is not None:
            try:
                await session.aclose()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass
        closer = getattr(run.provider, "aclose", None)
        if closer is not None:
            try:
                await closer()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass
        run.session = None
        run.provider = None

    async def _run_agent(self, run: AgentRun, definition: AgentDefinition) -> None:
        try:
            await self._execute(run, definition)
        except asyncio.CancelledError:
            run.status = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001 - report subagent failures as results
            run.status = "error"
            run.error = str(exc)
        finally:
            await self._finalize_run(run)
            run.completed_at = time.monotonic()
            if run.background:
                self._running_background -= 1
                self._drain_queue()
                self._handle_background_completion(run)

    async def _finalize_run(self, run: AgentRun) -> None:
        if run.output_writer is not None and run.session is not None:
            with contextlib.suppress(Exception):
                await run.output_writer.flush(run.session.messages)
        if run.worktree is not None:
            await self._cleanup_worktree(run)
        await self._persist_record(run)

    async def _cleanup_worktree(self, run: AgentRun) -> None:
        worktree = run.worktree
        run.worktree = None
        assert worktree is not None
        try:
            result = await cleanup_worktree(worktree, run.description)
        except Exception:  # noqa: BLE001 - worktree cleanup is best-effort
            return
        if result.has_changes and result.branch:
            annotation = (
                f"\n\n---\nChanges saved to branch `{result.branch}`."
                f" Merge with: `git merge {result.branch}`"
            )
            run.result_text += annotation
            # Error terminals surface run.error, not result_text — annotate
            # both so committed work is never invisible.
            if run.status == "error":
                run.error = (run.error or "subagent failed") + annotation

    async def _persist_record(self, run: AgentRun) -> None:
        try:
            await self._api.append_entry(
                "subagents:record",
                {
                    "id": run.agent_id,
                    "type": run.agent_type,
                    "description": run.description,
                    "status": run.status,
                    "result": run.result_text[:RECORD_RESULT_CHARS],
                    "error": run.error,
                    "turns": run.turns,
                    "tool_calls": run.tool_calls,
                },
            )
        except Exception:  # noqa: BLE001 - record persistence is best-effort
            pass

    def _handle_background_completion(self, run: AgentRun) -> None:
        if run.status == "cancelled":
            return
        if run.join_mode in ("smart", "group"):
            if any(pending is run for pending in self._batch):
                return  # the batch finalizer will route this completion
            if self._get_group_join().on_agent_complete(run) != "pass":
                return
        self._deliver_background_result(run)

    async def _execute(self, run: AgentRun, definition: AgentDefinition) -> None:
        settings = self._get_settings()
        cwd = self._api.context.cwd
        # Create the provider before the first await so concurrent spawns
        # claim providers in spawn order (tests script provider sequences).
        # Frontmatter wins over the tool param, per pi precedence.
        provider_settings = load_provider_settings()
        selection = resolve_provider_selection(
            provider_settings, model=definition.model or run.requested_model
        )
        provider = create_model_provider(
            selection.provider,
            model=selection.model,
            thinking_level=(
                definition.thinking or run.requested_thinking or DEFAULT_THINKING_LEVEL
            ),
        )
        run.provider = provider
        child_cwd = cwd
        if (definition.isolation or run.requested_isolation) == "worktree":
            worktree = await create_worktree(cwd, run.agent_id)
            if worktree is None:
                raise RuntimeError(WORKTREE_ERROR_MESSAGE)
            run.worktree = worktree
            run.used_worktree = True
            self._worktree_repos.add(str(worktree.repo))
            child_cwd = worktree.work_path
        if run.output_writer is not None:
            await run.output_writer.write_initial(run.prompt)
        skill_blocks = resolve_skill_blocks(
            definition.skills if isinstance(definition.skills, tuple) else None, cwd
        )
        memory_block: str | None = None
        memory_rw = False
        if definition.memory is not None:
            memory_rw = definition.tools is None or bool(
                {"write", "edit"} & set(definition.tools)
            )
            memory_block = await prepare_memory(
                definition.name, definition.memory, cwd, read_write=memory_rw
            )
        parent_prompt: str | None = None
        if definition.prompt_mode == "append":
            try:
                parent_prompt = self._api.context.system_prompt
            except Exception:  # noqa: BLE001 - fall back to replace-mode assembly
                parent_prompt = None
        environment = ""
        if definition.prompt_mode == "append" and parent_prompt:
            environment = await detect_environment(child_cwd)
        prompt_text = build_child_system_prompt(
            definition,
            parent_prompt=parent_prompt,
            environment=environment,
            skill_blocks=skill_blocks,
            memory_block=memory_block,
        )
        append_active = definition.prompt_mode == "append" and bool(parent_prompt)
        session = await CodingSession.load(
            CodingSessionConfig(
                provider=provider,
                model=selection.model,
                cwd=child_cwd,
                storage=_MemoryStorage(),
                system=prompt_text if append_active else None,
                custom_system_prompt=None if append_active else prompt_text,
                # Children always discover skills natively (Tau defaults
                # resource paths from the session cwd). skills: true pins
                # discovery to the PARENT cwd, which matters under worktree
                # isolation where the default would use the worktree copy.
                resource_paths=(
                    inherited_resource_paths(cwd)
                    if definition.skills is True
                    else None
                ),
                provider_name=selection.provider.name,
                auto_compact_enabled=False,
                # Subagents load no extensions, so they cannot spawn recursively.
                extensions_enabled=False,
            )
        )
        run.session = session
        for message in run.pending_steers:
            session.queue_steering_message(message)
        run.pending_steers.clear()
        run.max_turns = _effective_max_turns(
            definition.max_turns, run.requested_max_turns, settings.default_max_turns
        )
        run.grace_turns = settings.grace_turns
        if definition.tools is not None:
            allowed = set(definition.tools)
            if memory_block is not None:
                allowed |= {"read", "write", "edit"} if memory_rw else {"read"}
            session._harness.config.tools = [  # noqa: SLF001 - scoped tool gating
                tool for tool in session._harness.config.tools if tool.name in allowed
            ]
        final_text: list[str] = []
        async for event in session.prompt(run.prompt):
            self._observe(run, event, final_text)
            if event.type == "turn_end":
                self._enforce_turn_limit(run)
                if run.output_writer is not None:
                    await run.output_writer.flush(session.messages)
        run.result_text = final_text[-1] if final_text else ""
        if run.aborted:
            run.status = "aborted"
        elif run.status == "running":
            run.status = "steered" if run.soft_limit_reached else "completed"

    def _enforce_turn_limit(self, run: AgentRun) -> None:
        if run.max_turns is None or run.session is None:
            return
        if not run.soft_limit_reached and run.turns >= run.max_turns:
            run.soft_limit_reached = True
            run.session.queue_steering_message(SOFT_LIMIT_MESSAGE)
        elif (
            run.soft_limit_reached
            and not run.aborted
            and run.turns >= run.max_turns + run.grace_turns
        ):
            run.aborted = True
            run.session.cancel()

    def _observe(self, run: AgentRun, event: AgentEvent, final_text: list[str]) -> None:
        if event.type == "turn_end":
            run.turns += 1
        elif event.type == "tool_execution_start":
            run.tool_calls += 1
        elif event.type == "message_end":
            message = event.message
            if getattr(message, "role", None) == "assistant" and message.content.strip():
                final_text.append(message.content.strip())
        elif event.type == "error" and not event.recoverable:
            run.status = "error"
            run.error = event.message

    def _deliver_background_result(self, run: AgentRun) -> None:
        if run.result_consumed:
            return
        try:
            self._api.notify(
                f"Subagent {run.agent_id} ({run.description}) {run.status}.",
                "info" if run.status in ("completed", "steered") else "warning",
            )
            footer = (
                f"\nFull transcript available at: {run.output_file}"
                if run.output_file
                else ""
            )
            self._api.send_user_message(
                f"{format_task_notification(run)}\n{COMPLETION_NOTICE}{footer}",
                deliver_as="follow_up",
            )
        except Exception:  # noqa: BLE001 - timer callbacks must never crash the loop
            pass

    def _deliver_group_notification(self, records: list[AgentRun], partial: bool) -> None:
        try:
            self._api.send_user_message(
                format_group_notification(records, partial=partial),
                deliver_as="follow_up",
            )
        except Exception:  # noqa: BLE001 - timer callbacks must never crash the loop
            pass


def _effective_max_turns(
    frontmatter: int | None, param: int | None, default: int | None
) -> int | None:
    """Resolve the turn limit: frontmatter wins over param wins over default."""
    if frontmatter is not None:
        raw = frontmatter
    elif param is not None:
        raw = param
    else:
        raw = default
    if raw is None or raw <= 0:
        return None
    return max(1, raw)


def format_background_spawn(run: AgentRun, max_concurrent: int) -> str:
    """Format the tool result for a background spawn (started or queued)."""
    started = run.status != "queued"
    lines = [
        "Agent started in background." if started else "Agent queued in background.",
        f"Agent ID: {run.agent_id}",
        f"Type: {run.agent_type}",
        f"Description: {run.description}",
    ]
    if run.output_file:
        lines.append(f"Output file: {run.output_file}")
    if not started:
        lines.append(f"Position: queued (max {max_concurrent} concurrent)")
    lines.extend(
        [
            "",
            "You will be notified when this agent completes.",
            "Use get_subagent_result to retrieve full results, or steer_subagent"
            " to send it messages.",
            "Do not duplicate this agent's work.",
        ]
    )
    return "\n".join(lines)


COMPLETION_NOTICE = (
    "This is an automated subagent completion notice, not a user message. "
    "Use the result to continue the original task."
)


def format_task_notification(
    run: AgentRun, max_result_chars: int = INDIVIDUAL_RESULT_CHARS
) -> str:
    """Format one run's completion as a <task-notification> block."""
    body = run.error if run.status == "error" else run.result_text
    truncated = _truncate_result(body or "(no output)", max_result_chars)
    output_line = (
        f"<output-file>{run.output_file}</output-file>\n" if run.output_file else ""
    )
    return (
        "<task-notification>\n"
        f"<agent-id>{run.agent_id}</agent-id>\n"
        f"<type>{run.agent_type}</type>\n"
        f"<description>{run.description}</description>\n"
        f"{output_line}"
        f"<status>{run.status}</status>\n"
        f"<turns>{run.turns}</turns>\n"
        f"<result>{truncated}</result>\n"
        "</task-notification>"
    )


def format_group_notification(records: list[AgentRun], *, partial: bool) -> str:
    """Format a consolidated completion notice for a group of runs."""
    label = f"{len(records)} agent(s) finished"
    if partial:
        label += " (partial — others still running)"
    blocks = "\n\n".join(
        format_task_notification(run, GROUP_RESULT_CHARS) for run in records
    )
    return (
        f"Background agent group completed: {label}\n\n"
        f"{blocks}\n\n"
        "Use get_subagent_result for full output."
    )


def _truncate_result(body: str, max_chars: int) -> str:
    if len(body) > max_chars:
        return body[:max_chars] + TRUNCATION_SUFFIX
    return body


def format_run_summary(run: AgentRun) -> str:
    """Format one run for /agents and get_subagent_result output."""
    return (
        f"{run.agent_id} [{run.status}] type={run.agent_type}"
        f" turns={run.turns} tools={run.tool_calls} — {run.description}"
    )


def setup(tau: ExtensionAPI) -> None:
    """Register subagent tools, the /agents command, and shutdown cleanup."""
    manager = SubagentManager(tau)

    async def run_agent_tool(arguments, signal=None):  # noqa: ANN001, ANN202
        await manager.evict_stale()
        resume_id = arguments.get("resume")
        if resume_id:
            return await run_resume(str(resume_id), arguments)

        definitions = manager.definitions()
        agent_type = str(arguments.get("subagent_type", "general"))
        definition = definitions.get(agent_type)
        if definition is None:
            available = ", ".join(sorted(definitions))
            return _tool_result(
                "agent",
                ok=False,
                content=f"Unknown subagent_type: {agent_type}. Available: {available}",
            )
        prompt = str(arguments.get("prompt", "")).strip()
        if not prompt:
            return _tool_result("agent", ok=False, content="prompt is required")
        description = str(arguments.get("description", "")) or f"{agent_type} agent"
        background = bool(arguments.get("run_in_background", False))
        max_turns = _coerce_max_turns(arguments.get("max_turns"))
        isolation = (
            "worktree" if arguments.get("isolation") == "worktree" else None
        )
        model = str(arguments.get("model")) if arguments.get("model") else None
        thinking = arguments.get("thinking")
        if thinking is not None:
            thinking = str(thinking)
            if thinking not in THINKING_LEVELS:
                return _tool_result(
                    "agent",
                    ok=False,
                    content=f"Invalid thinking level: {thinking}."
                    f" Valid options: {', '.join(THINKING_LEVELS)}",
                )

        run = manager.spawn(
            agent_type=definition,
            prompt=prompt,
            description=description,
            background=background,
            max_turns=max_turns,
            isolation=isolation,
            model=model,
            thinking=thinking,
        )
        if background:
            return _tool_result(
                "agent",
                ok=True,
                content=format_background_spawn(run, manager.max_concurrent),
            )

        assert run.task is not None
        while not run.task.done():
            if signal is not None and signal.is_cancelled():
                if run.session is not None:
                    run.session.cancel()
                run.task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await run.task
                await manager.close_run(run)
                return _tool_result("agent", ok=False, content="Subagent cancelled")
            await asyncio.sleep(0.05)

        return _foreground_result(run)

    async def run_resume(agent_id: str, arguments) -> AgentToolResult:  # noqa: ANN001
        run = manager.runs.get(agent_id)
        if run is None:
            return _tool_result(
                "agent",
                ok=False,
                content=f'Agent not found: "{agent_id}". It may have been cleaned up.',
            )
        if run.task is not None and not run.task.done():
            return _tool_result(
                "agent",
                ok=False,
                content=f'Agent "{agent_id}" is still running.'
                " Use steer_subagent to redirect it.",
            )
        if run.used_worktree:
            return _tool_result(
                "agent",
                ok=False,
                content=f'Agent "{agent_id}" ran in an isolated worktree that has'
                " been cleaned up; resume is not supported for worktree agents.",
            )
        if run.session is None:
            return _tool_result(
                "agent",
                ok=False,
                content=f'Agent "{agent_id}" has no active session to resume.',
            )
        prompt = str(arguments.get("prompt", "")).strip()
        if not prompt:
            return _tool_result("agent", ok=False, content="prompt is required")
        await manager.resume(run, prompt)
        return _foreground_result(run)

    async def run_get_result_tool(arguments, signal=None):  # noqa: ANN001, ANN202
        del signal
        await manager.evict_stale()
        agent_id = str(arguments.get("agent_id", ""))
        run = manager.runs.get(agent_id)
        if run is None:
            known = ", ".join(sorted(manager.runs)) or "none"
            return _tool_result(
                "get_subagent_result",
                ok=False,
                content=f"Unknown agent_id: {agent_id}. Known agents: {known}",
            )
        if bool(arguments.get("wait", False)) and run.status in ("running", "queued"):
            # Claim the result before waiting so the background completion
            # path skips its redundant follow-up notification. Poll on status
            # rather than the task so a queued run is followed through its
            # queued -> started -> finished transitions.
            run.result_consumed = True
            while run.status in ("running", "queued"):
                await asyncio.sleep(0.05)
        header = format_run_summary(run)
        if run.output_file:
            header += f"\nOutput file: {run.output_file}"
        if run.status == "queued":
            return _tool_result(
                "get_subagent_result",
                ok=True,
                content=f"{header}\n\n"
                f"Still queued (max {manager.max_concurrent} concurrent).",
            )
        if run.status == "running":
            return _tool_result(
                "get_subagent_result",
                ok=True,
                content=f"{header}\n\nStill running.",
            )
        run.result_consumed = True
        body = run.error if run.status == "error" else run.result_text
        return _tool_result(
            "get_subagent_result",
            ok=run.status in ("completed", "steered"),
            content=f"{header}\n\n{body or '(no output)'}",
        )

    async def run_steer_tool(arguments, signal=None):  # noqa: ANN001, ANN202
        del signal
        agent_id = str(arguments.get("agent_id", ""))
        message = str(arguments.get("message", ""))
        run = manager.runs.get(agent_id)
        if run is None:
            return _tool_result(
                "steer_subagent",
                ok=False,
                content=f'Agent not found: "{agent_id}". It may have been cleaned up.',
            )
        if run.status not in ("running", "queued"):
            return _tool_result(
                "steer_subagent",
                ok=False,
                content=f'Agent "{agent_id}" is not running (status: {run.status}).'
                " Cannot steer a non-running agent.",
            )
        if run.session is None:
            run.pending_steers.append(message)
            return _tool_result(
                "steer_subagent",
                ok=True,
                content=f"Steering message queued for agent {agent_id}."
                " It will be delivered once the session initializes.",
            )
        run.session.queue_steering_message(message)
        return _tool_result(
            "steer_subagent",
            ok=True,
            content=f"Steering message sent to agent {agent_id}."
            " The agent will process it after its current tool execution.\n"
            f"Current state: {run.tool_calls} tool uses",
        )

    def agents_command(args: str, context) -> str:  # noqa: ANN001
        del args, context
        definitions = manager.definitions()
        lines = ["Agent types:", format_agent_type_list(definitions)]
        if manager.runs:
            lines.append("")
            lines.append("Runs:")
            lines.extend(format_run_summary(run) for run in manager.runs.values())
        else:
            lines.append("")
            lines.append("No agents have been spawned in this session.")
        return "\n".join(lines)

    async def on_shutdown(event: SessionShutdownEvent) -> None:
        # Tear down on every shutdown reason (new/resume/branch/quit): runs
        # belong to the outgoing transcript and would otherwise leak sessions.
        del event
        await manager.shutdown()
        manager.runs.clear()

    type_list = format_agent_type_list(load_agent_definitions(Path.cwd()))
    tau.register_tool(
        AgentTool(
            name="agent",
            description=(
                "Spawn an autonomous subagent to handle a task. The subagent works"
                " in its own context with its own tools and returns its final"
                " report. Set run_in_background=true for long tasks; a completion"
                " notification will arrive when it finishes. Use steer_subagent to"
                " redirect a running agent, and resume=<id> with a new prompt to"
                " continue a finished agent's session.\n\nAvailable agent"
                f" types:\n{type_list}"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "The task for the subagent to perform.",
                    },
                    "description": {
                        "type": "string",
                        "description": "A short (3-5 word) description of the task.",
                    },
                    "subagent_type": {
                        "type": "string",
                        "description": "The agent type to spawn (default: general).",
                    },
                    "run_in_background": {
                        "type": "boolean",
                        "description": "Return immediately and notify on completion.",
                    },
                    "model": {
                        "type": "string",
                        "description": "Model for the subagent (default: the agent"
                        " type's model, else the parent's).",
                    },
                    "thinking": {
                        "type": "string",
                        "enum": list(THINKING_LEVELS),
                        "description": "Reasoning effort for the subagent"
                        " (default: medium).",
                    },
                    "max_turns": {
                        "type": "number",
                        "minimum": 1,
                        "description": "Soft turn limit; the agent is asked to wrap"
                        " up, then hard-cancelled after a grace period.",
                    },
                    "resume": {
                        "type": "string",
                        "description": "Id of a finished agent to resume with this"
                        " prompt (always runs foreground).",
                    },
                    "isolation": {
                        "type": "string",
                        "enum": ["worktree"],
                        "description": "Set to \"worktree\" to run the agent in an"
                        " isolated git worktree; changes are saved to a"
                        " tau-agent-<id> branch.",
                    },
                },
                "required": ["prompt", "description"],
            },
            executor=run_agent_tool,
            prompt_snippet="Spawn an autonomous subagent for delegated tasks.",
        )
    )
    tau.register_tool(
        AgentTool(
            name="get_subagent_result",
            description="Get the status or final result of a spawned subagent.",
            input_schema={
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "The id returned when the agent was spawned.",
                    },
                    "wait": {
                        "type": "boolean",
                        "description": "Block until the agent finishes.",
                    },
                },
                "required": ["agent_id"],
            },
            executor=run_get_result_tool,
        )
    )
    tau.register_tool(
        AgentTool(
            name="steer_subagent",
            description=(
                "Send a steering message to a running subagent. It will be"
                " delivered after the agent's current tool execution and appear as"
                " a user message in the agent's conversation."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "The id of the running agent to steer.",
                    },
                    "message": {
                        "type": "string",
                        "description": "The message to send. This will appear as a"
                        " user message in the agent's conversation.",
                    },
                },
                "required": ["agent_id", "message"],
            },
            executor=run_steer_tool,
        )
    )
    tau.register_command(
        "agents",
        agents_command,
        description="List subagent types and runs.",
    )
    tau.on("session_shutdown", on_shutdown)


def _foreground_result(run: AgentRun) -> AgentToolResult:
    if run.status in ("completed", "steered"):
        content = run.result_text or "(subagent produced no output)"
        return _tool_result(
            "agent",
            ok=True,
            content=f"{format_run_summary(run)}\n\n{content}",
        )
    return _tool_result(
        "agent",
        ok=False,
        content=f"{format_run_summary(run)}\n\n{run.error or 'subagent failed'}",
    )


def _coerce_max_turns(value) -> int | None:  # noqa: ANN001
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _tool_result(name: str, *, ok: bool, content: str) -> AgentToolResult:
    return AgentToolResult(
        tool_call_id="",
        name=name,
        ok=ok,
        content=content,
        error=None if ok else content,
    )
