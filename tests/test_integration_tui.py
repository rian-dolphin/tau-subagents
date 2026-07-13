"""End-to-end integration test: the REAL ``TauTuiApp`` with the REAL extension.

Every other UI test in this repo drives the widgets against a
``FakeComponentBridge`` and runtime stubs. This test closes that gap: it builds
the actual :class:`tau_coding.tui.app.TauTuiApp` around a minimal fake session,
hands it a REAL :class:`~tau_coding.extensions.ExtensionRuntime` that has loaded
the REAL ``tau_subagents`` extension, runs it under ``run_test()``, and asserts
the extension's strip and conversation viewer actually mount into tau's
host-owned component slots.

It is the experiment's proof of life: it exercises the session_start ordering
(host bridge attached in ``__init__`` -> ``emit_pending_session_start`` in
``on_mount`` -> extension installs its widgets) that no isolated suite covers.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.containers import Container

from tau_agent import (
    QueueUpdateEvent,
    ToolCall,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
)
from tau_agent.messages import AssistantMessage
from tau_ai import FakeProvider, ProviderResponseEndEvent, ProviderResponseStartEvent
from tau_coding.session import ModelChoice, TerminalCommandResult
from tau_coding.skills import Skill
from tau_coding.system_prompt import ProjectContextFile
from tau_coding.tools import create_coding_tools
from tau_coding.tui.app import PromptInput, TauTuiApp
from tau_coding.tui.widgets import TranscriptMessageWidget, TranscriptView

from tau_subagents.ui.strip import AgentStripWidget
from tau_subagents.ui.viewer import ConversationViewer

# Sibling test modules (pytest prepend import mode puts tests/ on sys.path).
from test_extension import (  # noqa: E402
    _agent_tool,
    _extension_module,
    _load_runtime,
    _patch_provider_factory,
    _text_stream,
)
from test_ui import _run, _strip_text  # noqa: E402

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _FakeSessionState:
    thinking_level = "medium"


class _FakeSession:
    """Minimal fake session that satisfies what ``TauTuiApp`` reads.

    Mirrors the surface of tau's own ``tests/test_tui_app.py`` ``FakeSession``
    (which is not packaged, so this is a local copy trimmed to the mount path),
    plus the two hooks that make the real runtime live: an ``extension_runtime``
    the app can bind a UI bridge to, and an ``emit_pending_session_start`` that
    drives the runtime's real ``session_start`` fan-out (this is where the
    extension installs its widgets).
    """

    def __init__(self, runtime, cwd: Path) -> None:  # noqa: ANN001
        self._extension_runtime = runtime
        self.cwd = cwd
        self.session_id = "itest-session"
        self.is_running = False
        self.messages: tuple[object, ...] = ()
        self.events: tuple[object, ...] = ()
        self.provider_name = "openai"
        self.model = "fake-model"
        self.available_models = ("fake-model",)
        self.available_model_choices = (
            ModelChoice(provider_name="openai", model="fake-model"),
        )
        self.scoped_model_choices: tuple[ModelChoice, ...] = ()
        self.available_providers = ("openai",)
        self.tools = tuple(create_coding_tools(cwd=cwd))
        self.skills = (Skill(name="review", path=cwd / "review.md", content="Review"),)
        self.prompt_templates = ()
        self.context_files = (
            ProjectContextFile(path=str(cwd / "AGENTS.md"), content="Rules."),
        )
        self.context_token_estimate = 100
        self.auto_compact_token_threshold = 200000
        self.context_window_tokens = 216384
        self.thinking_level = "medium"
        self.available_thinking_levels = ("off", "low", "medium", "high")
        self.state = _FakeSessionState()
        self.resource_diagnostics = ()
        self.system_prompt = "You are Tau."
        self.session_manager = None
        self._session_title: str | None = None
        self.queued_steering_messages: tuple[str, ...] = ()
        self.queued_follow_up_messages: tuple[str, ...] = ()
        self.session_start_emissions = 0

    @property
    def extension_runtime(self):  # noqa: ANN201
        return self._extension_runtime

    async def emit_pending_session_start(self) -> None:
        # The real ordering under test: by the time the app calls this (in
        # on_mount), it has already attached its component bridge in __init__,
        # so the extension's session_start handler installs a working strip.
        self.session_start_emissions += 1
        await self._extension_runtime.emit_session_start("startup")

    @property
    def session_title(self) -> str | None:
        return self._session_title

    def queue_update_event(self) -> QueueUpdateEvent:
        return QueueUpdateEvent(
            steering=self.queued_steering_messages,
            follow_up=self.queued_follow_up_messages,
        )

    def pop_latest_follow_up_message(self) -> str | None:
        return None

    # -- BoundSession surface the extension may reach through the runtime -----

    def queue_steering_message(self, content: str, **_: object) -> None:
        self.queued_steering_messages = (*self.queued_steering_messages, content)

    def queue_follow_up_message(self, content: str, **_: object) -> None:
        self.queued_follow_up_messages = (*self.queued_follow_up_messages, content)

    async def append_custom_entry(self, namespace: str, data: dict) -> None:
        del namespace, data

    async def run_terminal_command(
        self, command: str, *, add_to_context: bool
    ) -> TerminalCommandResult:
        return TerminalCommandResult(
            command=command,
            output="",
            exit_code=0,
            ok=True,
            added_to_context=add_to_context,
        )


def _make_app(tmp_path: Path) -> tuple[TauTuiApp, _FakeSession]:
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    runtime = _load_runtime(tmp_path)
    session = _FakeSession(runtime, project)
    # Bind so the extension's context (session_id / cwd / steering) is live;
    # in production CodingSession.load does this. The app then attaches the
    # real _TuiExtensionUiBridge in TauTuiApp.__init__.
    runtime.bind(session)
    app = TauTuiApp(session)
    return app, session


async def test_real_app_mounts_extension_strip_and_viewer(tmp_path) -> None:  # noqa: ANN001
    app, session = _make_app(tmp_path)

    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()

        # 1. session_start fired exactly once, through the real fan-out.
        assert session.session_start_emissions == 1

        # 2. The extension installed its strip into tau's #below-prompt-slot.
        slot = app.query_one("#below-prompt-slot", Container)
        strips = slot.query(AgentStripWidget)
        assert len(strips) == 1, "extension strip did not mount into the host slot"
        strip = strips.first()
        assert strip.has_agents() is False  # no runs yet

        # 3. A run appears in the real manager; the manager's change signal
        #    (repointed at the controller by the extension) refreshes the strip.
        manager = strip._manager  # the real SubagentManager from setup()
        assert manager.sources_changed is not None, "controller never wired the push"
        run = _run(
            "agent-1",
            agent_type="explore",
            description="survey the repo",
            status="running",
            session=SimpleNamespace(messages=()),
        )
        manager.runs["agent-1"] = run
        manager.sources_changed()
        await pilot.pause()
        assert strip.has_agents() is True
        assert "explore" in _strip_text(strip)

        # 4. Opening the conversation (through the controller, via the strip's
        #    bound callback) mounts the viewer into #main-slot and hides the
        #    main transcript in place.
        opened = strip._open_conversation(run)
        assert opened is True
        await pilot.pause()

        main_slot = app.query_one("#main-slot", Container)
        viewers = main_slot.query(ConversationViewer)
        assert len(viewers) == 1, "viewer did not mount into the host main slot"
        assert app.query_one("#transcript", TranscriptView).display is False
        assert main_slot.display is True

        # 5. Closing the view restores the main transcript and drops the viewer.
        assert app._extension_main_view is not None
        app._extension_main_view.close()
        await pilot.pause()

        assert not app.query("#subagents-conversation-viewer")
        assert app.query_one("#transcript", TranscriptView).display is True
        assert app.query_one("#main-slot", Container).display is False
        # Focus returned to the host prompt.
        assert app.query_one("#prompt", PromptInput).has_focus


class _GatedProvider:
    """A provider whose single response stalls on an event, keeping the run alive.

    Modeled on ``test_extension.BlockingProvider``: the spawned run stays
    ``running`` (so the strip stays populated with a live row) until ``release``
    is set, which lets the E2E observe the strip and drive keyboard nav without
    racing the run to completion.
    """

    def __init__(self, release: asyncio.Event) -> None:
        self._release = release

    def stream_response(self, *, model, system, messages, tools, signal=None):  # noqa: ANN001, ANN202
        async def iterator():  # noqa: ANN202
            await self._release.wait()
            yield ProviderResponseStartEvent(model="fake")
            yield ProviderResponseEndEvent(message=AssistantMessage(content="done"))

        return iterator()


async def _wait_until(pilot, condition, *, tries: int = 200) -> None:  # noqa: ANN001
    """Pump the event loop until ``condition()`` holds (or we give up)."""
    for _ in range(tries):
        if condition():
            return
        await pilot.pause()


async def test_real_spawn_lights_strip_and_keyboard_opens_viewer(tmp_path) -> None:  # noqa: ANN001
    """End-to-end proof: a real background spawn lights the strip (with non-zero
    height), and REAL key presses drive the fleet-list nav — ``left`` activates,
    ``down``+``enter`` open the viewer, ``escape`` closes it — all through the
    pre-dispatch key interceptor, with the prompt keeping focus throughout.
    """
    app, session = _make_app(tmp_path)

    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()

        release = asyncio.Event()
        _patch_provider_factory(_extension_module(), _GatedProvider(release))
        agent_tool = _agent_tool(session.extension_runtime)

        # Spawn through the REAL agent tool, in the background so it returns at once
        # while the run keeps executing on the TUI event loop.
        result = await agent_tool.execute(
            {
                "prompt": "look around",
                "description": "survey the repo",
                "subagent_type": "explore",
                "run_in_background": True,
            }
        )
        assert result.ok, result.content

        slot = app.query_one("#below-prompt-slot", Container)
        strip = slot.query(AgentStripWidget).first()

        # A. The real spawn path lit up the strip with no manual signal.
        await _wait_until(pilot, strip.has_agents)
        assert strip.has_agents(), "spawn did not populate the strip"
        assert "explore" in _strip_text(strip)

        # A'. Geometry regression: the strip must actually occupy visible height
        # (the invisible zero-height bug — refresh() without layout=True).
        await _wait_until(pilot, lambda: strip.region.height > 0)
        assert strip.region.height > 0, "strip mounted but stayed zero-height"
        assert strip.display is True
        # And it is genuinely on screen (the compositor is painting it).
        assert strip in app.screen._compositor.visible_widgets

        # B. REAL keyboard: left at the empty prompt activates nav (no focus steal).
        prompt = app.query_one("#prompt", PromptInput)
        prompt.focus()
        await pilot.pause()
        assert prompt.has_focus
        await pilot.press("left")
        await pilot.pause()
        assert strip.nav_active, "left at empty prompt did not activate strip nav"
        assert prompt.has_focus, "strip stole focus from the prompt"
        assert strip.selected_index == 0  # highlights the main row first

        # C. down moves onto the agent row; enter opens the real viewer in #main-slot.
        await pilot.press("down")
        await pilot.pause()
        assert strip.selected_index == 1
        await pilot.press("enter")
        await pilot.pause()
        main_slot = app.query_one("#main-slot", Container)
        viewers = main_slot.query(ConversationViewer)
        assert viewers, "enter on the agent row did not open the viewer"
        assert app.query_one("#transcript", TranscriptView).display is False
        assert main_slot.display is True
        # Opening the viewer reset the strip's nav state.
        assert strip.nav_active is False

        # D. escape (with the viewer focused) closes it and restores the transcript.
        assert viewers.first().has_focus
        await pilot.press("escape")
        await pilot.pause()
        assert not main_slot.query(ConversationViewer)
        assert app.query_one("#transcript", TranscriptView).display is True
        assert main_slot.display is False

        # Let the stalled run finish so teardown is clean.
        release.set()
        await pilot.pause()


async def _settle(pilot) -> None:  # noqa: ANN001
    """Pump through an async component swap (deferred remove -> mount)."""
    for _ in range(4):
        await asyncio.sleep(0)
        await pilot.pause()


def _seed_run(strip, agent_id: str, *, agent_type: str, description: str):  # noqa: ANN001, ANN202
    run = _run(
        agent_id,
        agent_type=agent_type,
        description=description,
        status="running",
        session=SimpleNamespace(messages=()),
    )
    strip._manager.runs[agent_id] = run
    return run


# --- Regression: the deferred-remove DuplicateIds race (bug fix 2) -----------
# These capture the two user-reported crashes the crash spike diagnosed:
# opening a second agent's viewer while one is open, and a same-tick extension
# teardown+reinstall (session rebind). With the host sequencing swaps after
# removals drain, both land exactly one widget with zero component failures.


async def test_rapid_viewer_switch_mounts_exactly_one_viewer(tmp_path) -> None:  # noqa: ANN001
    """Opening a second viewer the same tick a first opens -> one viewer, no crash."""
    app, _session = _make_app(tmp_path)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        strip = app.query_one("#below-prompt-slot", Container).query(AgentStripWidget).first()
        controller = strip._open_conversation.__self__  # the SubagentUiController
        run_a = _seed_run(strip, "agent-1", agent_type="explore", description="A")
        run_b = _seed_run(strip, "agent-2", agent_type="general", description="B")
        strip._manager.sources_changed()
        await pilot.pause()

        # Same tick: open A, then immediately B (the reported second-viewer action).
        assert controller.open_conversation(run_a) is True
        assert controller.open_conversation(run_b) is True
        await _settle(pilot)

        viewers = app.query(ConversationViewer)
        assert len(viewers) == 1, "rapid switch left more than one viewer mounted"
        assert viewers.first()._run.agent_id == "agent-2"  # last-writer wins
        assert app.query_one("#transcript", TranscriptView).display is False
        assert app._extension_component_failures_reported == set()


async def test_rapid_viewer_switch_a_b_c(tmp_path) -> None:  # noqa: ANN001
    """Three viewer opens in one tick collapse to the last, no DuplicateIds."""
    app, _session = _make_app(tmp_path)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        strip = app.query_one("#below-prompt-slot", Container).query(AgentStripWidget).first()
        controller = strip._open_conversation.__self__
        run_a = _seed_run(strip, "agent-1", agent_type="explore", description="A")
        run_b = _seed_run(strip, "agent-2", agent_type="general", description="B")
        run_c = _seed_run(strip, "agent-3", agent_type="plan", description="C")
        strip._manager.sources_changed()
        await pilot.pause()

        controller.open_conversation(run_a)
        controller.open_conversation(run_b)
        controller.open_conversation(run_c)
        await _settle(pilot)

        viewers = app.query(ConversationViewer)
        assert len(viewers) == 1
        assert viewers.first()._run.agent_id == "agent-3"
        assert app._extension_component_failures_reported == set()


async def test_same_tick_teardown_reinstall_yields_one_strip(tmp_path) -> None:  # noqa: ANN001
    """Tearing the controller down and reinstalling in one tick -> one strip."""
    app, _session = _make_app(tmp_path)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        strip = app.query_one("#below-prompt-slot", Container).query(AgentStripWidget).first()
        controller = strip._open_conversation.__self__

        controller.teardown()
        controller.install()
        await _settle(pilot)

        strips = app.query(AgentStripWidget)
        assert len(strips) == 1, "same-tick teardown+reinstall left a duplicate strip"
        assert app._extension_component_failures_reported == set()


async def test_session_rebind_keeps_single_strip(tmp_path) -> None:  # noqa: ANN001
    """The real /new,/resume rebind sequence (shutdown then start) keeps one strip.

    Reproduces bug 3: the extension tears its widgets down on ``session_shutdown``
    and reinstalls on the next ``session_start``; the old strip's deferred
    remove() must drain before the fresh strip (same id) mounts.
    """
    app, session = _make_app(tmp_path)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        assert len(app.query(AgentStripWidget)) == 1

        runtime = session.extension_runtime
        await runtime.emit_session_shutdown("new")
        await runtime.emit_session_start("new")
        await _settle(pilot)

        strips = app.query(AgentStripWidget)
        assert len(strips) == 1, "strip lost / duplicated across session rebind"
        assert strips.first().has_agents() is False
        assert app._extension_component_failures_reported == set()


async def test_journey_switch_viewers_then_back_to_main(tmp_path) -> None:  # noqa: ANN001
    """The user's exact journey, end to end, with zero component failures.

    Spawn two real background runs -> open run 1's viewer via keys -> ``left``
    re-activates nav while viewing -> ``down``/``enter`` switches to run 2 ->
    ``left``, select ``main``, ``enter`` closes the viewer and restores the
    transcript with the prompt refocused.
    """
    app, session = _make_app(tmp_path)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()

        release = asyncio.Event()
        _patch_provider_factory(_extension_module(), _GatedProvider(release))
        agent_tool = _agent_tool(session.extension_runtime)
        for description, agent_type in (("survey the repo", "explore"), ("plan work", "general")):
            result = await agent_tool.execute(
                {
                    "prompt": "look around",
                    "description": description,
                    "subagent_type": agent_type,
                    "run_in_background": True,
                }
            )
            assert result.ok, result.content

        slot = app.query_one("#below-prompt-slot", Container)
        strip = slot.query(AgentStripWidget).first()
        await _wait_until(pilot, lambda: len(strip._agent_runs()) == 2)
        main_slot = app.query_one("#main-slot", Container)

        prompt = app.query_one("#prompt", PromptInput)
        prompt.focus()
        await pilot.pause()

        # Open run 1's viewer via the fleet-list keys.
        await pilot.press("left")   # activate nav on the main row
        await pilot.press("down")   # -> first agent row
        await pilot.press("enter")  # open its viewer
        await _settle(pilot)
        viewers = main_slot.query(ConversationViewer)
        assert len(viewers) == 1
        first_run_id = viewers.first()._run.agent_id
        assert app.query_one("#transcript", TranscriptView).display is False

        # `left` re-activates the strip nav while the viewer is open (bug fix 2).
        await pilot.press("left")
        await pilot.pause()
        assert strip.nav_active is True

        # `down`/`enter` switches the viewer to run 2 (race-safe swap).
        await pilot.press("down")   # main -> agent 1
        await pilot.press("down")   # agent 1 -> agent 2
        await pilot.press("enter")  # switch the viewer
        await _settle(pilot)
        viewers = main_slot.query(ConversationViewer)
        assert len(viewers) == 1, "switching viewers left more than one mounted"
        second_run_id = viewers.first()._run.agent_id
        assert second_run_id != first_run_id
        assert strip.viewing_id == second_run_id
        assert strip.nav_active is False  # opening reset nav

        # `left`, select `main`, `enter` closes the viewer and restores main.
        await pilot.press("left")
        await pilot.pause()
        assert strip.nav_active is True
        assert strip.selected_index == 0
        await pilot.press("enter")
        await _settle(pilot)

        assert not main_slot.query(ConversationViewer), "main row did not close the viewer"
        assert app.query_one("#transcript", TranscriptView).display is True
        assert main_slot.display is False
        assert app.query_one("#prompt", PromptInput).has_focus
        assert strip.viewing_id is None
        assert app._extension_component_failures_reported == set()

        release.set()
        await pilot.pause()


# --- Regression: focus integrity while a viewer is open ----------------------
# The user-reported cluster (ctrl+o toggling the MAIN transcript, typed text
# submitting to the main chat, esc doing nothing) is one state: viewer open but
# focus back on the prompt. The host's app-level click handler used to refocus
# the prompt on ANY left click, silently rerouting every key to the main chat.


def _focus_inside(app, widget) -> bool:  # noqa: ANN001
    focused = app.focused
    return focused is not None and (focused is widget or widget in focused.ancestors)


def _open_viewer(app, strip, run):  # noqa: ANN001, ANN202
    controller = strip._open_conversation.__self__
    assert controller.open_conversation(run) is True
    return controller


async def test_click_inside_viewer_keeps_viewer_focused(tmp_path) -> None:  # noqa: ANN001
    app, _session = _make_app(tmp_path)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        strip = app.query_one("#below-prompt-slot", Container).query(AgentStripWidget).first()
        run = _seed_run(strip, "agent-1", agent_type="explore", description="A")
        strip._manager.sources_changed()
        _open_viewer(app, strip, run)
        await _settle(pilot)
        viewer = app.query_one(ConversationViewer)
        assert _focus_inside(app, viewer)

        await pilot.click(ConversationViewer)
        await pilot.pause()
        assert _focus_inside(app, viewer), (
            "clicking inside the viewer moved focus away "
            f"(focused: {app.focused!r}) — every key now goes to the main chat"
        )


async def test_click_outside_viewer_keeps_viewer_focused(tmp_path) -> None:  # noqa: ANN001
    # A click on the strip (or any main-screen chrome) must not hand the
    # keyboard back to the prompt while an extension main view is open.
    app, _session = _make_app(tmp_path)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        strip = app.query_one("#below-prompt-slot", Container).query(AgentStripWidget).first()
        run = _seed_run(strip, "agent-1", agent_type="explore", description="A")
        strip._manager.sources_changed()
        _open_viewer(app, strip, run)
        await _settle(pilot)
        viewer = app.query_one(ConversationViewer)

        # Click the strip's hint line (row 2 with one agent): a dead row —
        # clicking the main row would legitimately close the viewer, and the
        # agent row would re-open it.
        await pilot.click(AgentStripWidget, offset=(1, 2))
        await pilot.pause()
        assert app.query(ConversationViewer), "the dead-row click closed the viewer"
        assert _focus_inside(app, viewer), (
            f"a click outside the viewer stole focus (focused: {app.focused!r})"
        )


async def test_viewer_keys_still_work_after_a_click(tmp_path) -> None:  # noqa: ANN001
    # The reported symptoms, end to end: after a click, ctrl+o must toggle the
    # VIEWER's tool results (not the hidden main transcript) and esc must
    # close the viewer (not fall through to the prompt's cancel).
    app, _session = _make_app(tmp_path)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        strip = app.query_one("#below-prompt-slot", Container).query(AgentStripWidget).first()
        run = _seed_run(strip, "agent-1", agent_type="explore", description="A")
        strip._manager.sources_changed()
        _open_viewer(app, strip, run)
        await _settle(pilot)
        viewer = app.query_one(ConversationViewer)

        await pilot.click(ConversationViewer)
        await pilot.pause()

        await pilot.press("ctrl+o")
        await pilot.pause()
        assert viewer._show_tool_results is True, "ctrl+o did not reach the viewer"
        assert app.state.show_tool_results is False, (
            "ctrl+o leaked to the host and toggled the main transcript"
        )

        await pilot.press("escape")
        await _settle(pilot)
        assert not app.query(ConversationViewer), "esc after a click did not close the viewer"
        assert app.query_one("#transcript", TranscriptView).display is True


async def test_agents_menu_open_path_focuses_viewer(tmp_path) -> None:  # noqa: ANN001
    # The /agents flow opens the viewer right after a modal select dismisses;
    # the modal's focus-restore must not outlive the viewer's mount focus.
    app, session = _make_app(tmp_path)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        strip = app.query_one("#below-prompt-slot", Container).query(AgentStripWidget).first()
        run = _seed_run(strip, "agent-1", agent_type="explore", description="A")
        strip._manager.sources_changed()
        await pilot.pause()

        bridge = session.extension_runtime.ui  # the real _TuiExtensionUiBridge
        select_task = asyncio.ensure_future(bridge.select("Agents", ["explore (A)"]))
        await _settle(pilot)
        await pilot.press("enter")  # choose the (only) run, dismissing the modal
        choice = await select_task
        assert choice == "explore (A)"

        # agents_menu.view_run_conversation runs after the await returns.
        _open_viewer(app, strip, run)
        await _settle(pilot)
        viewer = app.query_one(ConversationViewer)
        assert _focus_inside(app, viewer), (
            f"viewer opened from the /agents modal is not focused (focused: {app.focused!r})"
        )


async def test_foreground_result_renders_completion_card(tmp_path) -> None:  # noqa: ANN001
    """End-to-end proof of the render_result seam: a REAL foreground agent run's
    result, streamed through the REAL app, renders the completion card on the
    tool row (invocation line + ✓ stats + ⎿ preview) instead of the bare
    invocation the row used to collapse back to.
    """
    app, session = _make_app(tmp_path)

    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()

        _patch_provider_factory(
            _extension_module(),
            FakeProvider([_text_stream("Subagent report: all good.")]),
        )
        agent_tool = _agent_tool(session.extension_runtime)
        result = await agent_tool.execute(
            {"prompt": "go", "description": "survey the repo"}
        )
        # In production the loop stamps the id; the tool returns it blank.
        result = result.model_copy(update={"tool_call_id": "call-1"})

        async def stream(event) -> None:  # noqa: ANN001
            app.adapter.apply(event)
            await app._apply_streaming_transcript_event(event)

        await stream(
            ToolExecutionStartEvent(
                tool_call=ToolCall(
                    id="call-1",
                    name="agent",
                    arguments={"prompt": "go", "description": "survey the repo"},
                )
            )
        )
        await stream(ToolExecutionEndEvent(result=result))
        await pilot.pause()

        widget = next(
            w for w in app.query(TranscriptMessageWidget) if w.item.role == "tool"
        )
        text = widget.selection_text
        # The render_call invocation line stays…
        assert "▸ general agent · survey the repo" in text
        # …with the compact completion card beneath it.
        assert "✓ completed" in text
        assert "⎿  Subagent report: all good." in text
        # The raw tool-result body stays out of the collapsed row.
        assert "agent-1 [completed]" not in text
