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

from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.containers import Container

from tau_agent import QueueUpdateEvent, UserMessage
from tau_coding.session import ModelChoice, TerminalCommandResult
from tau_coding.skills import Skill
from tau_coding.system_prompt import ProjectContextFile
from tau_coding.tools import create_coding_tools
from tau_coding.tui.app import PromptInput, TauTuiApp
from tau_coding.tui.widgets import TranscriptView

from tau_subagents.ui.strip import AgentStripWidget
from tau_subagents.ui.viewer import ConversationViewer

# Sibling test modules (pytest prepend import mode puts tests/ on sys.path).
from test_extension import _load_runtime  # noqa: E402
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
