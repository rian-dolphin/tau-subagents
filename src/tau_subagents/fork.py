"""Fork subagents: children that continue the parent conversation.

A fork (Claude Code's `fork` subagent type) inherits the parent session's
system prompt, model, and full message history — real messages, not the
text digest `inherit_context` builds. The capability rests on Tau's
append-only session storage: `CodingSession.load` replays whatever entries
the storage already holds, so seeding the child's in-memory storage with
the parent transcript hands it the history natively. The principled
upstream seam would be a `CodingSessionConfig` seed-entries field; storage
seeding is the honest interim (ADR 0005).

The capture happens at tool-call time (matching `inherit_context`
semantics for queued runs). Only `SessionInfoEntry` plus chained
`MessageEntry` rows are seeded — no model or thinking entries, since
`state.model` would override the provider the manager actually built;
model fidelity lives in provider selection instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from tau_agent.messages import AgentMessage, ToolResultMessage
from tau_agent.session import MessageEntry, SessionEntry, SessionInfoEntry

if TYPE_CHECKING:
    from pathlib import Path

FORK_FILLER_RESULT = "(forked here — this call's result is not part of the fork)"

# The parent system prompt is kept byte-identical (appending anything there
# would break the shared prompt cache), so the fork framing rides on the task
# prompt instead.
_FORK_TASK_TEMPLATE = """<fork_task>
You are a fork of the conversation above and share its full context. Only
your final message is returned to the parent session.
{worktree_note}
{prompt}
</fork_task>"""

_WORKTREE_NOTE = """You are working in an isolated git worktree copy of the
repository. Use paths relative to your working directory — absolute paths
from the conversation above point at the parent checkout, not your copy.
"""


@dataclass(frozen=True, slots=True)
class ForkCapture:
    """Parent-session state captured when a fork is spawned."""

    system_prompt: str
    model: str
    provider_name: str
    messages: tuple[AgentMessage, ...]


def capture_fork(context) -> ForkCapture:  # noqa: ANN001 - ExtensionContext
    """Snapshot the parent session for a fork spawn.

    `context.transcript` already deep-copies each message. The trailing
    assistant message carries the fork's own `agent` tool call with no result
    yet; a dangling tool_use breaks provider requests, and Tau's own repair
    would fill it with an is_error "interrupted" message — so close every
    unanswered call here with a neutral filler instead.
    """
    messages = _close_dangling_tool_calls(list(context.transcript))
    return ForkCapture(
        system_prompt=context.system_prompt,
        model=context.model,
        provider_name=context.provider_name,
        messages=tuple(messages),
    )


def build_fork_entries(capture: ForkCapture, cwd: Path) -> list[SessionEntry]:
    """Seed entries for the fork's storage: session info + chained messages.

    The `parent_id` chain is load-bearing: the child's first persisted turn
    triggers a leaf-path replay, and unchained entries would be silently
    dropped by Tau's missing-parent detachment.
    """
    info = SessionInfoEntry(cwd=str(cwd))
    entries: list[SessionEntry] = [info]
    parent_id = info.id
    for message in capture.messages:
        entry = MessageEntry(parent_id=parent_id, message=message)
        entries.append(entry)
        parent_id = entry.id
    return entries


def wrap_fork_task(prompt: str, *, worktree: bool = False) -> str:
    """Wrap the fork's task prompt in its framing block."""
    return _FORK_TASK_TEMPLATE.format(
        worktree_note=_WORKTREE_NOTE if worktree else "",
        prompt=prompt,
    )


def _close_dangling_tool_calls(
    messages: list[AgentMessage],
) -> list[AgentMessage]:
    """Interleave fillers right after their calls, matching Tau's repair order."""
    answered = {
        message.tool_call_id
        for message in messages
        if isinstance(message, ToolResultMessage)
    }
    repaired: list[AgentMessage] = []
    for message in messages:
        repaired.append(message)
        repaired.extend(
            ToolResultMessage(
                tool_call_id=call.id,
                tool_name=call.name,
                content=FORK_FILLER_RESULT,
            )
            for call in getattr(message, "tool_calls", ())
            if call.id not in answered
        )
    return repaired
