"""Tests for the tau-subagents extension.

Requires Tau's packages on the import path; run from a Tau checkout:

    uv run --project /path/to/tau pytest tests/
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tau_agent.messages import AssistantMessage, UserMessage
from tau_agent.tools import ToolCall
from tau_ai import FakeProvider, ProviderResponseEndEvent, ProviderResponseStartEvent
from tau_coding import TauResourcePaths
from tau_coding.extensions import ExtensionRuntime

pytestmark = pytest.mark.anyio

EXTENSION_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _paths(tmp_path: Path) -> TauResourcePaths:
    return TauResourcePaths(
        root=tmp_path / "home-tau",
        cwd=tmp_path / "project",
        agents_root=tmp_path / "home-agents",
    )


class RecordingSession:
    """Minimal BoundSession implementation for runtime tests."""

    def __init__(self, tmp_path: Path, *, running: bool = False) -> None:
        self.cwd = tmp_path
        self.model = "fake"
        self.provider_name = "fake"
        self.session_id = "session-1"
        self.system_prompt = "You are Tau."
        self.is_running = running
        self.steered: list[str] = []
        self.followed_up: list[str] = []
        self.custom_entries: list[tuple[str, dict[str, object]]] = []

    def queue_steering_message(self, content: str) -> None:
        self.steered.append(content)

    def queue_follow_up_message(self, content: str) -> None:
        self.followed_up.append(content)

    async def append_custom_entry(self, namespace: str, data: dict[str, object]) -> None:
        self.custom_entries.append((namespace, data))


def _load_runtime(tmp_path: Path) -> ExtensionRuntime:
    runtime = ExtensionRuntime()
    runtime.load(
        _paths(tmp_path),
        extra_paths=(EXTENSION_DIR,),
        include_resource_dirs=False,
    )
    return runtime


def _extension_module() -> object:
    candidates = [
        module
        for module_name, module in sys.modules.items()
        if module_name.startswith("tau_extension_tau_subagents")
        and "." not in module_name
    ]
    assert candidates, "extension module not loaded"
    return candidates[-1]


def _patch_fake_provider(module: object, *, response: str) -> None:
    module.load_provider_settings = lambda: None  # type: ignore[attr-defined]
    module.resolve_provider_selection = (  # type: ignore[attr-defined]
        lambda settings, model=None: SimpleNamespace(
            provider=SimpleNamespace(name="fake"),
            model="fake",
        )
    )
    module.create_model_provider = (  # type: ignore[attr-defined]
        lambda provider, model, thinking_level: FakeProvider(
            [
                [
                    ProviderResponseStartEvent(model="fake"),
                    ProviderResponseEndEvent(message=AssistantMessage(content=response)),
                ]
            ]
        )
    )


def _agent_tool(runtime: ExtensionRuntime):  # noqa: ANN202
    return next(tool for tool in runtime.extension_tools if tool.name == "agent")


def _steer_tool(runtime: ExtensionRuntime):  # noqa: ANN202
    return next(tool for tool in runtime.extension_tools if tool.name == "steer_subagent")


def _text_stream(text: str) -> list[object]:
    return [
        ProviderResponseStartEvent(model="fake"),
        ProviderResponseEndEvent(message=AssistantMessage(content=text)),
    ]


def _tool_call_stream(text: str, call_id: str) -> list[object]:
    return [
        ProviderResponseStartEvent(model="fake"),
        ProviderResponseEndEvent(
            message=AssistantMessage(
                content=text,
                tool_calls=[ToolCall(id=call_id, name="noop")],
            )
        ),
    ]


class BlockingProvider:
    """A provider whose single text response waits on an event before yielding."""

    def __init__(self, release: asyncio.Event, text: str) -> None:
        self._release = release
        self._text = text
        self.calls: list[object] = []

    def stream_response(self, *, model, system, messages, tools, signal=None):  # noqa: ANN001, ANN202
        self.calls.append(list(messages))

        async def iterator():  # noqa: ANN202
            await self._release.wait()
            yield ProviderResponseStartEvent(model="fake")
            yield ProviderResponseEndEvent(message=AssistantMessage(content=self._text))

        return iterator()


def _patch_provider_settings(module: object) -> None:
    module.load_provider_settings = lambda: None  # type: ignore[attr-defined]
    module.resolve_provider_selection = (  # type: ignore[attr-defined]
        lambda settings, model=None: SimpleNamespace(
            provider=SimpleNamespace(name="fake"), model="fake"
        )
    )


def _patch_provider_factory(module: object, provider: object) -> None:
    _patch_provider_settings(module)
    module.create_model_provider = (  # type: ignore[attr-defined]
        lambda provider_arg, model, thinking_level: provider
    )


def _patch_provider_sequence(module: object, providers: list[object]) -> None:
    _patch_provider_settings(module)
    it = iter(providers)
    module.create_model_provider = (  # type: ignore[attr-defined]
        lambda provider_arg, model, thinking_level: next(it)
    )


async def _wait_for(condition, *, tries: int = 500) -> None:  # noqa: ANN001
    for _ in range(tries):
        if condition():
            return
        await asyncio.sleep(0.01)


async def test_settings_defaults_overrides_and_validation(tmp_path: Path) -> None:
    _load_runtime(tmp_path)
    module = _extension_module()
    load = module.load_subagent_settings  # type: ignore[attr-defined]
    Settings = module.SubagentSettings  # type: ignore[attr-defined]

    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    defaults = load(cwd, home=home)
    assert defaults == Settings()
    assert defaults.max_concurrent == 4
    assert defaults.default_max_turns is None
    assert defaults.grace_turns == 5
    assert defaults.default_join_mode == "smart"

    (home / ".tau").mkdir(parents=True)
    (cwd / ".tau").mkdir(parents=True)
    (home / ".tau" / "subagents.json").write_text(
        '{"maxConcurrent": 8, "graceTurns": 9, "defaultJoinMode": "group"}'
    )
    (cwd / ".tau" / "subagents.json").write_text('{"maxConcurrent": 2}')
    merged = load(cwd, home=home)
    assert merged.max_concurrent == 2  # project overrides global
    assert merged.grace_turns == 9  # inherited from global
    assert merged.default_join_mode == "group"

    # Project sets an out-of-range int (dropped) but leaves graceTurns unset.
    (cwd / ".tau" / "subagents.json").write_text(
        '{"maxConcurrent": 0, "defaultMaxTurns": 0}'
    )
    invalid = load(cwd, home=home)
    assert invalid.max_concurrent == 4  # 0 out of range dropped => default
    assert invalid.grace_turns == 9  # untouched, global value kept
    assert invalid.default_max_turns is None  # 0 => unlimited
    assert invalid.default_join_mode == "group"  # untouched global value kept

    # A wrong-typed value shadows the global and falls back to the default.
    (cwd / ".tau" / "subagents.json").write_text('{"graceTurns": "x"}')
    assert load(cwd, home=home).grace_turns == 5

    # A boolean is not a valid int even though bool subclasses int; the
    # shadowed global is dropped too, so the field falls back to its default.
    (cwd / ".tau" / "subagents.json").write_text('{"maxConcurrent": true}')
    assert load(cwd, home=home).max_concurrent == 4

    # Malformed project JSON is treated as empty, so only the global applies.
    (cwd / ".tau" / "subagents.json").write_text("not json {")
    assert load(cwd, home=home).max_concurrent == 8


async def test_background_queue_limits_concurrency(tmp_path: Path) -> None:
    runtime = _load_runtime(tmp_path)
    session = RecordingSession(tmp_path)
    runtime.bind(session)
    module = _extension_module()
    module.load_subagent_settings = (  # type: ignore[attr-defined]
        lambda cwd, home=None: module.SubagentSettings(max_concurrent=1)
    )
    release = asyncio.Event()
    _patch_provider_sequence(
        module,
        [
            BlockingProvider(release, "First done"),
            FakeProvider([_text_stream("Second done")]),
        ],
    )

    agent_tool = _agent_tool(runtime)
    first = await agent_tool.execute(
        {"prompt": "one", "description": "one", "run_in_background": True}
    )
    second = await agent_tool.execute(
        {"prompt": "two", "description": "two", "run_in_background": True}
    )
    assert "Agent started in background." in first.content
    assert "Agent queued in background." in second.content
    assert "Position: queued (max 1 concurrent)" in second.content

    release.set()
    await _wait_for(lambda: len(session.followed_up) >= 2)
    assert len(session.followed_up) == 2
    assert all("<status>completed</status>" in note for note in session.followed_up)


async def test_steer_unknown_and_completed(tmp_path: Path) -> None:
    runtime = _load_runtime(tmp_path)
    runtime.bind(RecordingSession(tmp_path))
    module = _extension_module()
    _patch_provider_factory(module, FakeProvider([_text_stream("done")]))

    steer_tool = _steer_tool(runtime)
    unknown = await steer_tool.execute({"agent_id": "nope", "message": "hi"})
    assert unknown.ok is False
    assert 'Agent not found: "nope". It may have been cleaned up.' in unknown.content

    agent_tool = _agent_tool(runtime)
    await agent_tool.execute({"prompt": "x", "description": "x"})
    completed = await steer_tool.execute({"agent_id": "agent-1", "message": "hi"})
    assert completed.ok is False
    assert 'is not running (status: completed)' in completed.content


async def test_steer_queued_run_is_delivered_after_drain(tmp_path: Path) -> None:
    runtime = _load_runtime(tmp_path)
    session = RecordingSession(tmp_path)
    runtime.bind(session)
    module = _extension_module()
    module.load_subagent_settings = (  # type: ignore[attr-defined]
        lambda cwd, home=None: module.SubagentSettings(max_concurrent=1)
    )
    release = asyncio.Event()
    queued_provider = FakeProvider([_text_stream("first"), _text_stream("second")])
    _patch_provider_sequence(
        module, [BlockingProvider(release, "First done"), queued_provider]
    )

    agent_tool = _agent_tool(runtime)
    await agent_tool.execute(
        {"prompt": "one", "description": "one", "run_in_background": True}
    )
    await agent_tool.execute(
        {"prompt": "two", "description": "two", "run_in_background": True}
    )

    steer_tool = _steer_tool(runtime)
    steered = await steer_tool.execute({"agent_id": "agent-2", "message": "go faster"})
    assert steered.ok is True
    assert "Steering message queued for agent agent-2" in steered.content

    release.set()
    await _wait_for(lambda: len(session.followed_up) >= 2)
    assert len(queued_provider.calls) >= 2
    assert UserMessage(content="go faster") in queued_provider.calls[1][2]


async def test_get_result_reports_queued_and_wait_follows_drain(tmp_path: Path) -> None:
    runtime = _load_runtime(tmp_path)
    session = RecordingSession(tmp_path)
    runtime.bind(session)
    module = _extension_module()
    module.load_subagent_settings = (  # type: ignore[attr-defined]
        lambda cwd, home=None: module.SubagentSettings(max_concurrent=1)
    )
    release = asyncio.Event()
    _patch_provider_sequence(
        module,
        [
            BlockingProvider(release, "First done"),
            FakeProvider([_text_stream("Second done")]),
        ],
    )

    agent_tool = _agent_tool(runtime)
    get_result = next(
        tool for tool in runtime.extension_tools if tool.name == "get_subagent_result"
    )
    await agent_tool.execute(
        {"prompt": "one", "description": "one", "run_in_background": True}
    )
    await agent_tool.execute(
        {"prompt": "two", "description": "two", "run_in_background": True}
    )

    queued = await get_result.execute({"agent_id": "agent-2"})
    assert queued.ok is True
    assert "[queued]" in queued.content
    assert "Still queued (max 1 concurrent)." in queued.content

    # wait=true follows the run through queued -> started -> finished.
    waiter = asyncio.create_task(
        get_result.execute({"agent_id": "agent-2", "wait": True})
    )
    await asyncio.sleep(0.02)
    release.set()
    waited = await waiter
    assert waited.ok is True
    assert "[completed]" in waited.content
    assert "Second done" in waited.content

    # The queued check did not consume agent-2's result (the wait did), so
    # only agent-1's completion notification is delivered.
    await _wait_for(lambda: any("agent-1" in note for note in session.followed_up))
    await asyncio.sleep(0.05)
    assert all("agent-1" in note for note in session.followed_up)


async def test_max_turns_soft_limit_wraps_up(tmp_path: Path) -> None:
    runtime = _load_runtime(tmp_path)
    runtime.bind(RecordingSession(tmp_path))
    module = _extension_module()
    provider = FakeProvider([_tool_call_stream("working", "t1"), _text_stream("Final answer")])
    _patch_provider_factory(module, provider)

    agent_tool = _agent_tool(runtime)
    result = await agent_tool.execute(
        {"prompt": "go", "description": "go", "max_turns": 1}
    )

    assert result.ok is True
    assert "[steered]" in result.content
    assert "Final answer" in result.content
    soft_message = module.SOFT_LIMIT_MESSAGE  # type: ignore[attr-defined]
    assert UserMessage(content=soft_message) in provider.calls[1][2]


async def test_max_turns_grace_abort(tmp_path: Path) -> None:
    runtime = _load_runtime(tmp_path)
    runtime.bind(RecordingSession(tmp_path))
    module = _extension_module()
    module.load_subagent_settings = (  # type: ignore[attr-defined]
        lambda cwd, home=None: module.SubagentSettings(grace_turns=1)
    )
    # More responses than the abort point allows: the run must stop early.
    provider = FakeProvider(
        [_tool_call_stream(f"t{i}", f"c{i}") for i in range(6)]
    )
    _patch_provider_factory(module, provider)

    agent_tool = _agent_tool(runtime)
    result = await agent_tool.execute(
        {"prompt": "loop", "description": "loop", "max_turns": 1}
    )

    assert result.ok is False
    assert "[aborted]" in result.content
    # Soft limit at turn 1, grace of 1 => hard cancel right after turn 2.
    assert "turns=2" in result.content
    assert len(provider.calls) == 2


async def test_resume_continues_session(tmp_path: Path) -> None:
    runtime = _load_runtime(tmp_path)
    runtime.bind(RecordingSession(tmp_path))
    module = _extension_module()
    provider = FakeProvider([_text_stream("First response"), _text_stream("Second response")])
    _patch_provider_factory(module, provider)

    agent_tool = _agent_tool(runtime)
    first = await agent_tool.execute({"prompt": "start", "description": "start"})
    assert first.ok is True
    assert "First response" in first.content

    resumed = await agent_tool.execute({"resume": "agent-1", "prompt": "keep going"})
    assert resumed.ok is True
    assert "Second response" in resumed.content
    assert "turns=2" in resumed.content

    unknown = await agent_tool.execute({"resume": "ghost", "prompt": "x"})
    assert unknown.ok is False
    assert 'Agent not found: "ghost". It may have been cleaned up.' in unknown.content


def test_extension_loads(tmp_path: Path) -> None:
    runtime = _load_runtime(tmp_path)

    assert runtime.extension_names == ("tau-subagents",)
    assert {tool.name for tool in runtime.extension_tools} == {
        "agent",
        "get_subagent_result",
        "steer_subagent",
    }
    registry = runtime.build_command_registry()
    assert registry.get("agents") is not None
    assert not [diag for diag in runtime.diagnostics if diag.severity == "error"]


async def test_foreground_run_returns_result(tmp_path: Path) -> None:
    runtime = _load_runtime(tmp_path)
    runtime.bind(RecordingSession(tmp_path))
    _patch_fake_provider(_extension_module(), response="Subagent report: all good.")

    agent_tool = next(tool for tool in runtime.extension_tools if tool.name == "agent")
    result = await agent_tool.execute(
        {"prompt": "Investigate the repo", "description": "investigate repo"}
    )

    assert result.ok is True
    assert "Subagent report: all good." in result.content
    assert "agent-1 [completed]" in result.content


async def test_background_run_delivers_notification(tmp_path: Path) -> None:
    runtime = _load_runtime(tmp_path)
    session = RecordingSession(tmp_path)
    runtime.bind(session)
    _patch_fake_provider(_extension_module(), response="Done.")

    agent_tool = next(tool for tool in runtime.extension_tools if tool.name == "agent")
    spawn_result = await agent_tool.execute(
        {
            "prompt": "Long task",
            "description": "long task",
            "run_in_background": True,
        }
    )
    assert spawn_result.ok is True
    assert "agent-1" in spawn_result.content

    for _ in range(200):
        if session.followed_up:
            break
        await asyncio.sleep(0.01)

    assert session.followed_up, "background completion should deliver a follow-up"
    assert "<task-notification>" in session.followed_up[0]
    assert "Done." in session.followed_up[0]


async def test_unknown_agent_type_is_reported(tmp_path: Path) -> None:
    runtime = _load_runtime(tmp_path)
    runtime.bind(RecordingSession(tmp_path))

    agent_tool = next(tool for tool in runtime.extension_tools if tool.name == "agent")
    result = await agent_tool.execute(
        {"prompt": "x", "description": "x", "subagent_type": "nope"}
    )

    assert result.ok is False
    assert "Unknown subagent_type" in result.content
