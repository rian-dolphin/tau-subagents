"""Tests for the extension-owned Textual widgets (component seam).

Two layers, per the design's test plan:

* Widget behaviour — mount the strip / viewer in a minimal Textual test ``App``
  and drive them with :class:`~tau_subagents.extension.AgentRun` instances and a
  ``SimpleNamespace`` manager (no full host wiring needed).
* Seam wiring — a ``FakeComponentBridge`` implementing tau's
  :class:`ComponentBridge`, exercising the controller's registrations and the
  key interceptor, plus a runtime-level check that ``setup()`` registers on a
  component host and skips cleanly on a null host.

These re-home the behaviours the deleted core UX pilot tests used to cover
(fills-only-viewed-dot, drops-finished, click-switches, opens+steers,
rejects-finished-steer, rerenders-on-change).
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from textual.app import App, ComposeResult
from textual.events import Key
from textual.widgets import Input

from tau_agent.messages import AssistantMessage, ToolResultMessage, UserMessage
from tau_agent.tools import ToolCall
from tau_coding.tui.app import _theme_css_variables
from tau_coding.tui.config import TAU_DARK_THEME
from tau_coding.tui.widgets import TranscriptMessageWidget

from tau_subagents.extension import AgentRun, SubagentManager
from tau_subagents.ui.controller import STRIP_KEY, SubagentUiController
from tau_subagents.ui.strip import AgentStripWidget
from tau_subagents.ui.viewer import ConversationViewer

# Sibling test module (pytest prepend import mode puts tests/ on sys.path).
from test_extension import RecordingSession, _load_runtime  # noqa: E402

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# --- helpers ---------------------------------------------------------------


def _run(
    agent_id: str = "agent-1",
    *,
    agent_type: str = "explore",
    description: str = "survey",
    status: str = "running",
    started: bool = True,
    finished: bool = False,
    session: object | None = None,
) -> AgentRun:
    run = AgentRun(
        agent_id=agent_id,
        agent_type=agent_type,
        description=description,
        prompt="child prompt",
        background=True,
        status=status,
    )
    if started:
        run.started_at = time.monotonic()
    if finished:
        run.completed_at = time.monotonic()
    run.session = session
    return run


def _manager(*runs: AgentRun) -> SimpleNamespace:
    return SimpleNamespace(runs={run.agent_id: run for run in runs})


class _Harness(App):
    """Minimal app that mounts an ``#prompt`` plus the widgets under test."""

    # Mirrors the real host's (non-priority) app-level toggle binding: it fires
    # only when a ctrl+o leaks past the widgets under test, which in the real
    # app means the hidden main transcript toggles instead of the viewer.
    BINDINGS = [("ctrl+o", "host_toggle_tool_results", "Tool results")]

    def __init__(self, *widgets) -> None:  # noqa: ANN002
        super().__init__()
        self._widgets = widgets
        self.leaked_tool_toggles = 0

    def action_host_toggle_tool_results(self) -> None:
        self.leaked_tool_toggles += 1

    def get_css_variables(self) -> dict[str, str]:
        # The reused TranscriptView (and its markdown widgets) reference tau's
        # $tau-* CSS variables, which the real TauTuiApp provides from the theme.
        variables = super().get_css_variables()
        variables.update(_theme_css_variables(TAU_DARK_THEME))
        return variables

    def compose(self) -> ComposeResult:
        yield Input(id="prompt")
        yield from self._widgets


def _strip_text(strip: AgentStripWidget) -> str:
    group = strip.render()
    return " ".join(getattr(part, "plain", "") for part in group.renderables)


# --- strip rendering -------------------------------------------------------


def test_strip_renders_runs_and_statuses() -> None:
    # Kept within STRIP_MAX_ROWS so every row is visible (no overflow window).
    running = _run("agent-1", agent_type="explore", status="running")
    done = _run("agent-2", agent_type="review", status="completed", finished=True)
    errored = _run("agent-3", agent_type="build", status="error", finished=True)
    manager = _manager(running, done, errored)
    strip = AgentStripWidget(manager, TAU_DARK_THEME, open_conversation=lambda run: True)

    text = _strip_text(strip)
    assert "main" in text
    for label in ("explore", "review", "build"):
        assert label in text
    # Running shows a hollow circle (the animated spinner lives on the agent
    # tool row in the main chat, not the strip); finished statuses render their
    # own glyph. No timer/token stats in strip rows.
    assert "○ explore" in text
    assert "✓" in text  # completed
    assert "✗" in text  # error
    assert "tokens" not in text
    assert "0s" not in text


