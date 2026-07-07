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
import dataclasses
import functools
import time
from collections.abc import Callable, Mapping
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
from tau_coding.extensions import (
    ExtensionAPI,
    SessionShutdownEvent,
    SessionStartEvent,
)
from tau_coding.provider_runtime import create_model_provider
from tau_coding.thinking import DEFAULT_THINKING_LEVEL, THINKING_LEVELS

from .agents import AgentDefinition, format_agent_type_list, load_agent_definitions
from .agents_menu import (
    show_agents_menu,
    supports_menu,
)
from .group_join import DEFAULT_TIMEOUT, STRAGGLER_TIMEOUT, GroupJoinManager
from .memory import prepare_memory
from .notification_render import render_agent_result, render_notification, stat_parts
from .output_file import OutputFileWriter, output_file_path
from .prompts import (
    build_child_system_prompt,
    build_parent_context,
    detect_environment,
    inherited_resource_paths,
    resolve_skill_blocks,
)
from .schedule import SubagentScheduler
from .schedule_store import ScheduleStore, resolve_store_path
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
# Display-only budget for the foreground card's expanded view (Ctrl+O). More
# generous than the notification previews above, which also enter the model's
# context: the old generic result block showed the full text, so a tight cap
# here would be a regression.
FOREGROUND_RESULT_CHARS = 6_000
RECORD_RESULT_CHARS = 4_000
TRUNCATION_SUFFIX = "\n...(truncated, use get_subagent_result for full output)"
BATCH_DEBOUNCE_SECONDS = 0.1
NUDGE_HOLD_SECONDS = 0.2
GROUP_TIMEOUT_SECONDS = DEFAULT_TIMEOUT
STRAGGLER_TIMEOUT_SECONDS = STRAGGLER_TIMEOUT
STALE_AFTER_SECONDS = 600.0
TERMINAL_STATUSES = ("completed", "steered", "aborted", "error", "cancelled")
# Event types that change what a viewer would show (session.messages / status).
# Streaming deltas are skipped: they never touch session.messages, so pushing on
# them would redraw identical content on every token.
RUN_PUSH_EVENTS = frozenset(
    {"message_end", "tool_execution_start", "tool_execution_end", "turn_end", "error"}
)
SOFT_LIMIT_MESSAGE = (
    "You have reached your turn limit. Wrap up immediately — provide your"
    " final answer now."
)


