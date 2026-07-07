"""The fleet strip: a below-prompt slot widget listing active subagent runs.

Ports pi-subagents' ``fleet-list.ts`` onto tau's component seam, blended with
the rendering feel of tau core's old ``_render_agent_strip``. One row for
``main`` plus one per active/lingering run; a braille spinner marks running
runs and richer statuses (``steered``/``aborted``) render their own glyph
directly (no down-mapping onto the old five-status vocabulary).

Navigation follows pi's fleet-list model. The strip NEVER takes Textual focus:
the prompt keeps focus throughout, and the controller's pre-dispatch key
interceptor (:meth:`~tau_subagents.ui.controller.SubagentUiController._intercept_key`)
owns the whole nav state machine. This widget only holds the visual nav state
(``_focused_nav`` + ``_selected_index``) and exposes small mutators the
controller drives — ``left``/``down`` at an empty prompt activate nav, arrows
move the selection, ``enter`` opens an agent's viewer, ``esc`` / up-past-top
deactivates. A mouse click still works with no focus (:meth:`on_click`).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Callable

from rich.console import Group
from rich.text import Text
from textual.widgets import Static

if TYPE_CHECKING:
    from textual import events
    from tau_coding.tui.config import TuiTheme

    from ..extension import AgentRun, SubagentManager

# Max agent rows shown at once; extras collapse into a "… N more — /agents" line
# (matches tau core's AGENT_STRIP_MAX_ROWS).
STRIP_MAX_ROWS = 4
# How long a finished run lingers in the strip before it drops off (pi's
# FINISHED_LINGER_MS). It stays reachable via /agents afterwards.
FINISHED_LINGER_SECONDS = 4.0
# Re-render cadence so the running spinner animates and lingering rows expire.
SPINNER_INTERVAL = 0.2
_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

ACTIVE_STATUSES = ("running", "queued")

# AgentRun status → strip glyph. "running" is special-cased (spinner); the rest
# render directly, so steered/aborted keep their own identity (the seam review's
# "no more down-mapping").
STATUS_GLYPHS = {
    "queued": "◌",
    "completed": "✓",
    "steered": "↻",
    "aborted": "⊘",
    "error": "✗",
    "cancelled": "∅",
}


def _lifetime_tokens(run: AgentRun) -> int:
    """Lifetime billed tokens (mirrors extension.lifetime_tokens without importing it)."""
    return run.tokens_input + run.tokens_output + run.tokens_cache_write


def format_elapsed(run: AgentRun) -> str:
    """`11s` — integer seconds, freezing once the run finishes (pi parity)."""
    if run.started_at is None:
        return "queued"
    end = run.completed_at if run.completed_at is not None else time.monotonic()
    return f"{max(0, round(end - run.started_at))}s"


def format_tokens(count: int) -> str:
    """`↓ 13.1k tokens` — compact magnitude with a down-arrow prefix (pi parity)."""
    if count >= 1_000_000:
        compact = f"{count / 1_000_000:.1f}M"
    elif count >= 1_000:
        compact = f"{count / 1_000:.1f}k"
    else:
        compact = str(count)
    return f"↓ {compact} tokens"


class AgentStripWidget(Static):
    """Below-prompt widget showing ``main`` + active subagent runs.

    Never takes focus: the controller's pre-dispatch key interceptor drives the
    nav state held here (``_focused_nav`` / ``_selected_index``) via the mutators
    below, so the prompt keeps focus throughout (pi's fleet-list model).
    """

    DEFAULT_CSS = """
    AgentStripWidget {
        height: auto;
        max-height: 8;
    }
    """

    def __init__(
        self,
        manager: SubagentManager,
        theme: TuiTheme,
        *,
        open_conversation: Callable[[AgentRun], bool],
        close_conversation: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__("", id="subagents-fleet-strip")
        self._manager = manager
        self._theme = theme
        self._open_conversation = open_conversation
        # Closes an open viewer if one is up; returns True if it did. Lets the
        # `main` row double as "back to main" while a viewer is open.
        self._close_conversation = close_conversation
        # 0 = main, 1..N = the agent at roster position N.
        self._selected_index = 0
        self._focused_nav = False
        self._spinner_frame = 0
        # id of the run whose viewer is currently open (● accent marker).
        self.viewing_id: str | None = None
        # Populated each render() so on_click can map a line offset back to a run
        # (None marks the main row / non-row lines).
        self._row_runs: list[AgentRun | None] = []

    # ---- Lifecycle --------------------------------------------------------

    def on_mount(self) -> None:
        """Start the spinner/linger tick; refresh reflects live manager state."""
        self.set_interval(SPINNER_INTERVAL, self._tick)

    def _tick(self) -> None:
        """Advance the spinner and re-render (cheap; render reads live state).

        Uses ``layout=True`` because a lingering run can expire between ticks,
        shrinking the row count — a plain ``refresh()`` would leave a stale
        height (the same class of bug as the mount-empty case).
        """
        if self._agent_runs():
            self._spinner_frame = (self._spinner_frame + 1) % len(_SPINNER_FRAMES)
        self.refresh(layout=True)

    def refresh_roster(self) -> None:
        """Re-render after the run list or a status changed (controller push).

        ``layout=True`` is mandatory: the strip mounts empty (height 0) and a
        plain ``refresh()`` never re-measures, so a widget that gains its first
        row would stay invisible. Any refresh that can change the row count
        must relayout.
        """
        self.refresh(layout=True)

    # ---- Roster -----------------------------------------------------------

    def _agent_runs(self) -> list[AgentRun]:
        """Runs shown in the strip, earliest-launched first (pi's agentRecords()).

        Included: running/queued, the currently-viewed run, and recently-finished
        runs during their linger window. Finished runs then drop off (still
        reachable via /agents).
        """
        now = time.monotonic()
        runs = [
            run
            for run in self._manager.runs.values()
            if run.status in ACTIVE_STATUSES
            or run.agent_id == self.viewing_id
            or (
                run.completed_at is not None
                and now - run.completed_at < FINISHED_LINGER_SECONDS
            )
        ]
        runs.sort(key=lambda run: (run.started_at if run.started_at is not None else 0.0))
        return runs

    def has_agents(self) -> bool:
        """Whether the strip currently has any agent row (gates strip entry)."""
        return bool(self._agent_runs())

    def _roster_len(self) -> int:
        """Total selectable rows: main + agents."""
        return 1 + len(self._agent_runs())

    # ---- Nav state (driven by the controller's key interceptor) -----------

    @property
    def nav_active(self) -> bool:
        """Whether arrow keys currently navigate the strip (vs. flow to prompt)."""
        return self._focused_nav

    @property
    def selected_index(self) -> int:
        """Current selection: 0 = ``main`` row, 1..N = agent rows."""
        return self._selected_index

    def activate_nav(self) -> None:
        """Turn nav on and select the top (``main``) row."""
        self._focused_nav = True
        self._selected_index = 0
        self.refresh()

    def deactivate_nav(self) -> None:
        """Turn nav off and drop the highlight (prompt keeps focus regardless)."""
        self._focused_nav = False
        self._selected_index = 0
        self.refresh()

    def move_selection(self, delta: int) -> None:
        """Move the selection by ``delta``, clamped to the roster (no wrap)."""
        self._selected_index = max(
            0, min(self._roster_len() - 1, self._selected_index + delta)
        )
        self.refresh()

    def selected_run(self) -> AgentRun | None:
        """The run at the current selection, or ``None`` for the ``main`` row."""
        agents = self._agent_runs()
        index = self._selected_index
        if index <= 0 or index - 1 >= len(agents):
            return None
        return agents[index - 1]

    # ---- Mouse ------------------------------------------------------------

    def on_click(self, event: events.Click) -> None:
        """Click a row to open its viewer; needs no focus (pi/tau parity).

        The ``main`` row closes an open viewer (back to main), else deactivates
        nav; an agent row selects and opens.
        """
        line = int(event.y)
        if 0 <= line < len(self._row_runs):
            run = self._row_runs[line]
            if run is None:
                if self._close_conversation is None or not self._close_conversation():
                    self.deactivate_nav()
                return
            # Reflect the click in the selection, then open.
            agents = self._agent_runs()
            if run in agents:
                self._selected_index = agents.index(run) + 1
            self._focused_nav = True
            self._open_conversation(run)

    # ---- Rendering --------------------------------------------------------

    def render(self) -> Group:
        """Render main + windowed agent rows, an overflow line, and a hint."""
        agents = self._agent_runs()
        self._row_runs = []
        if not agents:
            # No agents → render nothing (the strip is effectively hidden, pi parity).
            return Group()

        theme = self._theme
        rows: list[Text] = []
        sel = min(self._selected_index, self._roster_len() - 1)

        # main row (roster index 0)
        rows.append(self._render_row(0, sel, glyph="", label="main", detail="", run=None))
        self._row_runs.append(None)

        # Window agent rows so the selected one stays visible.
        visible = min(STRIP_MAX_ROWS, len(agents))
        sel_agent = max(0, sel - 1)
        start = 0 if sel_agent < visible else sel_agent - visible + 1
        window = agents[start : start + visible]
        for offset, run in enumerate(window):
            roster_index = start + offset + 1
            rows.append(
                self._render_row(
                    roster_index,
                    sel,
                    glyph=self._glyph_for(run),
                    label=run.agent_type,
                    detail=run.description,
                    run=run,
                )
            )
            self._row_runs.append(run)

        hidden = len(agents) - len(window)
        if hidden > 0:
            rows.append(
                Text(
                    f"    … {hidden} more — /agents",
                    style=theme.muted_text,
                    no_wrap=True,
                    overflow="ellipsis",
                )
            )
        hint = (
            "↑ ↓ select · enter view · esc back"
            if self._focused_nav
            else "← agents"
        )
        rows.append(
            Text(f"    {hint}", style=theme.muted_text, no_wrap=True, overflow="ellipsis")
        )
        return Group(*rows)

    def _glyph_for(self, run: AgentRun) -> str:
        if run.status == "running":
            return _SPINNER_FRAMES[self._spinner_frame]
        return STATUS_GLYPHS.get(run.status, "○")

    def _render_row(
        self,
        index: int,
        sel: int,
        *,
        glyph: str,
        label: str,
        detail: str,
        run: AgentRun | None,
    ) -> Text:
        theme = self._theme
        selected = index == sel
        viewing = run is not None and run.agent_id == self.viewing_id
        main_row = run is None and index == 0
        row = Text(no_wrap=True, overflow="ellipsis")
        row.append("❯ " if selected else "  ", style=theme.accent)
        # A ● accent dot marks the row being viewed (and main when nothing is viewed).
        if viewing or (main_row and self.viewing_id is None):
            row.append("● ", style=theme.accent)
        elif main_row:
            row.append("○ ", style=theme.muted_text)
        else:
            glyph_style = (
                theme.role_styles["error"].border
                if run is not None and run.status == "error"
                else theme.muted_text
            )
            row.append(f"{glyph} ", style=glyph_style)
        active = viewing or (main_row and self.viewing_id is None)
        label_style = theme.prompt_text if active else theme.muted_text
        row.append(label, style=f"bold {label_style}" if selected else label_style)
        if detail:
            row.append(f"  {detail}", style=theme.muted_text)
        if run is not None:
            tokens = _lifetime_tokens(run)
            stat = format_elapsed(run)
            if run.has_usage and tokens:
                stat += f" · {format_tokens(tokens)}"
            row.append(f"  {stat}", style=theme.muted_text)
        return row