def test_strip_renders_steered_and_aborted_glyphs() -> None:
    # steered/aborted render directly, no down-mapping onto the old vocabulary.
    steered = _run("agent-1", agent_type="plan", status="steered", finished=True)
    aborted = _run("agent-2", agent_type="test", status="aborted", finished=True)
    strip = AgentStripWidget(
        _manager(steered, aborted), TAU_DARK_THEME, open_conversation=lambda run: True
    )
    text = _strip_text(strip)
    assert "↻" in text  # steered
    assert "⊘" in text  # aborted


def test_strip_drops_finished_agents_after_linger() -> None:
    running = _run("agent-1", agent_type="explore", status="running")
    stale = _run("agent-2", agent_type="review", status="completed")
    # Finished well outside the linger window → dropped from the strip.
    stale.completed_at = time.monotonic() - 3600
    strip = AgentStripWidget(
        _manager(running, stale), TAU_DARK_THEME, open_conversation=lambda run: True
    )

    text = _strip_text(strip)
    assert "explore" in text
    assert "review" not in text


def test_strip_shows_overflow_line() -> None:
    runs = [_run(f"agent-{i}", agent_type=f"t{i}", status="running") for i in range(6)]
    strip = AgentStripWidget(_manager(*runs), TAU_DARK_THEME, open_conversation=lambda r: True)
    text = _strip_text(strip)
    assert "more — /agents" in text


def test_strip_marks_only_the_viewed_row() -> None:
    a = _run("agent-1", agent_type="explore", status="running")
    b = _run("agent-2", agent_type="review", status="running")
    strip = AgentStripWidget(_manager(a, b), TAU_DARK_THEME, open_conversation=lambda r: True)
    # Nothing viewed → the filled ● dot sits on main only.
    assert _strip_text(strip).count("●") == 1
    strip.viewing_id = "agent-2"
    # Viewing an agent moves the single filled dot to it (main goes hollow).
    assert _strip_text(strip).count("●") == 1


# --- strip navigation (controller-driven; the strip never takes focus) -----


async def test_strip_nav_mutators_track_selection() -> None:
    a = _run("agent-1", agent_type="explore", status="running")
    b = _run("agent-2", agent_type="review", status="running")
    strip = AgentStripWidget(_manager(a, b), TAU_DARK_THEME, open_conversation=lambda r: True)
    app = _Harness(strip)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()

        assert strip.nav_active is False
        assert strip.selected_index == 0

        strip.activate_nav()
        assert strip.nav_active is True
        assert strip.selected_index == 0  # highlights the main row
        assert strip.selected_run() is None  # main row → no run

        strip.move_selection(1)
        assert strip.selected_index == 1
        assert strip.selected_run() is a  # first agent row

        strip.move_selection(1)
        assert strip.selected_index == 2
        assert strip.selected_run() is b

        # Clamped at the last row (no wrap).
        strip.move_selection(1)
        assert strip.selected_index == 2

        strip.deactivate_nav()
        assert strip.nav_active is False
        assert strip.selected_index == 0

        # The strip is never focusable — the prompt keeps focus throughout.
        assert strip.can_focus is False
        assert app.query_one("#prompt", Input).has_focus


# --- viewer ----------------------------------------------------------------


class _FakeHandle:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

    @property
    def is_open(self) -> bool:
        return not self.closed


def _viewer_for(run: AgentRun, handle: _FakeHandle | None = None) -> ConversationViewer:
    return ConversationViewer(
        run, handle or _FakeHandle(), _manager(run), TAU_DARK_THEME
    )


