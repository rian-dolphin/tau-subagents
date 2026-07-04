"""Custom rendering for <task-notification> messages, ported from pi.

Registered as the "subagent-notification" renderer through Tau's
message-renderers seam (`register_message_renderer`). The renderer reads the
details dict built by `extension.build_notification_details` (pi's
buildNotificationDetails) and returns Rich markup; the raw XML content still
enters the model's context unchanged. Group notifications carry the extra
runs under details["others"], rendered as stacked blocks like pi.
"""

from __future__ import annotations

FAILURE_STATUSES = ("error", "stopped", "aborted", "cancelled")
EXPANDED_RESULT_LINES = 30
COLLAPSED_PREVIEW_CHARS = 80


def render_notification(view: object, options: object) -> str | None:
    """Render a subagent-notification message; None falls back to raw XML."""
    details = getattr(view, "details", None)
    if not isinstance(details, dict):
        return None
    expanded = bool(getattr(options, "expanded", False))
    others = details.get("others")
    blocks = [details, *(others if isinstance(others, list) else [])]
    rendered = [
        _render_one(block, expanded) for block in blocks if isinstance(block, dict)
    ]
    return "\n".join(rendered) if rendered else None


def _render_one(details: dict, expanded: bool) -> str:  # noqa: ANN001
    status = str(details.get("status", ""))
    is_error = status in FAILURE_STATUSES
    icon = "[red]✗[/red]" if is_error else "[green]✓[/green]"
    if is_error:
        status_text = status
    elif status == "steered":
        status_text = "completed (steered)"
    else:
        status_text = "completed"
    description = _escape(str(details.get("description", "")))
    line = f"{icon} [bold]{description}[/bold] [dim]{status_text}[/dim]"

    stats = _stat_parts(details)
    if stats:
        line += "\n  [dim]" + " · ".join(stats) + "[/dim]"

    preview = str(details.get("result_preview") or "No output.")
    if expanded:
        for preview_line in preview.split("\n")[:EXPANDED_RESULT_LINES]:
            line += f"\n  [dim]{_escape(preview_line)}[/dim]"
    else:
        first = preview.split("\n")[0][:COLLAPSED_PREVIEW_CHARS]
        line += f"\n  [dim]⎿  {_escape(first)}[/dim]"

    output_file = details.get("output_file")
    if output_file:
        line += f"\n  [dim]transcript: {_escape(str(output_file))}[/dim]"
    return line


def _stat_parts(details: dict) -> list[str]:  # noqa: ANN001
    parts: list[str] = []
    turns = int(details.get("turn_count") or 0)
    max_turns = details.get("max_turns")
    if turns > 0:
        parts.append(f"{turns}/{max_turns} turns" if max_turns else _plural(turns, "turn"))
    tools = int(details.get("tool_uses") or 0)
    if tools > 0:
        parts.append(_plural(tools, "tool use"))
    tokens = int(details.get("total_tokens") or 0)
    if tokens > 0:
        parts.append(_format_tokens(tokens))
    duration = int(details.get("duration_ms") or 0)
    if duration > 0:
        parts.append(f"{duration / 1000:.1f}s")
    return parts


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _format_tokens(total: int) -> str:
    if total >= 1000:
        return f"{total / 1000:.1f}k tokens"
    return f"{total} tokens"


def _escape(text: str) -> str:
    """Escape Rich markup brackets in run-provided text."""
    return text.replace("[", r"\[")