@functools.cache
def _supports_skills_enabled() -> bool:
    """Whether this Tau has the CodingSessionConfig.skills_enabled seam."""
    return "skills_enabled" in {
        config_field.name for config_field in dataclasses.fields(CodingSessionConfig)
    }


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
    revision: int = 0
    task: asyncio.Task[None] | None = None
    session: CodingSession | None = None
    provider: object | None = None
    result_consumed: bool = False
    started_at: float | None = None
    completed_at: float | None = None
    context_tokens: int = 0
    tokens_input: int = 0
    tokens_output: int = 0
    tokens_cache_write: int = 0
    has_usage: bool = False
    requested_model: str | None = None
    requested_thinking: str | None = None
    requested_max_turns: int | None = None
    max_turns: int | None = None
    grace_turns: int = 5
    soft_limit_reached: bool = False
    aborted: bool = False
    pending_steers: list[str] = field(default_factory=list)
    # Live stats ticker (foreground runs only): tau's tool-progress seam, set
    # for the duration of the blocking tool call. `last_progress` dedups so
    # the line only repaints when a stat actually changed.
    on_update: Callable[[str, dict[str, object] | None], None] | None = None
    last_progress: str = ""
    join_mode: str | None = None
    requested_isolation: str | None = None
    worktree: Worktree | None = None
    used_worktree: bool = False
    output_writer: OutputFileWriter | None = None
    output_file: str | None = None
    # Per-run push listeners (component seam): fired when this run's content or
    # status changes so an open conversation viewer can re-render. The direct
    # analog of pi's ``session.subscribe(() => tui.requestRender())``. Safe to
    # call widget methods from here because runs are asyncio tasks on the TUI
    # event loop (see the design's push-refresh invariant).
    listeners: list[Callable[[], None]] = field(default_factory=list)


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
        self._nudge_timers: dict[str, asyncio.TimerHandle] = {}
        # Roster change signal: fired when the run list or a run's status
        # changes. On the component seam it is wired (in setup()) to the UI
        # controller's on_change, which refreshes the extension's own strip
        # and any open viewer. (The name predates the seam migration: it once
        # pointed at tau core's removed transcript-sources callback.)
        self.sources_changed: Callable[[], None] | None = None

    def _notify_sources(self) -> None:
        callback = self.sources_changed
        if callback is not None:
            with contextlib.suppress(Exception):
                callback()

    def _notify_run(self, run: AgentRun) -> None:
        """Fire a run's per-run listeners (viewer push); guarded per listener."""
        for listener in tuple(run.listeners):
            with contextlib.suppress(Exception):
                listener()

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
        bypass_queue: bool = False,
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
        if (
            background
            and not bypass_queue
            and self._running_background >= self.max_concurrent
        ):
            run.status = "queued"
            self._queue.append((run, agent_type))
            self._notify_sources()
            return run
        self._start(run, agent_type)
        self._notify_sources()
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
        run.started_at = time.monotonic()
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
        run.started_at = time.monotonic()
        run.completed_at = None
        self._notify_sources()
        self._notify_run(run)
        final_text: list[str] = []
        try:
            async for event in session.prompt(prompt):
                self._observe(run, event, final_text)
            run.result_text = final_text[-1] if final_text else ""
            run.context_tokens = session.context_token_estimate
            if run.status == "running":
                run.status = "completed"
        except asyncio.CancelledError:
            # The resume runs inline in the tool coroutine, so a parent Esc
            # (hard-cancel) lands here; settle the record before re-raising.
            run.status = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001 - report subagent failures as results
            run.status = "error"
            run.error = str(exc)
        finally:
            await self._persist_record(run)
            run.completed_at = time.monotonic()
            self._notify_sources()
            self._notify_run(run)

    async def shutdown(self) -> None:
        self._shutting_down = True
        try:
            if self._batch_timer is not None:
                self._batch_timer.cancel()
                self._batch_timer = None
            self._batch.clear()
            if self._group_join is not None:
                self._group_join.cancel_all()
            for handle in self._nudge_timers.values():
                handle.cancel()
            self._nudge_timers.clear()
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
            self._notify_sources()

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
            self._notify_sources()
            self._notify_run(run)
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
                    "total_tokens": lifetime_tokens(run) if run.has_usage else None,
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
        extra_config: dict[str, bool] = {}
        # skills: none/false disables skill discovery; a named CSV also
        # disables it so preloaded bodies aren't double-listed in the index
        # (pi sets noSkills for both). Requires the skills_enabled seam;
        # otherwise fall back to default discovery.
        if (
            definition.skills is False or isinstance(definition.skills, tuple)
        ) and _supports_skills_enabled():
            extra_config["skills_enabled"] = False
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
                **extra_config,
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
                run.context_tokens = session.context_token_estimate
                if run.output_writer is not None:
                    await run.output_writer.flush(session.messages)
        run.result_text = final_text[-1] if final_text else ""
        run.context_tokens = session.context_token_estimate
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
        run.revision += 1
        if event.type == "turn_end":
            run.turns += 1
        elif event.type == "tool_execution_start":
            run.tool_calls += 1
        elif event.type == "message_end":
            message = event.message
            if getattr(message, "role", None) == "assistant":
                # Real billed usage (Tau provider-usage seam). Lifetime sum of
                # input + output + cache_write per response; cache reads are
                # excluded because each turn re-reads the whole cached prefix,
                # so summing them counts the prefix N times (pi issue #38).
                usage = getattr(message, "usage", None)
                if usage is not None:
                    run.tokens_input += getattr(usage, "input", 0) or 0
                    run.tokens_output += getattr(usage, "output", 0) or 0
                    run.tokens_cache_write += getattr(usage, "cache_write", 0) or 0
                    run.has_usage = True
                if message.content.strip():
                    final_text.append(message.content.strip())
        elif event.type == "error" and not event.recoverable:
            run.status = "error"
            run.error = event.message
        self._emit_progress(run, event)
        if event.type in RUN_PUSH_EVENTS:
            self._notify_run(run)

    def _emit_progress(self, run: AgentRun, event: AgentEvent) -> None:
        """Update the live stats ticker under the tool row (foreground runs).

        One stable, cumulative line in the completion card's stats vocabulary
        (turns · tool uses · tokens), so the running row morphs into the
        finished card — unlike the per-event activity echo this replaces
        (dropped as noise in d0d5ac8), the line only changes when a stat does.
        """
        if run.on_update is None:
            return
        if event.type not in ("turn_end", "tool_execution_start", "message_end"):
            return
        message = format_live_stats(run)
        if not message or message == run.last_progress:
            return
        run.last_progress = message
        data: dict[str, object] = {
            "agent_id": run.agent_id,
            "turns": run.turns,
            "tool_uses": run.tool_calls,
        }
        if run.has_usage:
            data["total_tokens"] = lifetime_tokens(run)
        with contextlib.suppress(Exception):
            run.on_update(message, data)

    def _deliver_background_result(self, run: AgentRun) -> None:
        if run.result_consumed:
            return
        self._schedule_nudge(run.agent_id, lambda: self._send_individual_nudge(run))

    def _deliver_group_notification(self, records: list[AgentRun], partial: bool) -> None:
        key = "group:" + ",".join(run.agent_id for run in records)
        self._schedule_nudge(
            key, lambda: self._send_group_notification(records, partial)
        )

    def _schedule_nudge(self, key: str, send: Callable[[], None]) -> None:
        """Hold a notification briefly so a prompt result read can cancel it."""
        existing = self._nudge_timers.pop(key, None)
        if existing is not None:
            existing.cancel()

        def fire() -> None:
            self._nudge_timers.pop(key, None)
            send()

        self._nudge_timers[key] = asyncio.get_running_loop().call_later(
            NUDGE_HOLD_SECONDS, fire
        )

    def cancel_nudge(self, agent_id: str) -> None:
        """Cancel a pending individual nudge for a consumed run."""
        handle = self._nudge_timers.pop(agent_id, None)
        if handle is not None:
            handle.cancel()

    def _send_individual_nudge(self, run: AgentRun) -> None:
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
            self._deliver_notification(
                f"{format_task_notification(run)}\n{COMPLETION_NOTICE}{footer}",
                build_notification_details(run),
            )
        except Exception:  # noqa: BLE001 - timer callbacks must never crash the loop
            pass

    def _send_group_notification(self, records: list[AgentRun], partial: bool) -> None:
        unconsumed = [run for run in records if not run.result_consumed]
        if not unconsumed:
            return
        try:
            details = build_notification_details(unconsumed[0], GROUP_RESULT_CHARS)
            details["others"] = [
                build_notification_details(run, GROUP_RESULT_CHARS)
                for run in unconsumed[1:]
            ]
            self._deliver_notification(
                format_group_notification(unconsumed, partial=partial),
                details,
            )
        except Exception:  # noqa: BLE001 - timer callbacks must never crash the loop
            pass

    def _deliver_notification(
        self, content: str, details: dict[str, object]
    ) -> None:
        """Send via the message-renderers seam when present, else raw."""
        send_custom = getattr(self._api, "send_custom_message", None)
        if send_custom is not None:
            send_custom(
                content,
                custom_type="subagent-notification",
                details=details,
                deliver_as="follow_up",
            )
        else:
            self._api.send_user_message(content, deliver_as="follow_up")


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
    usage_parts = []
    if run.has_usage:
        usage_parts.append(f"<total_tokens>{lifetime_tokens(run)}</total_tokens>")
    usage_parts.append(f"<tool_uses>{run.tool_calls}</tool_uses>")
    if run.context_tokens:
        usage_parts.append(
            f"<context_tokens>{run.context_tokens}</context_tokens>"
        )
    duration = _duration_ms(run)
    if duration is not None:
        usage_parts.append(f"<duration_ms>{duration}</duration_ms>")
    return (
        "<task-notification>\n"
        f"<agent-id>{run.agent_id}</agent-id>\n"
        f"<type>{run.agent_type}</type>\n"
        f"<description>{run.description}</description>\n"
        f"{output_line}"
        f"<status>{run.status}</status>\n"
        f"<turns>{run.turns}</turns>\n"
        f"<result>{truncated}</result>\n"
        f"<usage>{''.join(usage_parts)}</usage>\n"
        "</task-notification>"
    )