async def test_viewer_renders_messages_and_header() -> None:
    session = SimpleNamespace(
        messages=(UserMessage(content="do the thing"), AssistantMessage(content="on it")),
        queue_steering_message=lambda msg: None,
    )
    run = _run("agent-1", agent_type="explore", status="running", session=session)
    run.model = "claude-sonnet-5"
    viewer = _viewer_for(run)
    app = _Harness(viewer)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        texts = [w.item.text for w in viewer.query(TranscriptMessageWidget)]
        assert any("on it" in t for t in texts)
        # Header carries label + status + the run's resolved model.
        header_plain = viewer._header_text.plain
        assert "explore" in header_plain
        assert "[running]" in header_plain
        assert "claude-sonnet-5" in header_plain


async def test_viewer_header_model_fallback_and_omission() -> None:
    # Queued run, model not yet resolved: fall back to the requested model.
    run = _run("agent-1", status="queued", started=False, session=None)
    run.requested_model = "haiku"
    viewer = _viewer_for(run)
    app = _Harness(viewer)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert "[queued] · haiku" in viewer._header_text.plain

        # Resolution wins over the request (frontmatter may override it).
        run.model = "claude-sonnet-5"
        viewer.on_external_change()
        header_plain = viewer._header_text.plain
        assert "[queued] · claude-sonnet-5" in header_plain
        assert "haiku" not in header_plain

        # Neither known: the model segment is omitted entirely (the hints on
        # the right still legitimately contain "·" separators).
        run.model = None
        run.requested_model = None
        viewer.on_external_change()
        assert "[queued] ·" not in viewer._header_text.plain


async def test_viewer_composer_steers_the_run() -> None:
    run = _run("agent-1", status="running", session=None)  # no session → pending
    viewer = _viewer_for(run)
    app = _Harness(viewer)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert viewer.has_focus
        await pilot.press("enter")  # open the steer composer
        await pilot.pause()
        assert viewer._composer is not None
        await pilot.press("g", "o", "!")
        await pilot.press("enter")  # submit
        await pilot.pause()
        assert run.pending_steers == ["go!"]
        assert viewer._composer is None  # composer closed after send


async def test_viewer_steers_live_session() -> None:
    steered: list[str] = []
    session = SimpleNamespace(messages=(), queue_steering_message=steered.append)
    run = _run("agent-1", status="running", session=session)
    viewer = _viewer_for(run)
    app = _Harness(viewer)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("h", "i")
        await pilot.press("enter")
        await pilot.pause()
        assert steered == ["hi"]


async def test_viewer_composer_escape_cancels_without_steering() -> None:
    run = _run("agent-1", status="running", session=None)
    viewer = _viewer_for(run)
    app = _Harness(viewer)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("enter")  # open composer
        await pilot.pause()
        assert viewer._composer is not None
        await pilot.press("a", "b")
        await pilot.press("escape")  # cancel composer (pi parity: not the view)
        await pilot.pause()
        assert viewer._composer is None
        assert run.pending_steers == []  # nothing sent


async def test_viewer_empty_composer_autocloses_when_run_finishes() -> None:
    # An empty composer on a finished run is pure clutter; closing it is
    # lossless. (A non-empty one must survive — see the next test.)
    run = _run("agent-1", status="running", session=None)
    viewer = _viewer_for(run)
    app = _Harness(viewer)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("enter")  # open composer
        await pilot.pause()
        assert viewer._composer is not None

        run.status = "completed"
        run.completed_at = time.monotonic()
        viewer._on_run_event()  # completion push arrives on the UI loop
        await pilot.pause()

        assert viewer._composer is None
        assert viewer.has_focus


async def test_viewer_composer_with_text_survives_run_finish() -> None:
    # Completion arrives between keystrokes; yanking the input away would
    # destroy whatever the user was typing. Keep it, with the header already
    # showing the terminal status.
    run = _run("agent-1", status="running", session=None)
    viewer = _viewer_for(run)
    app = _Harness(viewer)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("a", "b")

        run.status = "completed"
        run.completed_at = time.monotonic()
        viewer._on_run_event()
        await pilot.pause()

        assert viewer._composer is not None
        assert viewer._composer.value == "ab"
        # The header reconciles even though the composer is preserved: the
        # terminal status shows, the steer hint is gone, and the notice goes
        # up immediately — no silent window inviting a doomed steer.
        header = viewer._header_text.plain
        assert "[completed]" in header
        assert "Enter steer" not in header
        assert "not sent" in header


