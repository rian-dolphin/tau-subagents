"""Custom rendering for subagent completions, ported from pi.

Two renderers share one card vocabulary (status glyph, stats line, ⎿ preview,
transcript path when expanded):

- `render_notification` — the "subagent-notification" message renderer
  (Tau's message-renderers seam, `register_message_renderer`) for background
  completions. It reads the details dict built by
  `extension.build_notification_details` (pi's buildNotificationDetails); the
  raw XML content still enters the model's context unchanged. Group
  notifications carry the extra runs under details["others"], rendered as
  stacked blocks like pi.
- `render_agent_result` — the agent tool's `render_result` hook (pi's
  ToolDefinition.renderResult) for foreground completions. It renders the
  compact card below the tool row's invocation line, so foreground and
  background finishes read as the same family.
"""

from __future__ import annotations

from rich.markup import escape as _escape

FAILURE_STATUSES = ("error", "stopped", "aborted", "cancelled")
# User-initiated stops render neutral-dim (pi's "■ Stopped"), not failure-red:
# the user asked for it, nothing went wrong.
USER_STOP_STATUSES = ("cancelled", "stopped")
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


def render_agent_result(result: object, *, expanded: bool) -> str | None:
    """Render the agent tool's result as a completion card (pi's renderResult).

    The invocation line (`render_call`) stays above; this renders what goes
    beneath it. Foreground finishes get the compact card — the description is
    already on the invocation line, so unlike the notification card it is not
    repeated. Background spawns get a one-line confirmation. ``None`` (no
    details, e.g. argument-validation failures) falls back to Tau's generic
    result block.
    """
    details = getattr(result, "details", None)
    if not isinstance(details, dict):
        return None
    status = str(details.get("status", ""))
    if status == "background":
        verb = "Queued" if details.get("queued") else "Running"
        agent_id = _escape(str(details.get("agent_id") or "?"))
        line = f"  [dim]⎿  {verb} in background ({agent_id})[/dim]"
        output_file = details.get("output_file")
        if expanded and output_file:
            line += f"\n  [dim]transcript: {_escape(str(output_file))}[/dim]"
        return line
    if not status:
        return None
    icon, status_text = _status_glyph(status)
    line = f"{icon} [dim]" + " · ".join([status_text, *stat_parts(details)]) + "[/dim]"
    return line + _result_body(details, expanded)


def _render_one(details: dict, expanded: bool) -> str:  # noqa: ANN001
    status = str(details.get("status", ""))
    icon, status_text = _status_glyph(status)
    description = _escape(str(details.get("description", "")))
    line = f"{icon} [bold]{description}[/bold] [dim]{status_text}[/dim]"

    stats = stat_parts(details)
    if stats:
        line += "\n  [dim]" + " · ".join(stats) + "[/dim]"
    return line + _result_body(details, expanded)


def _status_glyph(status: str) -> tuple[str, str]:
    """Return (icon markup, status text) for a run status."""
    if status in USER_STOP_STATUSES:
        # ∅ matches the fleet strip's cancelled glyph.
        return "[dim]∅[/dim]", status
    if status in FAILURE_STATUSES:
        return "[red]✗[/red]", status
    if status == "steered":
        # Wrapped up under a turn limit: a cautionary ✓ (pi's warning color).
        return "[yellow]✓[/yellow]", "completed (steered)"
    return "[green]✓[/green]", "completed"


def _result_body(details: dict, expanded: bool) -> str:  # noqa: ANN001
    """The preview/transcript tail shared by both cards.

    Failure previews (the run's error text) render red rather than dim so a
    failed run doesn't read like routine output.
    """
    preview = str(details.get("result_preview") or "No output.")
    status = str(details.get("status", ""))
    is_failure = status in FAILURE_STATUSES and status not in USER_STOP_STATUSES
    style = "red" if is_failure else "dim"
    lines = preview.split("\n")
    body = ""
    if expanded:
        for preview_line in lines[:EXPANDED_RESULT_LINES]:
            body += f"\n  [{style}]{_escape(preview_line)}[/{style}]"
        hidden = len(lines) - EXPANDED_RESULT_LINES
        if hidden > 0:
            body += (
                f"\n  [dim]… {_plural(hidden, 'more line')}"
                " — get_subagent_result for full output[/dim]"
            )
        # The transcript path is long and rarely needed at a glance; it only
        # appears here in the expanded view.
        output_file = details.get("output_file")
        if output_file:
            body += f"\n  [dim]transcript: {_escape(str(output_file))}[/dim]"
    else:
        first = _ellipsize(lines[0], COLLAPSED_PREVIEW_CHARS)
        if len(lines) > 1 and not first.endswith("…"):
            first += "…"
        body += f"\n  [{style}]⎿  {_escape(first)}[/{style}]"
    return body


def stat_parts(details: dict) -> list[str]:  # noqa: ANN001
    """Stats vocabulary shared by the cards and the live ticker."""
    parts: list[str] = []
    turns = int(details.get("turn_count") or 0)
    max_turns = details.get("max_turns")
    if turns > 0:
        parts.append(_plural(turns, "turn"))
        if max_turns:
            # "(max N)", not "x/N": the limit is a budget, and "3/8" reads
            # like progress toward a known total.
            parts[-1] += f" (max {max_turns})"
    tools = int(details.get("tool_uses") or 0)
    if tools > 0:
        parts.append(_plural(tools, "tool use"))
    tokens = int(details.get("total_tokens") or 0)
    if tokens > 0:
        parts.append(_format_tokens(tokens))
    duration = int(details.get("duration_ms") or 0)
    if duration > 0:
        parts.append(_format_duration(duration))
    return parts


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _format_duration(ms: int) -> str:
    """Match the running tool row's timer: 50.9s under a minute, then 1m 23s."""
    seconds = ms / 1000
    if seconds < 59.95:  # the .1f rounding threshold, so "60.0s" never prints
        return f"{seconds:.1f}s"
    # round() (not int()) so 59.96s becomes "1m 0s", never "0m 59s".
    minutes, secs = divmod(round(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def _ellipsize(text: str, max_chars: int) -> str:
    """Cut at a word boundary and mark the cut with an ellipsis."""
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    head, _, _ = cut.rpartition(" ")
    return (head or cut).rstrip() + "…"


def _format_tokens(total: int) -> str:
    if total >= 1000:
        return f"{total / 1000:.1f}k tokens"
    return f"{total} tokens"