def build_notification_details(
    run: AgentRun, result_max_chars: int = INDIVIDUAL_RESULT_CHARS
) -> dict[str, object]:
    """Structured details for the custom renderer (pi's buildNotificationDetails)."""
    body = run.error if run.status == "error" else run.result_text
    preview = body or "No output."
    if len(preview) > result_max_chars:
        preview = preview[:result_max_chars] + "…"
    return {
        "description": run.description,
        "status": run.status,
        "turn_count": run.turns,
        "max_turns": run.max_turns,
        "tool_uses": run.tool_calls,
        "total_tokens": lifetime_tokens(run) if run.has_usage else 0,
        "duration_ms": _duration_ms(run) or 0,
        "output_file": run.output_file,
        "error": run.error,
        "result_preview": preview,
    }


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


def lifetime_tokens(run: AgentRun) -> int:
    """Lifetime billed tokens: input + output + cache_write (pi semantics)."""
    return run.tokens_input + run.tokens_output + run.tokens_cache_write


def format_live_stats(run: AgentRun) -> str:
    """Cumulative running-stats line in the completion card's vocabulary."""
    return " · ".join(
        stat_parts(
            {
                "turn_count": run.turns,
                "max_turns": run.max_turns,
                "tool_uses": run.tool_calls,
                "total_tokens": lifetime_tokens(run) if run.has_usage else 0,
            }
        )
    )