async def test_viewer_empty_composer_autocloses_on_external_change() -> None:
    # Controller pushes (roster/status changes) reconcile the composer too,
    # not just the run's own listener events.
    run = _run("agent-1", status="running", session=None)
    viewer = _viewer_for(run)
    app = _Harness(viewer)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert viewer._composer is not None

        run.status = "completed"
        run.completed_at = time.monotonic()
        viewer.on_external_change()
        await pilot.pause()

        assert viewer._composer is None


async def test_viewer_rejects_steer_after_run_finished_without_losing_text() -> None:
    # pending_steers are only drained at session creation, so a steer sent to
    # a finished run silently vanishes. Refuse the send, keep the typed text
    # (the user can copy it), and say why in the header. Esc still closes.
    run = _run("agent-1", status="running", session=None)
    viewer = _viewer_for(run)
    app = _Harness(viewer)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("a", "b")

        run.status = "completed"
        run.completed_at = time.monotonic()
        viewer._on_run_event()
        await pilot.pause()
        await pilot.press("enter")  # submit into the finished run
        await pilot.pause()

        assert run.pending_steers == []  # nothing queued into the void
        assert viewer._composer is not None  # text not swallowed
        assert viewer._composer.value == "ab"
        assert "not sent" in viewer._header_text.plain

        await pilot.press("escape")
        await pilot.pause()
        assert viewer._composer is None
        assert "not sent" not in viewer._header_text.plain


async def test_viewer_and_composer_carry_identifying_chrome() -> None:
    # The viewer reuses tau's TranscriptView, so a subagent transcript is
    # pixel-identical to the main chat; the frame and composer title are what
    # tell the user they are inside a subagent view, not tau's prompt.
    run = _run("agent-1", agent_type="explore", status="running", session=None)
    viewer = _viewer_for(run)
    app = _Harness(viewer)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert "explore" in str(viewer.border_title)

        await pilot.press("enter")  # open composer
        await pilot.pause()
        composer = viewer._composer
        assert composer is not None
        assert composer.id == "viewer-composer"
        assert "steer" in str(composer.border_title).lower()


async def test_viewer_two_press_stop_guard() -> None:
    run = _run("agent-1", status="running", session=None)
    viewer = _viewer_for(run)
    app = _Harness(viewer)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("x")  # arms
        await pilot.pause()
        assert viewer._stop_armed is True
        assert run.aborted is False
        await pilot.press("x")  # confirms
        await pilot.pause()
        assert run.aborted is True  # stop_run fired


async def test_viewer_stop_disarms_on_other_key() -> None:
    run = _run("agent-1", status="running", session=None)
    viewer = _viewer_for(run)
    app = _Harness(viewer)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("x")
        await pilot.pause()
        assert viewer._stop_armed is True
        await pilot.press("down")  # any other key disarms (then scrolls)
        await pilot.pause()
        assert viewer._stop_armed is False
        assert run.aborted is False


async def test_viewer_escape_closes() -> None:
    handle = _FakeHandle()
    run = _run("agent-1", status="running", session=None)
    viewer = _viewer_for(run, handle)
    app = _Harness(viewer)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert handle.closed is True


async def test_viewer_cannot_steer_finished_run() -> None:
    # A finished run offers no steer affordance: Enter must not open a composer.
    run = _run("agent-1", status="completed", session=None, finished=True)
    viewer = _viewer_for(run)
    app = _Harness(viewer)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert viewer._composer is None


def _tool_result_session() -> SimpleNamespace:
    """A session whose transcript holds one tool call with a collapsed result."""
    return SimpleNamespace(
        messages=[
            UserMessage(content="list files"),
            AssistantMessage(
                content="",
                tool_calls=[ToolCall(id="call-1", name="bash", arguments={"command": "ls"})],
            ),
            ToolResultMessage(tool_call_id="call-1", name="bash", content="secret-output-line"),
        ],
        queue_steering_message=lambda m: None,
    )


def _transcript_text(viewer: ConversationViewer) -> str:
    return " ".join(str(w.selection_text) for w in viewer.query(TranscriptMessageWidget))


