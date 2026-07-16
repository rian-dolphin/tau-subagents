"""The conversation viewer: a main-area view of one subagent's live transcript.

Ports pi-subagents' ``conversation-viewer.ts`` onto tau's component seam. Opened
via ``context.ui.components.open_main_view`` (an in-tree, display-toggled main
view — NOT a modal — so the fleet strip stays visible for peripheral awareness).

Rendering deliberately REUSES tau core's own transcript internals rather than
reinventing them: it feeds a :class:`tau_coding.tui.state.TuiState` (via
``load_messages``) into a :class:`tau_coding.tui.widgets.TranscriptView` and
calls ``update_from_state`` — exactly the ``TuiState`` + ``#agent-transcript-pane``
mechanics tau core's ``_activate_source``/``_tick_agent_view`` used before this
migration. Importing those host internals is the coupling this experiment
measures; it is called out here because it is non-obvious.

Live updates are push, not poll: the viewer subscribes to the run's listener
registry on mount and unsubscribes on unmount (the analog of pi's
``session.subscribe(() => tui.requestRender())``). Runs execute as asyncio tasks
on the TUI event loop, so a listener calling widget methods runs on the UI
thread and is safe (see the design's push-refresh invariant).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from rich.text import Text
from textual.containers import Vertical
from textual.widgets import Input, Static

# Host-internal rendering reuse (the measured coupling): the viewer renders an
# agent's conversation through the very same state + transcript widget the host
# uses for the main chat, so message/tool/thinking formatting stays identical.
from tau_coding.tui.config import TuiKeybindings
from tau_coding.tui.state import TuiState
from tau_coding.tui.widgets import TranscriptView

from tau_agent.messages import UserMessage

from ..agents_menu import run_snapshot_messages, steer_run, stop_run

if TYPE_CHECKING:
    from textual import events
    from tau_coding.tui.config import TuiTheme

    from ..extension import AgentRun, SubagentManager
    from tau_coding.extensions import MainViewHandle

ACTIVE_STATUSES = ("running", "queued")

# Shown while the composer holds text the finished run can no longer accept.
_STEER_REFUSED_NOTICE = "run finished — steer not sent · Esc close"

_STATUS_GLYPHS = {
    "queued": "◌",
    "running": "●",
    "completed": "✓",
    "steered": "↻",
    "aborted": "⊘",
    "error": "✗",
    "cancelled": "∅",
}


def run_messages(run: AgentRun) -> tuple:
    """Current conversation for a run (live session, else a terminal snapshot).

    Mirrors the old ``run_transcript_source.messages()`` policy so the viewer
    keeps showing a finished run's story after its session closes.
    """
    session = run.session
    if session is not None:
        try:
            return tuple(session.messages)
        except Exception:  # noqa: BLE001 - a closing session must not break the view
            return ()
    if run.status == "queued":
        return (UserMessage(content=run.prompt),)
    return run_snapshot_messages(run)


class _SteerComposer(Input):
    """Single-line steer composer; Esc cancels back to the viewer (pi parity)."""

    def __init__(self, on_cancel: Callable[[], None]) -> None:
        # The id is what binds the DEFAULT_CSS #viewer-composer rules (it was
        # previously constructed without one, leaving that CSS dead).
        super().__init__(
            placeholder="Steer the agent…  Enter send · Esc cancel",
            id="viewer-composer",
        )
        self._on_cancel = on_cancel

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()
            event.prevent_default()
            self._on_cancel()


class ConversationViewer(Vertical):
    """Live transcript + steer composer + two-press stop for one subagent run."""

    can_focus = True

    DEFAULT_CSS = """
    ConversationViewer {
        height: 1fr;
        border: round $tau-border;
        padding: 0 1;
    }
    ConversationViewer > #viewer-header {
        height: auto;
        padding: 0 0 1 0;
    }
    ConversationViewer > #viewer-transcript {
        height: 1fr;
    }
    ConversationViewer > #viewer-composer {
        height: auto;
        margin: 1 0 0 0;
        border: round $tau-accent;
    }
    """

    def __init__(
        self,
        run: AgentRun,
        handle: MainViewHandle,
        manager: SubagentManager,
        theme: TuiTheme,
        *,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(id="subagents-conversation-viewer")
        self._run = run
        self._handle = handle
        self._manager = manager
        self._theme = theme
        # Public so the controller can bind an identity-checked callback after
        # construction (the viewer instance isn't available until then).
        self.on_close = on_close
        self._stop_armed = False
        self._composer: _SteerComposer | None = None
        self._transcript: TranscriptView | None = None
        # Viewer-local expand/collapse for tool results. Each repaint feeds a
        # fresh TuiState into the transcript, so the flag must live here — and
        # it is deliberately independent of the main chat's toggle: expanding
        # a subagent's tool output should not re-render the hidden main view.
        self._show_tool_results = False
        self._header: Static | None = None
        # Last rendered header line, kept for observability/tests.
        self._header_text: Text = Text()
        # Transient header notice (e.g. a steer refused on a finished run);
        # lives exactly as long as the composer that provoked it.
        self._notice: str | None = None

    # ---- Lifecycle --------------------------------------------------------

    def compose(self):
        """Header line, the reused transcript view, then the (empty) composer slot."""
        self._header = Static("", id="viewer-header")
        self._transcript = TranscriptView(
            id="viewer-transcript",
            min_width=1,
            wrap=True,
            highlight=True,
            markup=False,
        )
        yield self._header
        yield self._transcript

    def on_mount(self) -> None:
        """Focus the viewer, subscribe to run push events, and paint once."""
        # open_main_view leaves focus on the prompt; the viewer takes it so its
        # own key handling (esc close, enter steer, x stop, scroll, and the
        # ctrl+o tool-results toggle) works
        # without routing every command through the pre-dispatch interceptor
        # (which stays live for strip nav while the viewer is open).
        self.focus()
        # The transcript inside is rendered by tau's own TranscriptView, so it
        # is pixel-identical to the main chat; the frame title is what tells
        # the user they are looking at a subagent, not tau.
        self.border_title = f"subagent · {self._run.agent_type}"
        self._run.listeners.append(self._on_run_event)
        self._refresh_transcript()
        self._update_header()

    @property
    def composer_active(self) -> bool:
        """Whether the steer composer is up (and owns the keyboard)."""
        return self._composer is not None

    def request_close(self) -> None:
        """Close this view via its host handle (the strip-nav close path)."""
        self._handle.close()

    def on_unmount(self) -> None:
        """Unsubscribe and let the controller forget this viewer."""
        try:
            self._run.listeners.remove(self._on_run_event)
        except ValueError:
            pass
        if self.on_close is not None:
            self.on_close()

    def _on_run_event(self) -> None:
        """Run listener (on the UI loop): repaint transcript + header live."""
        self._reconcile_composer()
        self._refresh_transcript()
        self._update_header()

    def on_external_change(self) -> None:
        """Controller push (roster/status change) — repaint header + transcript."""
        self._reconcile_composer()
        self._refresh_transcript()
        self._update_header()

    def _reconcile_composer(self) -> None:
        """Fold an *empty* composer away when the run stops being steerable.

        Closing an empty composer is lossless clutter removal. A composer with
        text is deliberately left alone — completion arrives between keystrokes
        and yanking the input would destroy what the user typed — but the
        notice goes up immediately, so the composer never sits silently
        inviting a steer that the guarded submit path would refuse.
        """
        composer = self._composer
        if composer is None or self._can_steer():
            return
        if not composer.value.strip():
            self._close_composer()
        elif self._notice is None:
            self._notice = _STEER_REFUSED_NOTICE

    # ---- Steer / stop capability -----------------------------------------

    def _can_steer(self) -> bool:
        return self._run.status in ACTIVE_STATUSES

    def _is_stoppable(self) -> bool:
        return self._run.status in ACTIVE_STATUSES

    # ---- Mouse ------------------------------------------------------------

    def on_click(self, event: events.Click) -> None:
        """Keep the keyboard on this viewer when it is clicked.

        Bubbling would reach the host app's click handler, which (on hosts
        without the main-view guard) refocuses the main prompt — silently
        rerouting esc/ctrl+o/typed text to the main chat while this view is
        open. Clicks on the steer composer keep their native Input focus.
        """
        event.stop()
        if self._composer is None and not self.has_focus:
            self.focus()

    # ---- Keyboard ---------------------------------------------------------

    def on_key(self, event: events.Key) -> None:
        """Viewer commands while focused.

        The composer owns the keyboard while it is up — except the
        tool-results toggle: in the main chat ctrl+o works while typing at
        the prompt, so it keeps working (and stays consumed) mid-steer too.
        """
        if self._composer is not None:
            self._maybe_toggle_tool_results(event)
            return
        key = event.key
        if key in ("escape", "q"):
            event.stop()
            event.prevent_default()
            self._handle.close()
            return
        if key == "enter":
            event.stop()
            event.prevent_default()
            if self._can_steer():
                self._stop_armed = False
                self._open_composer()
            return
        if key == "x":
            event.stop()
            event.prevent_default()
            if self._is_stoppable():
                if self._stop_armed:
                    self._stop_armed = False
                    stop_run(self._run)
                else:
                    self._stop_armed = True
                self._update_header()
            return
        # Any other key disarms a pending stop (pi's guard), then scrolls.
        if self._stop_armed:
            self._stop_armed = False
            self._update_header()
        if self._maybe_toggle_tool_results(event):
            return
        transcript = self._transcript
        if transcript is None:
            return
        if key in ("up", "k"):
            transcript.scroll_up()
            event.stop()
        elif key in ("down", "j"):
            transcript.scroll_down()
            event.stop()
        elif key == "pageup":
            transcript.scroll_page_up()
            event.stop()
        elif key == "pagedown":
            transcript.scroll_page_down()
            event.stop()
        elif key == "home":
            transcript.scroll_home()
            event.stop()
        elif key == "end":
            transcript.scroll_end()
            event.stop()

    def _maybe_toggle_tool_results(self, event: events.Key) -> bool:
        """Apply and consume the tool-results toggle key; False for other keys.

        Consuming matters: letting the key bubble would hit the host app's
        own binding, toggling the hidden main transcript and notifying
        "Tool results expanded." while this view visibly does nothing.
        """
        if event.key != self._toggle_tool_results_key():
            return False
        event.stop()
        event.prevent_default()
        self._show_tool_results = not self._show_tool_results
        self._refresh_transcript()
        return True

    def _toggle_tool_results_key(self) -> str:
        """The host's configured tool-results toggle key (default ctrl+o).

        Read live from the running app's settings so a remapped binding keeps
        working here; hosts without ``tui_settings`` (tests) get the default.
        """
        settings = getattr(self.app, "tui_settings", None)
        keybindings = getattr(settings, "keybindings", None)
        key = getattr(keybindings, "toggle_tool_results", None)
        return key if isinstance(key, str) else TuiKeybindings().toggle_tool_results

    # ---- Steer composer ---------------------------------------------------

    def _open_composer(self) -> None:
        composer = _SteerComposer(on_cancel=self._close_composer)
        composer.border_title = f"steer {self._run.agent_type}"
        self._composer = composer
        self._notice = None
        self.mount(composer)
        composer.focus()

    def _close_composer(self) -> None:
        composer = self._composer
        self._composer = None
        self._notice = None
        if composer is not None:
            composer.remove()
        self.focus()
        self._update_header()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in the composer sends the steering message and closes the composer.

        Steerability is re-checked at submit time, not just at composer-open:
        pending_steers are only drained at session creation, so a steer sent to
        a finished run would silently vanish. Refuse the send, keep the typed
        text on screen, and say why in the header (Esc still closes).
        """
        if self._composer is None or event.input is not self._composer:
            return
        event.stop()
        message = event.value.strip()
        if not message:
            self._close_composer()
            return
        if not self._can_steer():
            self._notice = _STEER_REFUSED_NOTICE
            self._update_header()
            return
        self._close_composer()
        steer_run(self._run, message)

    # ---- Rendering --------------------------------------------------------

    def _refresh_transcript(self) -> None:
        transcript = self._transcript
        if transcript is None:
            return
        state = TuiState()
        state.show_tool_results = self._show_tool_results
        state.load_messages(run_messages(self._run))
        transcript.update_from_state(state, theme=self._theme)

    def _update_header(self) -> None:
        header = self._header
        if header is None:
            return
        run = self._run
        theme = self._theme
        glyph = _STATUS_GLYPHS.get(run.status, "○")
        glyph_style = (
            theme.role_styles["error"].border
            if run.status == "error"
            else theme.accent
            if run.status in ACTIVE_STATUSES
            else theme.muted_text
        )
        line = Text(no_wrap=True, overflow="ellipsis")
        line.append(f"{glyph} ", style=glyph_style)
        line.append(run.agent_type, style=f"bold {theme.prompt_text}")
        line.append(f" [{run.status}]", style=theme.muted_text)
        if self._notice is not None:
            # Ahead of the model/description/hints: the line ellipsizes from
            # the right, and on a narrow terminal the notice is the one part
            # that must survive (it explains why Enter is doing nothing).
            line.append(f"  {self._notice}", style=theme.role_styles["error"].border)
        # Resolved model once the run has one; the requested model while
        # queued. Always shown so "same as the main thread" is visible too.
        model = run.model or run.requested_model
        if model:
            line.append(f" · {model}", style=theme.muted_text)
        if run.description:
            line.append(f"  {run.description}", style=theme.muted_text)
        # Right-hand action hints, mirroring pi's footer affordances.
        hints: list[str] = []
        if self._can_steer():
            hints.append("Enter steer")
        if self._is_stoppable():
            hints.append("x again to STOP" if self._stop_armed else "x stop")
        hints.append("Esc close")
        hint_style = (
            theme.role_styles["error"].border if self._stop_armed else theme.muted_text
        )
        line.append("   ")
        line.append(" · ".join(hints), style=hint_style)
        self._header_text = line
        header.update(line)