def format_usage_parts(run: AgentRun) -> str:
    """Format pi-style usage parts joined by middle dots."""
    parts = []
    if run.has_usage:
        parts.append(f"{lifetime_tokens(run)} tokens")
    parts.append(f"{run.tool_calls} tool uses")
    if run.context_tokens:
        parts.append(f"~{run.context_tokens} context tokens")
    duration = _duration_ms(run)
    if duration is not None:
        parts.append(f"{duration / 1000:.1f}s")
    return " · ".join(parts)


def _duration_ms(run: AgentRun) -> int | None:
    if run.started_at is None or run.completed_at is None:
        return None
    return max(0, int((run.completed_at - run.started_at) * 1000))


STEER_CALL_PREVIEW_CHARS = 60


def render_agent_call(arguments: Mapping[str, object]) -> str:
    """Friendly invocation line for the agent tool (pi's renderCall port)."""
    agent_type = str(arguments.get("subagent_type") or "general")
    description = str(arguments.get("description") or "").strip()
    line = f"▸ {agent_type} agent"
    schedule = arguments.get("schedule")
    if schedule:
        line += f" (scheduled {schedule})"
    return f"{line} · {description}" if description else line


def render_get_result_call(arguments: Mapping[str, object]) -> str:
    """Friendly invocation line for get_subagent_result."""
    line = f"▸ get result · {arguments.get('agent_id') or '?'}"
    if arguments.get("wait"):
        line += " (wait)"
    return line


def render_steer_call(arguments: Mapping[str, object]) -> str:
    """Friendly invocation line for steer_subagent."""
    message = " ".join(str(arguments.get("message") or "").split())
    if len(message) > STEER_CALL_PREVIEW_CHARS:
        message = message[:STEER_CALL_PREVIEW_CHARS].rstrip() + "…"
    line = f"▸ steer {arguments.get('agent_id') or '?'}"
    return f"{line} · {message}" if message else line