async def test_viewer_toggles_tool_result_expansion() -> None:
    # Ctrl+O (the host's toggle_tool_results key) must expand/collapse tool
    # results inside the viewer's own transcript — not fall through to the
    # host app, which would toggle the hidden main transcript instead.
    session = _tool_result_session()
    run = _run("agent-1", status="running", session=session)
    viewer = _viewer_for(run)
    app = _Harness(viewer)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert "secret-output-line" not in _transcript_text(viewer)  # collapsed by default
        await pilot.press("ctrl+o")
        await pilot.pause()
        assert "secret-output-line" in _transcript_text(viewer)
        # Expansion survives a live repaint (each push rebuilds the TuiState).
        session.messages.append(AssistantMessage(content="progress update"))
        for listener in list(run.listeners):
            listener()
        await pilot.pause()
        assert "secret-output-line" in _transcript_text(viewer)
        await pilot.press("ctrl+o")
        await pilot.pause()
        assert "secret-output-line" not in _transcript_text(viewer)
        assert app.leaked_tool_toggles == 0  # never reached the host binding


async def test_viewer_toggles_tool_results_while_composer_open() -> None:
    # Parity with the main chat, where ctrl+o works while typing at the
    # prompt: mid-steer, the toggle still applies to the viewer (and is still
    # consumed) without disturbing the composer's text.
    run = _run("agent-1", status="running", session=_tool_result_session())
    viewer = _viewer_for(run)
    app = _Harness(viewer)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("enter")  # open the steer composer
        await pilot.pause()
        await pilot.press("h", "i")
        await pilot.press("ctrl+o")
        await pilot.pause()
        assert "secret-output-line" in _transcript_text(viewer)
        assert viewer._composer is not None
        assert viewer._composer.value == "hi"
        assert app.leaked_tool_toggles == 0