def setup(tau: ExtensionAPI) -> None:
    """Register subagent tools, the /agents command, and shutdown cleanup."""
    manager = SubagentManager(tau)
    if hasattr(tau, "register_message_renderer"):
        # message-renderers seam: notifications render as pi-style cards
        # instead of raw <task-notification> XML bubbles.
        tau.register_message_renderer("subagent-notification", render_notification)
    scheduler = SubagentScheduler(manager)

    def start_scheduler() -> None:
        # Session-scoped: the store is built once the session id is available
        # (mirrors pi, which constructs it in session_start). Scheduling is
        # non-essential — swallow failures so an unwritable .tau/ can't break
        # the rest of the extension.
        try:
            session_id = tau.context.session_id
            if not session_id:
                return  # id not yet available — retry on the next session_start
            store = ScheduleStore(resolve_store_path(tau.context.cwd, session_id))
            scheduler.start(store)
        except Exception:  # noqa: BLE001 - scheduling must never break the session
            pass

    # Component seam (experimental): the extension owns the agents strip and the
    # conversation viewer as Textual widgets mounted through the component
    # bridge. Installed lazily on session_start — NOT in setup() — because the
    # host attaches its UI bridge (making supports_components True) only after
    # CodingSession.load has already run setup() with the print-mode NullUiBridge.
    ui_controller: list[object] = []  # 0 or 1 element (a nonlocal-friendly cell)

    def _install_ui_components() -> None:
        # getattr-guard keeps the extension loadable on an older tau without the
        # component seam (constraint 8): it then runs dialog-only, no strip.
        components = getattr(getattr(tau, "context", None), "ui", None)
        components = getattr(components, "components", None)
        if components is None or not getattr(components, "supports_components", False):
            return
        # Defensive reinstall (bug fix 2): a controller can survive into the next
        # bind if a session_start arrives without a matching shutdown (or the host
        # force-cleared its slots on rebind). Tear the stale one down and mount
        # fresh widgets against the host's current slots. The host sequences the
        # same-tick teardown+reinstall, so the strip's slot never collides on its
        # id (which used to raise DuplicateIds and drop the strip).
        existing = _current_controller()
        if existing is not None:
            existing.teardown()
            ui_controller.clear()
        from .ui import SubagentUiController

        controller = SubagentUiController(manager, components)
        controller.install()
        manager.sources_changed = controller.on_change
        ui_controller.append(controller)

    def _current_controller():  # noqa: ANN202 - SubagentUiController | None
        return ui_controller[0] if ui_controller else None

    async def on_session_start(event: SessionStartEvent) -> None:
        del event
        if not scheduler.is_active():
            start_scheduler()
        _install_ui_components()

    async def run_agent_tool(arguments, signal=None, *, on_update=None):  # noqa: ANN001, ANN202
        await manager.evict_stale()
        if arguments.get("schedule"):
            return await run_schedule(arguments)
        resume_id = arguments.get("resume")
        if resume_id:
            return await run_resume(str(resume_id), arguments, on_update)

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
        inherit = arguments.get("inherit_context")
        if inherit is None:
            inherit = definition.inherit_context
        if inherit:
            # Captured at spawn time (pi semantics): the digest reflects the
            # parent conversation as of this tool call, even for queued runs.
            context = getattr(tau, "context", None)
            transcript = (
                getattr(context, "transcript", None) if context is not None else None
            )
            if transcript is None:
                return _tool_result(
                    "agent",
                    ok=False,
                    content="inherit_context requires a Tau build with the"
                    " parent-context seam (the parent transcript is not"
                    " exposed to extensions here).",
                )
            parent_context = build_parent_context(transcript)
            if parent_context:
                prompt = parent_context + prompt
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
                # A "background" card: the row confirms the spawn in one dim
                # line; completion arrives later as a notification card.
                details={
                    "status": "background",
                    "agent_id": run.agent_id,
                    "queued": run.status == "queued",
                    "output_file": run.output_file,
                },
            )

        async def cancel_child() -> None:
            """Stop the child and settle its record (pi's parent-abort cascade)."""
            if run.session is not None:
                run.session.cancel()
            assert run.task is not None
            run.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await run.task
            await manager.close_run(run)
            # A cancel that lands before the task first runs never reaches
            # _run_agent's CancelledError handler; settle the record here so
            # the roster and the result agree the run is over.
            if run.status not in TERMINAL_STATUSES:
                run.status = "cancelled"
            if run.completed_at is None:
                run.completed_at = time.monotonic()

        # Live stats ticker: only valid while this tool call is executing, so
        # foreground-only. The try/finally clears it even when tau hard-cancels
        # this coroutine mid-wait.
        run.on_update = on_update
        assert run.task is not None
        try:
            while not run.task.done():
                if signal is not None and signal.is_cancelled():
                    await cancel_child()
                    if run.status in ("completed", "steered"):
                        # The run beat the cancel to the finish line — report
                        # the real result, not a phantom cancellation.
                        return _foreground_result(run)
                    return _tool_result(
                        "agent",
                        ok=False,
                        content="Subagent cancelled",
                        # Cancelled runs stay in the card family (∅ cancelled).
                        details=build_notification_details(
                            run, FOREGROUND_RESULT_CHARS
                        ),
                    )
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            # Esc in the TUI: tau hard-cancels this tool coroutine (the event
            # stream is torn down before the 50ms poll can see the signal), so
            # the cooperative branch above never runs. Take the child down
            # with us — otherwise it keeps running as a zombie and its result
            # is silently dropped. Background runs are untouched by design:
            # they never pass through this wait loop (pi wires the parent
            # abort signal on the foreground path only).
            await cancel_child()
            raise
        finally:
            run.on_update = None

        return _foreground_result(run)

    async def run_resume(agent_id: str, arguments, on_update=None) -> AgentToolResult:  # noqa: ANN001
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
        run.on_update = on_update
        run.last_progress = ""
        try:
            await manager.resume(run, prompt)
        finally:
            run.on_update = None
        return _foreground_result(run)

    async def run_schedule(arguments) -> AgentToolResult:  # noqa: ANN001
        # Guards mirror pi: a scheduled job creates a fresh background agent at
        # fire time, so it is incompatible with resume, inherit_context, and a
        # foreground run.
        if arguments.get("resume"):
            return _tool_result(
                "agent",
                ok=False,
                content="Cannot combine `schedule` with `resume` —"
                " schedules create fresh agents.",
            )
        if arguments.get("inherit_context"):
            return _tool_result(
                "agent",
                ok=False,
                content="Cannot combine `schedule` with `inherit_context` —"
                " there is no parent conversation at fire time.",
            )
        if arguments.get("run_in_background") is False:
            return _tool_result(
                "agent",
                ok=False,
                content="Cannot combine `schedule` with `run_in_background: false`"
                " — scheduled jobs always run in background.",
            )
        if not scheduler.is_active():
            return _tool_result(
                "agent",
                ok=False,
                content="Scheduler is not active in this session yet."
                " Try again after the session has fully started.",
            )
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
        isolation = "worktree" if arguments.get("isolation") == "worktree" else None
        try:
            job = scheduler.add_job(
                name=description,
                description=description,
                schedule=str(arguments.get("schedule")),
                subagent_type=agent_type,
                prompt=prompt,
                model=str(arguments.get("model")) if arguments.get("model") else None,
                thinking=thinking,
                max_turns=_coerce_max_turns(arguments.get("max_turns")),
                isolation=isolation,
            )
        except (ValueError, RuntimeError) as exc:
            return _tool_result("agent", ok=False, content=str(exc))
        next_run = scheduler.get_next_run(job.id) or "(unknown)"
        return _tool_result(
            "agent",
            ok=True,
            content=(
                f'Scheduled "{job.name}" (id: {job.id}, type: {job.schedule_type}).'
                f" Next run: {next_run}."
                " Manage via /agents -> Scheduled jobs."
            ),
        )

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
            manager.cancel_nudge(agent_id)
            while run.status in ("running", "queued"):
                await asyncio.sleep(0.05)
        header = format_run_summary(run)
        header += f"\nUsage: {format_usage_parts(run)}"
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
        manager.cancel_nudge(agent_id)
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
        state_parts = []
        if run.has_usage:
            state_parts.append(f"{lifetime_tokens(run)} tokens")
        state_parts.append(f"{run.tool_calls} tool uses")
        with contextlib.suppress(Exception):
            state_parts.append(
                f"~{run.session.context_token_estimate} context tokens"
            )
        return _tool_result(
            "steer_subagent",
            ok=True,
            content=f"Steering message sent to agent {agent_id}."
            " The agent will process it after its current tool execution.\n"
            f"Current state: {' · '.join(state_parts)}",
        )

    menu_tasks: set[asyncio.Task[None]] = set()

    def agents_command(args: str, context) -> str | None:  # noqa: ANN001
        del args, context
        ui = getattr(getattr(tau, "context", None), "ui", None)
        if supports_menu(ui):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                # Sync handlers can't await dialogs; drive the menu from a
                # loop task (the documented ui-dialogs pattern). Return no
                # message: any text would open a modal the user must dismiss
                # before the menu appears.
                task = loop.create_task(
                    show_agents_menu(
                        manager, ui, scheduler, controller=_current_controller()
                    )
                )
                menu_tasks.add(task)
                task.add_done_callback(menu_tasks.discard)
                return None
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
        scheduler.stop()
        controller = _current_controller()
        if controller is not None:
            # pi parity: the extension clears its own widgets on shutdown (the
            # host also force-clears as a safety net on the next bridge install).
            controller.teardown()
            ui_controller.clear()
        await manager.shutdown()
        manager.runs.clear()
        manager._notify_sources()  # noqa: SLF001 - same-module teardown signal

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
                " continue a finished agent's session. Use inherit_context if"
                " the agent needs the parent conversation history.\n\nAvailable"
                f" agent types:\n{type_list}"
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
                    "inherit_context": {
                        "type": "boolean",
                        "description": "If true, prepend the parent conversation"
                        " history to the agent's prompt. Default: false"
                        " (fresh context).",
                    },
                    "schedule": {
                        "type": "string",
                        "description": "Opt-in only — fire later instead of now."
                        " Omit to run immediately (the default, almost always"
                        ' correct). Formats: 5-field cron ("0 9 * * 1" = 9am Mon),'
                        ' interval ("5m"/"1h"), one-shot ("+10m" or ISO). Forces'
                        " run_in_background; incompatible with inherit_context and"
                        " resume. Returns a job ID.",
                    },
                },
                "required": ["prompt", "description"],
            },
            executor=run_agent_tool,
            prompt_snippet="Spawn an autonomous subagent for delegated tasks.",
            render_call=render_agent_call,
            render_result=render_agent_result,
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
            render_call=render_get_result_call,
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
            render_call=render_steer_call,
        )
    )
    tau.register_command(
        "agents",
        agents_command,
        description="List subagent types and runs.",
    )
    tau.on("session_start", on_session_start)
    tau.on("session_shutdown", on_shutdown)