async def test_viewer_honors_remapped_toggle_key() -> None:
    # The viewer reads the toggle key from the host's live settings, so a
    # remap applies here too — and ctrl+o goes back to flowing through.
    run = _run("agent-1", status="running", session=_tool_result_session())
    viewer = _viewer_for(run)
    app = _Harness(viewer)
    app.tui_settings = SimpleNamespace(
        keybindings=SimpleNamespace(toggle_tool_results="ctrl+b")
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+b")
        await pilot.pause()
        assert "secret-output-line" in _transcript_text(viewer)
        await pilot.press("ctrl+o")  # no longer the toggle: bubbles to the app
        await pilot.pause()
        assert "secret-output-line" in _transcript_text(viewer)  # viewer unchanged
        assert app.leaked_tool_toggles == 1


async def test_viewer_push_listener_rerenders_on_new_message() -> None:
    session = SimpleNamespace(
        messages=[UserMessage(content="start")], queue_steering_message=lambda m: None
    )
    run = _run("agent-1", status="running", session=session)
    viewer = _viewer_for(run)
    app = _Harness(viewer)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert run.listeners  # viewer subscribed on mount
        session.messages.append(AssistantMessage(content="progress update"))
        # Fire the per-run push (the analog of manager._notify_run).
        for listener in list(run.listeners):
            listener()
        await pilot.pause()
        texts = [w.item.text for w in viewer.query(TranscriptMessageWidget)]
        assert any("progress update" in t for t in texts)


async def test_viewer_unsubscribes_on_unmount() -> None:
    run = _run("agent-1", status="running", session=None)
    closed: list[int] = []
    viewer = ConversationViewer(
        run, _FakeHandle(), _manager(run), TAU_DARK_THEME, on_close=lambda: closed.append(1)
    )
    app = _Harness(viewer)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert run.listeners
        # The host removes the widget when its handle closes; unmount unsubscribes.
        await viewer.remove()
        await pilot.pause()
        assert run.listeners == []  # listener removed on unmount
        assert closed == [1]  # on_close notified the controller


# --- push at the manager level ---------------------------------------------


def test_manager_notify_run_fires_listeners() -> None:
    manager = SubagentManager(SimpleNamespace())
    run = _run("agent-1")
    fired: list[int] = []
    run.listeners.append(lambda: fired.append(1))
    manager._notify_run(run)
    assert fired == [1]


# --- component-seam wiring --------------------------------------------------


class FakeComponentBridge:
    """Records ComponentBridge calls; feeds synthetic keys to interceptors."""

    def __init__(self, *, supports: bool = True) -> None:
        self._supports = supports
        self.theme = TAU_DARK_THEME
        self.prompt_text = ""
        self.slot_calls: list[tuple[str, object, str]] = []
        self.slots: dict[str, object] = {}
        self.interceptors: list = []
        self.main_views: list = []
        self.render_requests = 0
        self.has_ui = True

    @property
    def supports_components(self) -> bool:
        return self._supports

    def get_prompt_text(self) -> str:
        return self.prompt_text

    def request_render(self) -> None:
        self.render_requests += 1

    def set_slot_widget(self, key, factory, *, placement="below_prompt"):  # noqa: ANN001
        self.slot_calls.append((key, factory, placement))
        if factory is None:
            self.slots.pop(key, None)
        else:
            self.slots[key] = factory

    def open_main_view(self, factory):  # noqa: ANN001
        handle = _FakeHandle()
        widget = factory(handle, self.theme)
        self.main_views.append((handle, widget))
        return handle

    def register_key_interceptor(self, handler):  # noqa: ANN001
        self.interceptors.append(handler)

        def unsub() -> None:
            if handler in self.interceptors:
                self.interceptors.remove(handler)

        return unsub


def test_controller_install_registers_slot_and_interceptor() -> None:
    manager = _manager()
    bridge = FakeComponentBridge()
    controller = SubagentUiController(manager, bridge)
    controller.install()

    assert [key for key, _f, _p in bridge.slot_calls] == [STRIP_KEY]
    assert bridge.slot_calls[0][2] == "below_prompt"
    assert len(bridge.interceptors) == 1

    # Teardown unregisters the interceptor and clears the slot.
    controller.teardown()
    assert bridge.interceptors == []
    assert (STRIP_KEY, None, "below_prompt") in bridge.slot_calls


def test_controller_open_conversation_uses_main_view() -> None:
    run = _run("agent-1", status="running")
    controller = SubagentUiController(_manager(run), FakeComponentBridge())
    assert controller.open_conversation(run) is True
    assert controller._components.main_views  # type: ignore[attr-defined]
    handle, widget = controller._components.main_views[0]  # type: ignore[attr-defined]
    assert isinstance(widget, ConversationViewer)


def test_controller_open_conversation_degrades_without_components() -> None:
    run = _run("agent-1", status="running")
    controller = SubagentUiController(_manager(run), FakeComponentBridge(supports=False))
    assert controller.open_conversation(run) is False


async def test_controller_interceptor_drives_nav_and_opens_viewer() -> None:
    run = _run("agent-1", status="running")
    manager = _manager(run)
    bridge = FakeComponentBridge()
    controller = SubagentUiController(manager, bridge)
    controller.install()
    strip = bridge.slots[STRIP_KEY](TAU_DARK_THEME)
    interceptor = bridge.interceptors[0]
    app = _Harness(strip)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        # Non-empty prompt → not consumed (pi's empty-editor gate).
        assert interceptor(Key("left", None), "hello") is False
        assert strip.nav_active is False
        # Empty prompt + left activates nav — the strip NEVER takes focus.
        assert interceptor(Key("left", None), "") is True
        assert strip.nav_active is True
        assert strip.selected_index == 0
        assert app.query_one("#prompt", Input).has_focus
        # Down moves onto the agent row; Enter opens its viewer via the bridge.
        assert interceptor(Key("down", None), "") is True
        assert strip.selected_index == 1
        assert interceptor(Key("enter", None), "") is True
        assert bridge.main_views, "Enter on the agent row did not open a viewer"
        # Opening a viewer resets the strip's nav state.
        assert strip.nav_active is False


async def test_controller_interceptor_escape_and_up_deactivate() -> None:
    run = _run("agent-1", status="running")
    bridge = FakeComponentBridge()
    controller = SubagentUiController(_manager(run), bridge)
    controller.install()
    strip = bridge.slots[STRIP_KEY](TAU_DARK_THEME)
    interceptor = bridge.interceptors[0]
    app = _Harness(strip)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        interceptor(Key("down", None), "")  # activate (highlight main)
        assert strip.nav_active is True
        # Escape deactivates.
        assert interceptor(Key("escape", None), "") is True
        assert strip.nav_active is False
        # Re-activate; up-past-top deactivates.
        interceptor(Key("down", None), "")
        assert interceptor(Key("up", None), "") is True
        assert strip.nav_active is False


async def test_controller_interceptor_other_key_flows_to_prompt() -> None:
    run = _run("agent-1", status="running")
    bridge = FakeComponentBridge()
    controller = SubagentUiController(_manager(run), bridge)
    controller.install()
    strip = bridge.slots[STRIP_KEY](TAU_DARK_THEME)
    interceptor = bridge.interceptors[0]
    app = _Harness(strip)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        interceptor(Key("down", None), "")  # activate
        # A typed character cancels nav and is NOT consumed (flows to the prompt).
        assert interceptor(Key("a", None), "") is False
        assert strip.nav_active is False


async def test_controller_interceptor_nav_stays_live_while_viewer_open() -> None:
    """A viewer being open must NOT strand strip nav (bug fix 2).

    ``left`` re-activates the strip while a viewer is up; ``down`` is left to the
    focused viewer for scrolling; ``enter`` on the ``main`` row closes the viewer.
    """
    run = _run("agent-1", status="running", session=SimpleNamespace(messages=()))
    bridge = FakeComponentBridge()
    controller = SubagentUiController(_manager(run), bridge)
    controller.install()
    strip = bridge.slots[STRIP_KEY](TAU_DARK_THEME)
    interceptor = bridge.interceptors[0]
    app = _Harness(strip)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        controller.open_conversation(run)
        assert controller._viewer is not None
        handle = bridge.main_views[0][0]

        # Nav inactive + viewer open: `down` belongs to the focused viewer (scroll).
        assert interceptor(Key("down", None), "") is False
        assert strip.nav_active is False
        # `left` re-activates strip nav even while the viewer is up.
        assert interceptor(Key("left", None), "") is True
        assert strip.nav_active is True
        assert strip.selected_index == 0
        # `enter` on the main row closes the viewer and returns to the prompt.
        assert interceptor(Key("enter", None), "") is True
        assert handle.closed is True
        assert strip.nav_active is False


async def test_controller_interceptor_enter_agent_switches_viewer() -> None:
    """`enter` on another agent row while a viewer is open switches the viewer."""
    run_a = _run("agent-1", status="running", session=SimpleNamespace(messages=()))
    run_b = _run("agent-2", status="running", session=SimpleNamespace(messages=()))
    bridge = FakeComponentBridge()
    controller = SubagentUiController(_manager(run_a, run_b), bridge)
    controller.install()
    strip = bridge.slots[STRIP_KEY](TAU_DARK_THEME)
    interceptor = bridge.interceptors[0]
    app = _Harness(strip)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        controller.open_conversation(run_a)
        assert len(bridge.main_views) == 1
        # Re-enter nav, move onto run_b's row, enter switches the viewer.
        assert interceptor(Key("left", None), "") is True
        assert interceptor(Key("down", None), "") is True  # main -> agent 1
        assert interceptor(Key("down", None), "") is True  # agent 1 -> agent 2
        assert strip.selected_index == 2
        assert interceptor(Key("enter", None), "") is True
        assert len(bridge.main_views) == 2  # a new viewer opened for run_b
        assert controller._viewing_id == "agent-2"
        assert strip.viewing_id == "agent-2"
        assert strip.nav_active is False  # opening resets nav


async def test_controller_interceptor_yields_while_composer_active() -> None:
    """While the steer composer owns the keyboard, the interceptor yields."""
    run = _run("agent-1", status="running", session=SimpleNamespace(messages=()))
    bridge = FakeComponentBridge()
    controller = SubagentUiController(_manager(run), bridge)
    controller.install()
    strip = bridge.slots[STRIP_KEY](TAU_DARK_THEME)
    interceptor = bridge.interceptors[0]
    app = _Harness(strip)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        controller.open_conversation(run)
        # Simulate the composer being up (it owns keys until it closes).
        controller._viewer._composer = object()  # type: ignore[attr-defined]
        assert controller._viewer.composer_active is True
        assert interceptor(Key("left", None), "") is False
        assert interceptor(Key("escape", None), "") is False


async def test_strip_click_main_row_closes_open_viewer() -> None:
    """Clicking the ``main`` row while a viewer is open closes it (bug fix 2)."""
    run = _run("agent-1", status="running", session=SimpleNamespace(messages=()))
    closed: list[int] = []

    def _close() -> bool:
        closed.append(1)
        return True

    strip = AgentStripWidget(
        _manager(run),
        TAU_DARK_THEME,
        open_conversation=lambda r: True,
        close_conversation=_close,
    )
    strip.viewing_id = "agent-1"
    app = _Harness(strip)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        # render() populated _row_runs; index 0 is the main row (None).
        assert strip._row_runs[0] is None
        strip.on_click(SimpleNamespace(y=0))  # type: ignore[arg-type]
        assert closed == [1]
        # With no viewer open, the main-row click just deactivates nav.
        closed_none = AgentStripWidget(
            _manager(run),
            TAU_DARK_THEME,
            open_conversation=lambda r: True,
            close_conversation=lambda: False,
        )
        app2 = _Harness(closed_none)
        async with app2.run_test(size=(80, 24)) as pilot2:
            await pilot2.pause()
            closed_none.activate_nav()
            closed_none.on_click(SimpleNamespace(y=0))  # type: ignore[arg-type]
            assert closed_none.nav_active is False


async def test_controller_interceptor_ignored_without_agents() -> None:
    manager = _manager()  # no runs
    bridge = FakeComponentBridge()
    controller = SubagentUiController(manager, bridge)
    controller.install()
    strip = bridge.slots[STRIP_KEY](TAU_DARK_THEME)
    interceptor = bridge.interceptors[0]
    app = _Harness(strip)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert interceptor(Key("down", None), "") is False


async def test_controller_on_change_refreshes_strip_after_spawn() -> None:
    manager = _manager()
    bridge = FakeComponentBridge()
    controller = SubagentUiController(manager, bridge)
    controller.install()
    strip = bridge.slots[STRIP_KEY](TAU_DARK_THEME)
    app = _Harness(strip)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert strip.has_agents() is False
        # A run appears; the manager's change signal re-renders the strip.
        manager.runs["agent-1"] = _run("agent-1", agent_type="explore", status="running")
        controller.on_change()
        await pilot.pause()
        assert "explore" in _strip_text(strip)


# --- setup() wiring at the runtime level -----------------------------------


async def test_setup_registers_components_on_component_host(tmp_path) -> None:  # noqa: ANN001
    runtime = _load_runtime(tmp_path)
    session = RecordingSession(tmp_path)
    runtime.bind(session)
    bridge = FakeComponentBridge()
    runtime.set_ui_bridge(bridge)

    await runtime.emit_session_start("startup")

    assert [key for key, _f, _p in bridge.slot_calls] == [STRIP_KEY]
    assert len(bridge.interceptors) == 1


async def test_setup_skips_components_on_null_host(tmp_path) -> None:  # noqa: ANN001
    runtime = _load_runtime(tmp_path)
    session = RecordingSession(tmp_path)
    runtime.bind(session)
    # Default runtime.ui is the print-mode NullUiBridge (supports_components False).
    assert runtime.ui.supports_components is False
    # Must not raise, and nothing to assert beyond a clean session_start.
    await runtime.emit_session_start("startup")


async def test_setup_survives_component_less_core(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    # Simulate an OLDER tau whose ``context.ui`` predates the component seam:
    # the ``components`` attribute is absent entirely (not merely a no-op
    # bridge). The extension's getattr-guard must degrade to dialog-only rather
    # than crash session_start (constraint 8).
    from tau_coding.extensions.api import ExtensionUi

    monkeypatch.delattr(ExtensionUi, "components", raising=True)
    runtime = _load_runtime(tmp_path)
    session = RecordingSession(tmp_path)
    runtime.bind(session)

    bridge = FakeComponentBridge()  # a real component host is present…
    runtime.set_ui_bridge(bridge)
    # …but the facade no longer exposes `.components`, so the guard sees None.
    await runtime.emit_session_start("startup")

    # No strip / interceptor installed, and no exception surfaced.
    assert bridge.slot_calls == []
    assert bridge.interceptors == []