def _foreground_result(run: AgentRun) -> AgentToolResult:
    details = build_notification_details(run, FOREGROUND_RESULT_CHARS)
    if run.status in ("completed", "steered"):
        content = run.result_text or "(subagent produced no output)"
        duration = _duration_ms(run)
        seconds = f"{duration / 1000:.1f}" if duration is not None else "?"
        if run.has_usage:
            tokens_note = f", {lifetime_tokens(run)} tokens"
        elif run.context_tokens:
            tokens_note = f", ~{run.context_tokens} context tokens"
        else:
            tokens_note = ""
        completed_line = (
            f"Agent completed in {seconds}s"
            f" ({run.tool_calls} tool uses{tokens_note})."
        )
        return _tool_result(
            "agent",
            ok=True,
            content=f"{format_run_summary(run)}\n{completed_line}\n\n{content}",
            details=details,
        )
    return _tool_result(
        "agent",
        ok=False,
        content=f"{format_run_summary(run)}\n\n{run.error or 'subagent failed'}",
        details=details,
    )


def _coerce_max_turns(value) -> int | None:  # noqa: ANN001
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _tool_result(
    name: str,
    *,
    ok: bool,
    content: str,
    details: dict | None = None,
) -> AgentToolResult:
    return AgentToolResult(
        tool_call_id="",
        name=name,
        ok=ok,
        content=content,
        details=details,
        error=None if ok else content,
    )
