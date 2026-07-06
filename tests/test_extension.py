"""Tests for the tau-subagents extension.

Requires Tau's packages on the import path. With this repo's own env
(tau resolved via the pyproject path source): `uv run pytest`. Or borrow
a Tau checkout's env: `uv run --project /path/to/tau pytest tests/`.
"""

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tau_agent.messages import AssistantMessage, UserMessage

try:  # provider-usage seam (tau branch `provider-usage`)
    from tau_agent.messages import Usage
except ImportError:  # pragma: no cover - tau branch without the usage seam
    Usage = None  # type: ignore[assignment, misc]
from tau_agent.tools import ToolCall
from tau_ai import (
    FakeProvider,
    ProviderErrorEvent,
    ProviderResponseEndEvent,
    ProviderResponseStartEvent,
)
from tau_coding import TauResourcePaths
from tau_coding.extensions import ExtensionRuntime

pytestmark = pytest.mark.anyio

EXTENSION_DIR = Path(__file__).resolve().parent.parent / "src" / "tau_subagents"


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
        self.messages: list[object] = []
        self.followed_up_custom: list[
            tuple[str | None, dict[str, object] | None]
        ] = []

    def queue_steering_message(
        self,
        content: str,
        *,
        custom_type: str | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        del custom_type, details
        self.steered.append(content)

    def queue_follow_up_message(
        self,
        content: str,
        *,
        custom_type: str | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        self.followed_up.append(content)
        self.followed_up_custom.append((custom_type, details))

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


def _submodule(name: str) -> object:
    top = _extension_module()
    return sys.modules[f"{top.__name__}.{name}"]  # type: ignore[attr-defined]


def _prompts_module() -> object:
    return _submodule("prompts")


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for args in (
        ["init", "-q"],
        ["config", "user.email", "test@example.com"],
        ["config", "user.name", "Test"],
    ):
        subprocess.run(["git", *args], cwd=path, check=True)
    (path / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)


def _git_stdout(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout


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


def _patch_recording_provider(
    module: object, providers: list[object]
) -> tuple[list[object], list[object]]:
    """Patch provider factories, recording model and thinking_level per spawn."""
    models: list[object] = []
    thinking_levels: list[object] = []
    module.load_provider_settings = lambda: None  # type: ignore[attr-defined]

    def fake_resolve(settings, model=None):  # noqa: ANN001, ANN202
        models.append(model)
        return SimpleNamespace(provider=SimpleNamespace(name="fake"), model="fake")

    provider_iter = iter(providers)

    def fake_create(provider, model, thinking_level):  # noqa: ANN001, ANN202
        thinking_levels.append(thinking_level)
        return next(provider_iter)

    module.resolve_provider_selection = fake_resolve  # type: ignore[attr-defined]
    module.create_model_provider = fake_create  # type: ignore[attr-defined]
    return models, thinking_levels


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
        lambda cwd, home=None: module.SubagentSettings(
            max_concurrent=1, default_join_mode="async"
        )
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
        lambda cwd, home=None: module.SubagentSettings(
            max_concurrent=1, default_join_mode="async"
        )
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
        lambda cwd, home=None: module.SubagentSettings(
            max_concurrent=1, default_join_mode="async"
        )
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


async def test_group_join_full_delivery(tmp_path: Path) -> None:
    _load_runtime(tmp_path)
    module = _extension_module()
    delivered: list[tuple[list[object], bool]] = []
    join = module.GroupJoinManager(  # type: ignore[attr-defined]
        lambda records, partial: delivered.append((list(records), partial)),
        group_timeout=5.0,
        straggler_timeout=5.0,
    )
    first = SimpleNamespace(agent_id="a", result_consumed=False)
    second = SimpleNamespace(agent_id="b", result_consumed=False)
    loner = SimpleNamespace(agent_id="x", result_consumed=False)

    join.register_group("g1", ["a", "b"])
    assert join.on_agent_complete(loner) == "pass"
    assert join.on_agent_complete(first) == "held"
    assert join.on_agent_complete(second) == "delivered"
    assert delivered == [([first, second], False)]
    join.cancel_all()


async def test_group_join_timeout_partial_then_straggler(tmp_path: Path) -> None:
    _load_runtime(tmp_path)
    module = _extension_module()
    delivered: list[tuple[list[object], bool]] = []
    join = module.GroupJoinManager(  # type: ignore[attr-defined]
        lambda records, partial: delivered.append((list(records), partial)),
        group_timeout=0.05,
        straggler_timeout=0.02,
    )
    first = SimpleNamespace(agent_id="a", result_consumed=False)
    second = SimpleNamespace(agent_id="b", result_consumed=False)

    join.register_group("g1", ["a", "b"])
    assert join.on_agent_complete(first) == "held"
    await asyncio.sleep(0.1)
    assert delivered == [([first], True)]

    # The remaining member is now a straggler group with its own timeout.
    assert join.on_agent_complete(second) == "delivered"
    assert delivered[1] == ([second], False)
    join.cancel_all()


async def test_group_join_skips_consumed_members(tmp_path: Path) -> None:
    _load_runtime(tmp_path)
    module = _extension_module()
    delivered: list[tuple[list[object], bool]] = []
    join = module.GroupJoinManager(  # type: ignore[attr-defined]
        lambda records, partial: delivered.append((list(records), partial)),
        group_timeout=5.0,
        straggler_timeout=5.0,
    )
    consumed = SimpleNamespace(agent_id="a", result_consumed=True)
    fresh = SimpleNamespace(agent_id="b", result_consumed=False)
    join.register_group("g1", ["a", "b"])
    join.on_agent_complete(consumed)
    assert join.on_agent_complete(fresh) == "delivered"
    assert delivered == [([fresh], False)]

    both_consumed = [
        SimpleNamespace(agent_id="c", result_consumed=True),
        SimpleNamespace(agent_id="d", result_consumed=True),
    ]
    join.register_group("g2", ["c", "d"])
    for record in both_consumed:
        join.on_agent_complete(record)
    assert len(delivered) == 1  # all-consumed delivery is suppressed
    join.cancel_all()


async def test_smart_mode_consolidates_two_background_agents(tmp_path: Path) -> None:
    runtime = _load_runtime(tmp_path)
    session = RecordingSession(tmp_path)
    runtime.bind(session)
    module = _extension_module()
    _patch_provider_sequence(
        module,
        [
            FakeProvider([_text_stream("First done")]),
            FakeProvider([_text_stream("Second done")]),
        ],
    )

    agent_tool = _agent_tool(runtime)
    await agent_tool.execute(
        {"prompt": "one", "description": "one", "run_in_background": True}
    )
    await agent_tool.execute(
        {"prompt": "two", "description": "two", "run_in_background": True}
    )

    await _wait_for(lambda: session.followed_up)
    await asyncio.sleep(0.2)
    assert len(session.followed_up) == 1
    note = session.followed_up[0]
    assert "Background agent group completed: 2 agent(s) finished" in note
    assert "(partial" not in note
    assert "<agent-id>agent-1</agent-id>" in note
    assert "<agent-id>agent-2</agent-id>" in note
    assert "Use get_subagent_result for full output." in note


async def test_async_mode_sends_individual_notifications(tmp_path: Path) -> None:
    runtime = _load_runtime(tmp_path)
    session = RecordingSession(tmp_path)
    runtime.bind(session)
    module = _extension_module()
    module.load_subagent_settings = (  # type: ignore[attr-defined]
        lambda cwd, home=None: module.SubagentSettings(default_join_mode="async")
    )
    _patch_provider_sequence(
        module,
        [
            FakeProvider([_text_stream("First done")]),
            FakeProvider([_text_stream("Second done")]),
        ],
    )

    agent_tool = _agent_tool(runtime)
    await agent_tool.execute(
        {"prompt": "one", "description": "one", "run_in_background": True}
    )
    await agent_tool.execute(
        {"prompt": "two", "description": "two", "run_in_background": True}
    )

    await _wait_for(lambda: len(session.followed_up) >= 2)
    await asyncio.sleep(0.2)
    assert len(session.followed_up) == 2
    assert all("<task-notification>" in note for note in session.followed_up)
    assert not any("agent group completed" in note for note in session.followed_up)


async def test_smart_mode_partial_delivery_then_straggler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _load_runtime(tmp_path)
    session = RecordingSession(tmp_path)
    runtime.bind(session)
    module = _extension_module()
    monkeypatch.setattr(module, "GROUP_TIMEOUT_SECONDS", 0.15)
    monkeypatch.setattr(module, "STRAGGLER_TIMEOUT_SECONDS", 0.05)
    release = asyncio.Event()
    _patch_provider_sequence(
        module,
        [
            BlockingProvider(release, "Slow done"),
            FakeProvider([_text_stream("Fast done")]),
        ],
    )

    agent_tool = _agent_tool(runtime)
    await agent_tool.execute(
        {"prompt": "slow", "description": "slow", "run_in_background": True}
    )
    await agent_tool.execute(
        {"prompt": "fast", "description": "fast", "run_in_background": True}
    )

    await _wait_for(lambda: session.followed_up)
    partial_note = session.followed_up[0]
    assert "1 agent(s) finished (partial — others still running)" in partial_note
    assert "<agent-id>agent-2</agent-id>" in partial_note

    release.set()
    await _wait_for(lambda: len(session.followed_up) >= 2)
    straggler_note = session.followed_up[1]
    assert "1 agent(s) finished" in straggler_note
    assert "(partial" not in straggler_note
    assert "<agent-id>agent-1</agent-id>" in straggler_note


async def test_build_child_system_prompt_append_mode(tmp_path: Path) -> None:
    _load_runtime(tmp_path)
    module = _extension_module()
    prompts = _prompts_module()
    definition = module.AgentDefinition(  # type: ignore[attr-defined]
        name="helper",
        description="d",
        system_prompt="Do things.",
        prompt_mode="append",
    )

    environment = await prompts.detect_environment(tmp_path)  # type: ignore[attr-defined]
    assert environment == (
        "# Environment\n"
        f"Working directory: {tmp_path}\n"
        "Not a git repository\n"
        f"Platform: {sys.platform}"
    )

    prompt = module.build_child_system_prompt(  # type: ignore[attr-defined]
        definition,
        parent_prompt="PARENT PROMPT.",
        environment=environment,
        skill_blocks=[("foo", "FOO BLOCK")],
    )

    expected_prefix = (
        "PARENT PROMPT.\n\n"
        f"{prompts.SUB_AGENT_BRIDGE}\n\n"  # type: ignore[attr-defined]
        '<active_agent name="helper"/>\n\n'
        f"{environment}"
    )
    assert prompt.startswith(expected_prefix)
    assert "<agent_instructions>\nDo things.\n</agent_instructions>" in prompt
    # pi's extras suffix layout: three newlines before the first skill header.
    assert prompt.endswith("\n\n\n# Preloaded Skill: foo\nFOO BLOCK")

    # Without a body there is no <agent_instructions> section.
    bodyless = module.AgentDefinition(  # type: ignore[attr-defined]
        name="helper", description="d", prompt_mode="append"
    )
    prompt = module.build_child_system_prompt(  # type: ignore[attr-defined]
        bodyless, parent_prompt="PARENT.", environment="ENV", skill_blocks=[]
    )
    assert "<agent_instructions>" not in prompt

    # Without a parent prompt, append mode falls back to replace assembly.
    fallback = module.build_child_system_prompt(  # type: ignore[attr-defined]
        definition, parent_prompt=None, environment="", skill_blocks=[]
    )
    assert fallback == "Do things."


async def test_build_child_system_prompt_replace_mode(tmp_path: Path) -> None:
    _load_runtime(tmp_path)
    module = _extension_module()
    definition = module.AgentDefinition(  # type: ignore[attr-defined]
        name="helper", description="d", system_prompt="Body."
    )

    with_skills = module.build_child_system_prompt(  # type: ignore[attr-defined]
        definition,
        parent_prompt="PARENT.",
        environment="",
        skill_blocks=[("foo", "FOO BLOCK")],
    )
    assert with_skills == "Body.\n\n# Preloaded Skill: foo\nFOO BLOCK"

    plain = module.AgentDefinition(name="helper", description="d")  # type: ignore[attr-defined]
    assert (
        module.build_child_system_prompt(  # type: ignore[attr-defined]
            plain, parent_prompt="PARENT.", environment="", skill_blocks=[]
        )
        is None
    )


async def test_resolve_skill_blocks(tmp_path: Path) -> None:
    _load_runtime(tmp_path)
    module = _extension_module()
    cwd = tmp_path / "proj"
    (cwd / ".tau" / "skills").mkdir(parents=True)
    (cwd / ".tau" / "skills" / "foo.md").write_text(
        "---\ndescription: Foo skill\n---\nAlways foo."
    )
    home = tmp_path / "empty-home"

    blocks = module.resolve_skill_blocks(("foo", "missing"), cwd, home)  # type: ignore[attr-defined]

    assert blocks[0][0] == "foo"
    assert '<skill name="foo"' in blocks[0][1]
    assert "Always foo." in blocks[0][1]
    assert blocks[1] == ("missing", '(Skill "missing" not found)')


async def test_spawn_injects_skills_and_append_prompt(tmp_path: Path) -> None:
    runtime = _load_runtime(tmp_path)
    runtime.bind(RecordingSession(tmp_path))
    module = _extension_module()
    (tmp_path / ".tau" / "skills").mkdir(parents=True)
    (tmp_path / ".tau" / "skills" / "myskill.md").write_text("Skill body here.")
    (tmp_path / ".tau" / "agents").mkdir(parents=True)
    (tmp_path / ".tau" / "agents" / "skilled.md").write_text(
        "---\n"
        "description: Skilled agent\n"
        "skills: myskill\n"
        "prompt_mode: append\n"
        "---\n"
        "Use the skill."
    )
    provider = FakeProvider([_text_stream("done")])
    _patch_provider_factory(module, provider)

    agent_tool = _agent_tool(runtime)
    result = await agent_tool.execute(
        {"prompt": "go", "description": "go", "subagent_type": "skilled"}
    )

    assert result.ok is True
    system = provider.calls[0][1]
    assert system.startswith("You are Tau.\n\n<sub_agent_context>")
    assert "<agent_instructions>\nUse the skill.\n</agent_instructions>" in system
    assert "# Preloaded Skill: myskill" in system
    assert '<skill name="myskill"' in system
    assert "Skill body here." in system


async def test_worktree_create_and_cleanup_dirty(tmp_path: Path) -> None:
    _load_runtime(tmp_path)
    worktree_mod = _submodule("worktree")
    repo = tmp_path / "repo"
    _init_git_repo(repo)

    worktree = await worktree_mod.create_worktree(repo, "t1")  # type: ignore[attr-defined]
    assert worktree is not None
    assert worktree.branch == "tau-agent-t1"
    assert worktree.work_path.exists()
    assert worktree.repo == repo.resolve()

    (worktree.path / "new.txt").write_text("dirty\n")
    result = await worktree_mod.cleanup_worktree(worktree, "my task")  # type: ignore[attr-defined]

    assert result.has_changes is True
    assert result.branch == "tau-agent-t1"
    assert not worktree.path.exists()
    assert "tau-agent-t1" in _git_stdout(["branch", "--list", "tau-agent-t1"], repo)
    message = _git_stdout(["log", "-1", "--format=%s", "tau-agent-t1"], repo)
    assert message.strip() == "tau-agent: my task"


async def test_worktree_cleanup_clean_removes_without_branch(tmp_path: Path) -> None:
    _load_runtime(tmp_path)
    worktree_mod = _submodule("worktree")
    repo = tmp_path / "repo"
    _init_git_repo(repo)

    worktree = await worktree_mod.create_worktree(repo, "t2")  # type: ignore[attr-defined]
    assert worktree is not None
    result = await worktree_mod.cleanup_worktree(worktree, "task")  # type: ignore[attr-defined]

    assert result.has_changes is False
    assert not worktree.path.exists()
    assert _git_stdout(["branch", "--list", "tau-agent-t2"], repo).strip() == ""

    plain = tmp_path / "plain"
    plain.mkdir()
    assert await worktree_mod.create_worktree(plain, "t3") is None  # type: ignore[attr-defined]


async def test_worktree_spawn_fails_in_non_git_cwd(tmp_path: Path) -> None:
    runtime = _load_runtime(tmp_path)
    runtime.bind(RecordingSession(tmp_path))
    module = _extension_module()
    _patch_provider_factory(module, FakeProvider([_text_stream("unused")]))

    agent_tool = _agent_tool(runtime)
    result = await agent_tool.execute(
        {"prompt": "x", "description": "x", "isolation": "worktree"}
    )

    assert result.ok is False
    assert 'Cannot run with isolation: "worktree"' in result.content
    assert "Initialize git and commit at least once, or omit isolation." in result.content


async def test_worktree_isolation_runs_child_in_worktree(tmp_path: Path) -> None:
    runtime = _load_runtime(tmp_path)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    runtime.bind(RecordingSession(repo))
    module = _extension_module()
    provider = FakeProvider(
        [
            [
                ProviderResponseStartEvent(model="fake"),
                ProviderResponseEndEvent(
                    message=AssistantMessage(
                        content="checking",
                        tool_calls=[
                            ToolCall(id="c1", name="bash", arguments={"command": "pwd"})
                        ],
                    )
                ),
            ],
            _text_stream("done"),
        ]
    )
    _patch_provider_factory(module, provider)

    agent_tool = _agent_tool(runtime)
    result = await agent_tool.execute(
        {"prompt": "where am I", "description": "where", "isolation": "worktree"}
    )

    assert result.ok is True
    assert "tau-agent-agent-1" in str(provider.calls[1][2])  # pwd ran in the worktree
    assert "tau-agent" not in _git_stdout(["worktree", "list"], repo)
    assert _git_stdout(["branch", "--list", "tau-agent-agent-1"], repo).strip() == ""


async def test_background_worktree_failure_delivers_error_notification(
    tmp_path: Path,
) -> None:
    runtime = _load_runtime(tmp_path)
    session = RecordingSession(tmp_path)  # not a git repo
    runtime.bind(session)
    module = _extension_module()
    _patch_provider_factory(module, FakeProvider([_text_stream("unused")]))

    agent_tool = _agent_tool(runtime)
    spawn_result = await agent_tool.execute(
        {
            "prompt": "x",
            "description": "x",
            "isolation": "worktree",
            "run_in_background": True,
        }
    )
    assert spawn_result.ok is True
    assert "Agent started in background." in spawn_result.content

    await _wait_for(lambda: session.followed_up)
    note = session.followed_up[0]
    assert "<status>error</status>" in note
    assert 'Cannot run with isolation: "worktree"' in note


async def test_resume_blocked_for_worktree_agents(tmp_path: Path) -> None:
    runtime = _load_runtime(tmp_path)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    runtime.bind(RecordingSession(repo))
    module = _extension_module()
    _patch_provider_factory(module, FakeProvider([_text_stream("done")]))

    agent_tool = _agent_tool(runtime)
    first = await agent_tool.execute(
        {"prompt": "x", "description": "x", "isolation": "worktree"}
    )
    assert first.ok is True

    resumed = await agent_tool.execute({"resume": "agent-1", "prompt": "more"})
    assert resumed.ok is False
    assert (
        'Agent "agent-1" ran in an isolated worktree that has been cleaned up;'
        " resume is not supported for worktree agents." in resumed.content
    )


async def test_worktree_error_run_surfaces_branch_annotation(tmp_path: Path) -> None:
    runtime = _load_runtime(tmp_path)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    runtime.bind(RecordingSession(repo))
    module = _extension_module()
    provider = FakeProvider(
        [
            [
                ProviderResponseStartEvent(model="fake"),
                ProviderResponseEndEvent(
                    message=AssistantMessage(
                        content="working",
                        tool_calls=[
                            ToolCall(
                                id="c1",
                                name="bash",
                                arguments={"command": "echo dirty > newfile.txt"},
                            )
                        ],
                    )
                ),
            ],
            [
                ProviderResponseStartEvent(model="fake"),
                ProviderErrorEvent(message="boom"),
            ],
        ]
    )
    _patch_provider_factory(module, provider)

    agent_tool = _agent_tool(runtime)
    result = await agent_tool.execute(
        {"prompt": "break", "description": "break", "isolation": "worktree"}
    )

    assert result.ok is False
    assert "boom" in result.content
    assert "Changes saved to branch `tau-agent-agent-1`" in result.content
    assert "tau-agent-agent-1" in _git_stdout(
        ["branch", "--list", "tau-agent-agent-1"], repo
    )

    get_result = next(
        tool for tool in runtime.extension_tools if tool.name == "get_subagent_result"
    )
    fetched = await get_result.execute({"agent_id": "agent-1"})
    assert "Changes saved to branch `tau-agent-agent-1`" in fetched.content


async def test_output_file_streams_transcript(tmp_path: Path) -> None:
    runtime = _load_runtime(tmp_path)
    session = RecordingSession(tmp_path)
    runtime.bind(session)
    module = _extension_module()
    _patch_provider_factory(module, FakeProvider([_text_stream("Answer text")]))

    agent_tool = _agent_tool(runtime)
    spawn_result = await agent_tool.execute(
        {"prompt": "Long task", "description": "long", "run_in_background": True}
    )
    output_line = next(
        line for line in spawn_result.content.splitlines()
        if line.startswith("Output file: ")
    )
    output_path = Path(output_line.removeprefix("Output file: "))
    assert "tau-subagents-" in str(output_path)
    assert output_path.name == "agent-1.jsonl"
    output_mod = _submodule("output_file")
    assert output_mod.encode_cwd("/") == "root"  # type: ignore[attr-defined]

    await _wait_for(lambda: session.followed_up)
    note = session.followed_up[0]
    assert f"<output-file>{output_path}</output-file>" in note
    assert f"Full transcript available at: {output_path}" in note

    entries = [
        json.loads(line) for line in output_path.read_text().splitlines() if line
    ]
    assert entries[0]["type"] == "user"
    assert entries[0]["isSidechain"] is True
    assert entries[0]["message"]["content"] == "Long task"
    assert entries[0]["cwd"] == str(tmp_path)
    assert any(
        entry["type"] == "assistant" and "Answer text" in json.dumps(entry["message"])
        for entry in entries[1:]
    )

    get_result = next(
        tool for tool in runtime.extension_tools if tool.name == "get_subagent_result"
    )
    fetched = await get_result.execute({"agent_id": "agent-1"})
    assert f"Output file: {output_path}" in fetched.content


async def test_memory_dir_layout_and_validation(tmp_path: Path) -> None:
    _load_runtime(tmp_path)
    memory = _submodule("memory")
    home = tmp_path / "home"
    cwd = tmp_path / "proj"

    resolve = memory.resolve_memory_dir  # type: ignore[attr-defined]
    assert resolve("user", "helper", cwd, home) == home / ".tau" / "agent-memory" / "helper"
    assert resolve("project", "helper", cwd, home) == cwd / ".tau" / "agent-memory" / "helper"
    assert (
        resolve("local", "helper", cwd, home)
        == cwd / ".tau" / "agent-memory-local" / "helper"
    )
    assert resolve("global", "helper", cwd, home) is None
    assert resolve("user", "../evil", cwd, home) is None
    assert resolve("user", ".hidden", cwd, home) is None
    assert resolve("user", "a" * 129, cwd, home) is None


async def test_memory_block_builders(tmp_path: Path) -> None:
    _load_runtime(tmp_path)
    memory = _submodule("memory")
    memory_dir = tmp_path / "mem"

    empty = memory.build_memory_block(memory_dir, "project", None)  # type: ignore[attr-defined]
    assert empty.startswith("# Agent Memory\n\n")
    assert "Memory scope: project" in empty
    assert f"No MEMORY.md exists yet. Create one at {memory_dir}/MEMORY.md" in empty
    assert "MEMORY.md is your index. Keep it under 200 lines." in empty

    populated = memory.build_memory_block(memory_dir, "user", "my notes")  # type: ignore[attr-defined]
    assert "## Current MEMORY.md\nmy notes" in populated

    read_only = memory.build_read_only_memory_block(memory_dir, "user", None)  # type: ignore[attr-defined]
    assert read_only.startswith("# Agent Memory (read-only)")
    assert "No memory is available yet." in read_only
    assert "Memory instructions" not in read_only

    memory_dir.mkdir()
    (memory_dir / "MEMORY.md").write_text("\n".join(f"line{i}" for i in range(250)))
    content = memory.read_memory_file(memory_dir)  # type: ignore[attr-defined]
    assert content.endswith("... (truncated at 200 lines)")
    assert "line199" in content
    assert "line200\n" not in content


async def test_memory_injection_rw_and_ro(tmp_path: Path) -> None:
    runtime = _load_runtime(tmp_path)
    runtime.bind(RecordingSession(tmp_path))
    module = _extension_module()
    (tmp_path / ".tau" / "agents").mkdir(parents=True)
    (tmp_path / ".tau" / "agents" / "memo.md").write_text(
        "---\ndescription: RW memory agent\nmemory: project\n---\nRemember things."
    )
    (tmp_path / ".tau" / "agents" / "memoro.md").write_text(
        "---\ndescription: RO memory agent\nmemory: project\ntools: read\n---\nRead only."
    )
    rw_provider = FakeProvider([_text_stream("done")])
    ro_provider = FakeProvider([_text_stream("done")])
    _patch_provider_sequence(module, [rw_provider, ro_provider])

    agent_tool = _agent_tool(runtime)
    await agent_tool.execute(
        {"prompt": "go", "description": "go", "subagent_type": "memo"}
    )
    rw_system = rw_provider.calls[0][1]
    assert "# Agent Memory\n" in rw_system
    assert "read-only" not in rw_system
    assert (tmp_path / ".tau" / "agent-memory" / "memo").is_dir()

    await agent_tool.execute(
        {"prompt": "go", "description": "go", "subagent_type": "memoro"}
    )
    ro_system = ro_provider.calls[0][1]
    assert "# Agent Memory (read-only)" in ro_system
    assert not (tmp_path / ".tau" / "agent-memory" / "memoro").exists()
    ro_tool_names = {tool.name for tool in ro_provider.calls[0][3]}
    assert "read" in ro_tool_names
    assert "write" not in ro_tool_names


async def test_memory_block_precedes_skills_in_prompt(tmp_path: Path) -> None:
    _load_runtime(tmp_path)
    module = _extension_module()
    definition = module.AgentDefinition(  # type: ignore[attr-defined]
        name="helper", description="d", system_prompt="Body."
    )
    combined = module.build_child_system_prompt(  # type: ignore[attr-defined]
        definition,
        parent_prompt=None,
        environment="",
        skill_blocks=[("s", "SB")],
        memory_block="MEM",
    )
    assert combined == "Body.\n\nMEM\n\n# Preloaded Skill: s\nSB"


async def test_agent_frontmatter_memory_and_isolation(tmp_path: Path) -> None:
    _load_runtime(tmp_path)
    module = _extension_module()
    (tmp_path / ".tau" / "agents").mkdir(parents=True)
    (tmp_path / ".tau" / "agents" / "iso.md").write_text(
        "---\ndescription: x\nmemory: local\nisolation: worktree\n---\nBody."
    )
    (tmp_path / ".tau" / "agents" / "bad.md").write_text(
        "---\ndescription: x\nmemory: galactic\nisolation: docker\n---\nBody."
    )

    definitions = module.load_agent_definitions(tmp_path)  # type: ignore[attr-defined]
    assert definitions["iso"].memory == "local"
    assert definitions["iso"].isolation == "worktree"
    assert definitions["bad"].memory is None
    assert definitions["bad"].isolation is None


async def test_run_records_persisted(tmp_path: Path) -> None:
    runtime = _load_runtime(tmp_path)
    session = RecordingSession(tmp_path)
    runtime.bind(session)
    module = _extension_module()
    _patch_provider_sequence(
        module,
        [
            FakeProvider([_text_stream("fg done")]),
            FakeProvider([_text_stream("bg done")]),
        ],
    )

    agent_tool = _agent_tool(runtime)
    await agent_tool.execute({"prompt": "fg", "description": "fg task"})
    records = [
        data for namespace, data in session.custom_entries
        if namespace == "subagents:record"
    ]
    assert len(records) == 1
    assert records[0]["id"] == "agent-1"
    assert records[0]["status"] == "completed"
    assert records[0]["result"] == "fg done"
    assert records[0]["turns"] == 1

    await agent_tool.execute(
        {"prompt": "bg", "description": "bg task", "run_in_background": True}
    )
    await _wait_for(
        lambda: any(
            namespace == "subagents:record" and data["id"] == "agent-2"
            for namespace, data in session.custom_entries
        )
    )


async def test_model_and_thinking_param_precedence(tmp_path: Path) -> None:
    runtime = _load_runtime(tmp_path)
    runtime.bind(RecordingSession(tmp_path))
    module = _extension_module()
    (tmp_path / ".tau" / "agents").mkdir(parents=True)
    (tmp_path / ".tau" / "agents" / "pinned.md").write_text(
        "---\ndescription: Pinned agent\nmodel: pinned-model\nthinking: low\n---\nBody."
    )
    models, thinking_levels = _patch_recording_provider(
        module, [FakeProvider([_text_stream("done")]) for _ in range(3)]
    )

    agent_tool = _agent_tool(runtime)
    await agent_tool.execute(
        {"prompt": "x", "description": "x", "model": "haiku", "thinking": "high"}
    )
    assert models[-1] == "haiku"  # param used when frontmatter has none
    assert thinking_levels[-1] == "high"

    await agent_tool.execute({"prompt": "x", "description": "x"})
    assert models[-1] is None  # parent default
    assert thinking_levels[-1] == "medium"  # DEFAULT_THINKING_LEVEL

    await agent_tool.execute(
        {
            "prompt": "x",
            "description": "x",
            "subagent_type": "pinned",
            "model": "haiku",
            "thinking": "high",
        }
    )
    assert models[-1] == "pinned-model"  # frontmatter beats the param
    assert thinking_levels[-1] == "low"


async def test_invalid_thinking_rejected(tmp_path: Path) -> None:
    runtime = _load_runtime(tmp_path)
    runtime.bind(RecordingSession(tmp_path))

    agent_tool = _agent_tool(runtime)
    result = await agent_tool.execute(
        {"prompt": "x", "description": "x", "thinking": "ultra"}
    )

    assert result.ok is False
    assert "Invalid thinking level: ultra." in result.content
    assert "Valid options: off, minimal, low, medium, high, xhigh" in result.content

    get_result = next(
        tool for tool in runtime.extension_tools if tool.name == "get_subagent_result"
    )
    unknown = await get_result.execute({"agent_id": "agent-1"})
    assert unknown.ok is False  # nothing was spawned


async def test_skills_true_pins_discovery_to_parent_cwd_under_worktree(
    tmp_path: Path,
) -> None:
    runtime = _load_runtime(tmp_path)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    runtime.bind(RecordingSession(repo))
    module = _extension_module()
    # Created AFTER the commit: the parent cwd has this skill, but a detached
    # worktree checkout of HEAD does not.
    (repo / ".tau" / "skills").mkdir(parents=True)
    (repo / ".tau" / "skills" / "parentskill.md").write_text(
        "---\ndescription: Parent-only skill\n---\nDo parent things."
    )
    (repo / ".tau" / "agents").mkdir(exist_ok=True)
    (repo / ".tau" / "agents" / "pinned.md").write_text(
        "---\ndescription: Pins skills\nskills: true\nisolation: worktree\n---\nBody."
    )
    (repo / ".tau" / "agents" / "unpinned.md").write_text(
        "---\ndescription: Default discovery\nisolation: worktree\n---\nBody."
    )
    pinned_provider = FakeProvider([_text_stream("done")])
    unpinned_provider = FakeProvider([_text_stream("done")])
    _patch_provider_sequence(module, [pinned_provider, unpinned_provider])

    agent_tool = _agent_tool(runtime)
    pinned = await agent_tool.execute(
        {"prompt": "go", "description": "go", "subagent_type": "pinned"}
    )
    unpinned = await agent_tool.execute(
        {"prompt": "go", "description": "go", "subagent_type": "unpinned"}
    )

    assert pinned.ok is True
    assert unpinned.ok is True
    pinned_system = pinned_provider.calls[0][1]
    unpinned_system = unpinned_provider.calls[0][1]
    # skills: true resolves resources against the parent cwd, so the child
    # sees the uncommitted parent skill; default discovery resolves against
    # the worktree copy, which lacks it.
    assert "<name>parentskill</name>" in pinned_system
    assert "# Preloaded Skill:" not in pinned_system  # native index, not blocks
    assert "<name>parentskill</name>" not in unpinned_system


async def test_skills_none_disables_native_discovery(tmp_path: Path) -> None:
    runtime = _load_runtime(tmp_path)
    runtime.bind(RecordingSession(tmp_path))
    module = _extension_module()
    (tmp_path / ".tau" / "skills").mkdir(parents=True)
    (tmp_path / ".tau" / "skills" / "idxskill.md").write_text(
        "---\ndescription: Indexed skill\n---\nDo indexed things."
    )
    (tmp_path / ".tau" / "agents").mkdir(exist_ok=True)
    (tmp_path / ".tau" / "agents" / "noskills.md").write_text(
        "---\ndescription: No skills\nskills: none\n---\nBody."
    )
    control_provider = FakeProvider([_text_stream("done")])
    noskills_provider = FakeProvider([_text_stream("done")])
    _patch_provider_sequence(module, [control_provider, noskills_provider])

    agent_tool = _agent_tool(runtime)
    control = await agent_tool.execute({"prompt": "x", "description": "x"})
    noskills = await agent_tool.execute(
        {"prompt": "x", "description": "x", "subagent_type": "noskills"}
    )

    assert control.ok is True
    assert noskills.ok is True
    control_system = control_provider.calls[0][1]
    assert "<name>idxskill</name>" in control_system  # omitted => native discovery
    noskills_system = noskills_provider.calls[0][1]
    assert "<available_skills>" not in noskills_system
    assert "idxskill" not in noskills_system


async def test_named_skills_preload_disables_native_index(tmp_path: Path) -> None:
    runtime = _load_runtime(tmp_path)
    runtime.bind(RecordingSession(tmp_path))
    module = _extension_module()
    (tmp_path / ".tau" / "skills").mkdir(parents=True)
    (tmp_path / ".tau" / "skills" / "idxskill.md").write_text(
        "---\ndescription: Indexed skill\n---\nDo indexed things."
    )
    (tmp_path / ".tau" / "agents").mkdir(exist_ok=True)
    (tmp_path / ".tau" / "agents" / "preloader.md").write_text(
        "---\ndescription: Preloads\nskills: idxskill\n---\nBody."
    )
    provider = FakeProvider([_text_stream("done")])
    _patch_provider_factory(module, provider)

    agent_tool = _agent_tool(runtime)
    result = await agent_tool.execute(
        {"prompt": "x", "description": "x", "subagent_type": "preloader"}
    )

    assert result.ok is True
    system = provider.calls[0][1]
    assert "# Preloaded Skill: idxskill" in system
    assert "<available_skills>" not in system  # pi: named preload sets noSkills


async def test_skills_none_falls_back_without_seam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _load_runtime(tmp_path)
    runtime.bind(RecordingSession(tmp_path))
    module = _extension_module()
    (tmp_path / ".tau" / "skills").mkdir(parents=True)
    (tmp_path / ".tau" / "skills" / "idxskill.md").write_text(
        "---\ndescription: Indexed skill\n---\nDo indexed things."
    )
    (tmp_path / ".tau" / "agents").mkdir(exist_ok=True)
    (tmp_path / ".tau" / "agents" / "noskills.md").write_text(
        "---\ndescription: No skills\nskills: none\n---\nBody."
    )
    monkeypatch.setattr(module, "_supports_skills_enabled", lambda: False)
    provider = FakeProvider([_text_stream("done")])
    _patch_provider_factory(module, provider)

    agent_tool = _agent_tool(runtime)
    result = await agent_tool.execute(
        {"prompt": "x", "description": "x", "subagent_type": "noskills"}
    )

    # Against an older Tau without the seam, the spawn still works and
    # native discovery stays on.
    assert result.ok is True
    assert "<name>idxskill</name>" in provider.calls[0][1]


async def test_usage_surfaced_in_results_and_notifications(tmp_path: Path) -> None:
    runtime = _load_runtime(tmp_path)
    session = RecordingSession(tmp_path)
    runtime.bind(session)
    module = _extension_module()
    _patch_provider_sequence(
        module,
        [
            FakeProvider([_text_stream("fg done")]),
            FakeProvider([_text_stream("bg done")]),
        ],
    )

    agent_tool = _agent_tool(runtime)
    result = await agent_tool.execute({"prompt": "fg", "description": "fg"})
    assert "Agent completed in " in result.content
    assert "(0 tool uses, ~" in result.content
    assert "context tokens)." in result.content

    get_result = next(
        tool for tool in runtime.extension_tools if tool.name == "get_subagent_result"
    )
    fetched = await get_result.execute({"agent_id": "agent-1"})
    assert "Usage: 0 tool uses · ~" in fetched.content
    assert "context tokens" in fetched.content

    await agent_tool.execute(
        {"prompt": "bg", "description": "bg", "run_in_background": True}
    )
    await _wait_for(lambda: session.followed_up)
    note = session.followed_up[0]
    assert "<usage><tool_uses>0</tool_uses><context_tokens>" in note
    assert "<duration_ms>" in note


def _usage_stream(
    text: str, input_tokens: int, output: int, cache_write: int
) -> list[object]:
    return [
        ProviderResponseStartEvent(model="fake"),
        ProviderResponseEndEvent(
            message=AssistantMessage(
                content=text,
                usage=Usage(
                    input=input_tokens,
                    output=output,
                    cache_write=cache_write,
                    # cache_read must be excluded from lifetime totals.
                    cache_read=999,
                ),
            )
        ),
    ]


async def test_real_usage_accumulates_and_surfaces(tmp_path: Path) -> None:
    if Usage is None:
        pytest.skip("tau branch lacks the provider-usage seam")
    runtime = _load_runtime(tmp_path)
    session = RecordingSession(tmp_path)
    runtime.bind(session)
    module = _extension_module()
    _patch_provider_sequence(
        module,
        [
            FakeProvider(
                [
                    _usage_stream("fg done", 100, 20, 7),
                    _usage_stream("resumed done", 30, 10, 3),
                ]
            ),
            FakeProvider([_usage_stream("bg done", 50, 10, 0)]),
        ],
    )

    agent_tool = _agent_tool(runtime)
    result = await agent_tool.execute({"prompt": "fg", "description": "fg"})
    assert "(0 tool uses, 127 tokens)." in result.content

    get_result = next(
        tool for tool in runtime.extension_tools if tool.name == "get_subagent_result"
    )
    fetched = await get_result.execute({"agent_id": "agent-1"})
    assert "Usage: 127 tokens · 0 tool uses" in fetched.content

    # Resume keeps accumulating into the lifetime total (127 + 43 = 170),
    # matching pi, which preserves lifetimeUsage across resume.
    resumed = await agent_tool.execute({"resume": "agent-1", "prompt": "more"})
    assert "(0 tool uses, 170 tokens)." in resumed.content

    await agent_tool.execute(
        {"prompt": "bg", "description": "bg", "run_in_background": True}
    )
    await _wait_for(lambda: session.followed_up)
    note = session.followed_up[0]
    assert "<usage><total_tokens>60</total_tokens><tool_uses>0</tool_uses>" in note


async def test_foreground_run_emits_no_progress_updates(tmp_path: Path) -> None:
    # The spinner on the pending tool row (tau core) is the live activity
    # signal now; per-event "agent-n: turn n" updates were deliberately
    # removed as transcript noise.
    runtime = _load_runtime(tmp_path)
    runtime.bind(RecordingSession(tmp_path))
    module = _extension_module()
    provider = FakeProvider(
        [_tool_call_stream("working", "t1"), _text_stream("Final answer")]
    )
    _patch_provider_factory(module, provider)

    agent_tool = _agent_tool(runtime)
    updates: list[str] = []

    result = await agent_tool.execute(
        {"prompt": "go", "description": "d"},
        on_update=lambda message, data=None: updates.append(message),
    )

    assert result.ok is True
    assert updates == []


async def test_inherit_context_prepends_parent_conversation(tmp_path: Path) -> None:
    try:
        from tau_coding.extensions.api import ExtensionContext
    except ImportError:
        pytest.skip("tau branch lacks the parent-context seam")
    if not hasattr(ExtensionContext, "transcript"):
        pytest.skip("tau branch lacks the parent-context seam")
    runtime = _load_runtime(tmp_path)
    session = RecordingSession(tmp_path)
    session.messages = [
        UserMessage(content="parent question"),
        AssistantMessage(content="parent answer"),
    ]
    runtime.bind(session)
    module = _extension_module()
    provider = FakeProvider([_text_stream("done")])
    _patch_provider_factory(module, provider)

    agent_tool = _agent_tool(runtime)
    result = await agent_tool.execute(
        {"prompt": "child task", "description": "d", "inherit_context": True}
    )

    assert result.ok is True
    child_messages = provider.calls[0][2]
    first = child_messages[0]
    assert first.content.startswith("# Parent Conversation Context")
    assert "[User]: parent question" in first.content
    assert "[Assistant]: parent answer" in first.content
    assert "# Your Task (below)\nchild task" in first.content


def test_notification_renderer_formats_details(tmp_path: Path) -> None:
    _load_runtime(tmp_path)
    render = _submodule("notification_render").render_notification
    details = {
        "description": "deploy watch",
        "status": "completed",
        "turn_count": 3,
        "max_turns": 10,
        "tool_uses": 2,
        "total_tokens": 1500,
        "duration_ms": 2300,
        "output_file": "/tmp/t.jsonl",
        "error": None,
        "result_preview": "line one\nline two",
    }
    view = SimpleNamespace(details=details)

    collapsed = render(view, SimpleNamespace(expanded=False))
    assert "[green]✓[/green]" in collapsed
    assert "[bold]deploy watch[/bold]" in collapsed
    assert "3/10 turns" in collapsed
    assert "1.5k tokens" in collapsed
    assert "⎿  line one" in collapsed
    assert "line two" not in collapsed
    assert "transcript: /tmp/t.jsonl" in collapsed

    expanded = render(view, SimpleNamespace(expanded=True))
    assert "line two" in expanded

    error_view = SimpleNamespace(details={**details, "status": "error"})
    assert "[red]✗[/red]" in render(error_view, SimpleNamespace(expanded=False))

    grouped = SimpleNamespace(
        details={**details, "others": [{**details, "description": "second run"}]}
    )
    both = render(grouped, SimpleNamespace(expanded=False))
    assert "deploy watch" in both
    assert "second run" in both

    assert render(SimpleNamespace(details=None), SimpleNamespace(expanded=False)) is None


async def test_notification_delivered_as_custom_message(tmp_path: Path) -> None:
    try:
        from tau_coding.extensions import CustomMessageView  # noqa: F401
    except ImportError:
        pytest.skip("tau branch lacks the message-renderers seam")
    runtime = _load_runtime(tmp_path)
    session = RecordingSession(tmp_path)
    runtime.bind(session)
    _patch_provider_factory(_extension_module(), FakeProvider([_text_stream("done")]))

    agent_tool = _agent_tool(runtime)
    await agent_tool.execute(
        {"prompt": "bg", "description": "bg task", "run_in_background": True}
    )
    await _wait_for(lambda: session.followed_up)

    custom_type, details = session.followed_up_custom[0]
    assert custom_type == "subagent-notification"
    assert details is not None
    assert details["description"] == "bg task"
    assert details["status"] == "completed"
    assert details["result_preview"] == "done"
    # The raw XML content still enters context for the model.
    assert "<task-notification>" in session.followed_up[0]


class ScriptedUi:
    """Scripted DialogUi fake for menu tests."""

    def __init__(self, selects, confirms=(), inputs=()) -> None:  # noqa: ANN001
        self.selects = list(selects)
        self.confirms = list(confirms)
        self.inputs = list(inputs)
        self.select_calls: list[tuple[str, tuple[str, ...]]] = []
        self.notifications: list[str] = []

    @property
    def has_ui(self) -> bool:
        return True

    def notify(self, message: str, level: str = "info") -> None:
        self.notifications.append(message)

    async def select(self, title, options, *, timeout=None):  # noqa: ANN001, ANN202
        self.select_calls.append((title, tuple(options)))
        answer = self.selects.pop(0)
        return answer(options) if callable(answer) else answer

    async def confirm(self, title, message, *, timeout=None):  # noqa: ANN001, ANN202
        return self.confirms.pop(0)

    async def input(self, title, placeholder="", *, timeout=None):  # noqa: ANN001, ANN202
        return self.inputs.pop(0)


def _menu_run(module, **overrides):  # noqa: ANN001, ANN202
    defaults = {
        "agent_id": "agent-1",
        "agent_type": "general",
        "description": "task",
        "prompt": "p",
        "background": True,
    }
    defaults.update(overrides)
    return module.AgentRun(**defaults)


async def test_agents_menu_stops_running_agent(tmp_path: Path) -> None:
    _load_runtime(tmp_path)
    menu = _submodule("agents_menu")
    run = _menu_run(_extension_module(), status="running")
    manager = SimpleNamespace(runs={"agent-1": run}, definitions=dict)
    ui = ScriptedUi(
        selects=[
            lambda options: options[0],  # top: Running agents (…)
            lambda options: options[0],  # the run
            "Stop",
            None,  # leave run list
            None,  # leave top menu
        ],
        confirms=[True],
    )

    await menu.show_agents_menu(manager, ui)

    assert run.aborted is True
    assert any("Stopped" in note for note in ui.notifications)


async def test_agents_menu_steers_queued_agent(tmp_path: Path) -> None:
    _load_runtime(tmp_path)
    menu = _submodule("agents_menu")
    run = _menu_run(_extension_module(), status="queued")
    manager = SimpleNamespace(runs={"agent-1": run}, definitions=dict)
    ui = ScriptedUi(
        selects=[
            lambda options: options[0],
            lambda options: options[0],
            "Steer…",
            None,
            None,
        ],
        inputs=["focus on tests"],
    )

    await menu.show_agents_menu(manager, ui)

    assert run.pending_steers == ["focus on tests"]


async def test_agents_menu_shows_finished_result(tmp_path: Path) -> None:
    _load_runtime(tmp_path)
    menu = _submodule("agents_menu")
    run = _menu_run(
        _extension_module(), status="completed", result_text="All done here"
    )
    manager = SimpleNamespace(runs={"agent-1": run}, definitions=dict)
    ui = ScriptedUi(
        selects=[
            lambda options: options[0],
            lambda options: options[0],
            "View result",
            None,
            None,
        ],
    )

    await menu.show_agents_menu(manager, ui)

    assert any("All done here" in note for note in ui.notifications)
    assert menu.supports_menu(ui) is True
    assert menu.supports_menu(None) is False


class TranscriptScriptedUi(ScriptedUi):
    """ScriptedUi that also supports the show_transcript seam."""

    def __init__(self, *args, transcript_results=(), **kwargs) -> None:  # noqa: ANN001, ANN002, ANN003
        super().__init__(*args, **kwargs)
        self.transcript_results = list(transcript_results)
        self.transcript_calls: list[tuple[str, tuple, object]] = []

    async def show_transcript(self, title, messages, *, poll=None, timeout=None):  # noqa: ANN001, ANN202
        self.transcript_calls.append((title, tuple(messages), poll))
        return self.transcript_results.pop(0)


async def test_agents_menu_opens_live_transcript_on_run_selection(tmp_path: Path) -> None:
    _load_runtime(tmp_path)
    menu = _submodule("agents_menu")
    child_messages = [
        UserMessage(content="child prompt"),
        AssistantMessage(content="child progress"),
    ]
    run = _menu_run(_extension_module(), status="running")
    run.session = SimpleNamespace(messages=child_messages)
    manager = SimpleNamespace(runs={"agent-1": run}, definitions=dict)
    ui = TranscriptScriptedUi(
        selects=[
            lambda options: options[0],  # top: Running agents (…)
            lambda options: options[0],  # the run → opens the transcript
            None,  # leave run list
            None,  # leave top menu
        ],
        transcript_results=[False],  # Escape: back to the run list, no actions
    )

    await menu.show_agents_menu(manager, ui)

    (title, messages, poll) = ui.transcript_calls[0]
    assert "agent-1" in title
    assert messages == tuple(child_messages)
    assert poll is not None
    assert poll() == tuple(child_messages)
    # Escape must not open the action submenu.
    assert not any(title.startswith("agent-1 [") for title, _ in ui.select_calls)


async def test_agents_menu_transcript_enter_opens_actions(tmp_path: Path) -> None:
    _load_runtime(tmp_path)
    menu = _submodule("agents_menu")
    run = _menu_run(_extension_module(), status="completed", result_text="All done here")
    run.session = SimpleNamespace(messages=[UserMessage(content="p")])
    manager = SimpleNamespace(runs={"agent-1": run}, definitions=dict)
    ui = TranscriptScriptedUi(
        selects=[
            lambda options: options[0],  # top: Running agents (…)
            lambda options: options[0],  # the run → opens the transcript
            "View result",  # action submenu, reached via Enter
            None,  # leave run list
            None,  # leave top menu
        ],
        transcript_results=[True],  # Enter: open the action submenu
    )

    await menu.show_agents_menu(manager, ui)

    assert any("All done here" in note for note in ui.notifications)


async def test_agents_menu_transcript_synthesizes_when_session_gone(tmp_path: Path) -> None:
    _load_runtime(tmp_path)
    menu = _submodule("agents_menu")
    run = _menu_run(
        _extension_module(),
        status="completed",
        result_text="Final report",
        prompt="original task",
    )
    assert run.session is None
    manager = SimpleNamespace(runs={"agent-1": run}, definitions=dict)
    ui = TranscriptScriptedUi(
        selects=[
            lambda options: options[0],
            lambda options: options[0],
            None,
            None,
        ],
        transcript_results=[False],
    )

    await menu.show_agents_menu(manager, ui)

    (_, messages, poll) = ui.transcript_calls[0]
    assert poll is None
    assert [type(m).__name__ for m in messages] == ["UserMessage", "AssistantMessage"]
    assert messages[0].content == "original task"
    assert messages[1].content == "Final report"


def test_run_transcript_source_maps_live_and_terminal_runs() -> None:
    extension = _extension_module()

    live = _menu_run(extension, status="running")
    steered: list[str] = []
    live.session = SimpleNamespace(
        messages=[UserMessage(content="child prompt")],
        queue_steering_message=steered.append,
    )
    live.revision = 7
    source = extension.run_transcript_source(live)
    assert (source.id, source.label, source.detail) == ("agent-1", "general", "task")
    assert source.status == "running"
    assert source.revision == 7
    assert source.messages() == (UserMessage(content="child prompt"),)
    source.steer("focus on tests")
    assert steered == ["focus on tests"]

    done = _menu_run(extension, status="completed", result_text="Final report")
    source = extension.run_transcript_source(done)
    assert source.status == "done"
    assert source.steer is None
    snapshot = source.messages()
    assert snapshot is not None
    assert snapshot[0].content == "p"
    assert snapshot[1].content == "Final report"

    queued = _menu_run(extension, status="queued")
    source = extension.run_transcript_source(queued)
    assert source.status == "queued"
    assert source.messages() == (UserMessage(content="p"),)
    source.steer("get ahead of it")
    assert queued.pending_steers == ["get ahead of it"]

    aborted = _menu_run(extension, status="aborted")
    assert extension.run_transcript_source(aborted).status == "cancelled"


async def test_transcript_sources_published_and_signalled(tmp_path: Path) -> None:
    runtime = _load_runtime(tmp_path)
    session = RecordingSession(tmp_path)
    runtime.bind(session)
    fired: list[int] = []
    runtime.set_transcript_sources_changed_callback(lambda: fired.append(1))
    _patch_provider_factory(_extension_module(), FakeProvider([_text_stream("done")]))

    agent_tool = _agent_tool(runtime)
    await agent_tool.execute({"prompt": "task", "description": "map the code"})

    assert fired  # spawn + completion pushed the sources-changed signal
    sources = runtime.transcript_sources()
    assert len(sources) == 1
    assert sources[0].id == "agent-1"
    assert sources[0].status == "done"
    assert sources[0].detail == "map the code"
    messages = sources[0].messages()
    assert messages is not None and len(messages) >= 2


async def test_agents_menu_prefers_in_place_view(tmp_path: Path) -> None:
    _load_runtime(tmp_path)
    menu = _submodule("agents_menu")
    run = _menu_run(_extension_module(), status="running")
    manager = SimpleNamespace(runs={"agent-1": run}, definitions=dict)

    class InPlaceUi(TranscriptScriptedUi):
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            super().__init__(*args, **kwargs)
            self.viewed: list[str] = []

        async def view_transcript(self, source_id: str) -> bool:
            self.viewed.append(source_id)
            return True

    ui = InPlaceUi(
        selects=[
            lambda options: options[0],  # top: Running agents (…)
            lambda options: options[0],  # the run → switches the main view
        ],
    )

    await menu.show_agents_menu(manager, ui)

    # The whole menu unwound so the user lands in the in-place view; the
    # modal fallback and the action submenu never opened.
    assert ui.viewed == ["agent-1"]
    assert ui.transcript_calls == []
    assert len(ui.select_calls) == 2


def test_render_call_lines() -> None:
    extension = _extension_module()

    assert (
        extension.render_agent_call(
            {"subagent_type": "explore", "description": "Summarize codebase"}
        )
        == "▸ explore agent · Summarize codebase"
    )
    assert extension.render_agent_call({"prompt": "x"}) == "▸ general agent"
    assert (
        extension.render_agent_call(
            {"description": "Daily check", "schedule": "0 9 * * 1"}
        )
        == "▸ general agent (scheduled 0 9 * * 1) · Daily check"
    )
    assert (
        extension.render_get_result_call({"agent_id": "agent-3", "wait": True})
        == "▸ get result · agent-3 (wait)"
    )
    steer_line = extension.render_steer_call(
        {"agent_id": "agent-3", "message": "focus " * 30}
    )
    assert steer_line.startswith("▸ steer agent-3 · focus")
    assert len(steer_line) < 90


def test_registered_tools_carry_render_call(tmp_path: Path) -> None:
    runtime = _load_runtime(tmp_path)

    line = runtime.render_tool_call(
        "agent", {"subagent_type": "explore", "description": "Summarize codebase"}
    )

    assert line == "▸ explore agent · Summarize codebase"


async def test_inherit_context_skips_empty_parent(tmp_path: Path) -> None:
    runtime = _load_runtime(tmp_path)
    runtime.bind(RecordingSession(tmp_path))
    module = _extension_module()
    provider = FakeProvider([_text_stream("done")])
    _patch_provider_factory(module, provider)

    agent_tool = _agent_tool(runtime)
    result = await agent_tool.execute(
        {"prompt": "child task", "description": "d", "inherit_context": True}
    )

    if not result.ok:
        assert "parent-context seam" in result.content
        pytest.skip("tau branch lacks the parent-context seam")
    first = provider.calls[0][2][0]
    assert first.content == "child task"


async def test_consuming_within_nudge_window_suppresses_notification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _load_runtime(tmp_path)
    session = RecordingSession(tmp_path)
    runtime.bind(session)
    module = _extension_module()
    module.load_subagent_settings = (  # type: ignore[attr-defined]
        lambda cwd, home=None: module.SubagentSettings(default_join_mode="async")
    )
    monkeypatch.setattr(module, "NUDGE_HOLD_SECONDS", 0.3)
    _patch_provider_factory(module, FakeProvider([_text_stream("done")]))

    agent_tool = _agent_tool(runtime)
    await agent_tool.execute(
        {"prompt": "x", "description": "x", "run_in_background": True}
    )
    get_result = next(
        tool for tool in runtime.extension_tools if tool.name == "get_subagent_result"
    )
    fetched = None
    for _ in range(500):
        fetched = await get_result.execute({"agent_id": "agent-1"})
        if "[completed]" in fetched.content:
            break
        await asyncio.sleep(0.01)
    assert fetched is not None and "[completed]" in fetched.content

    # The read consumed the result inside the hold window; no nudge arrives.
    await asyncio.sleep(0.5)
    assert session.followed_up == []


async def test_nudge_arrives_when_unconsumed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _load_runtime(tmp_path)
    session = RecordingSession(tmp_path)
    runtime.bind(session)
    module = _extension_module()
    module.load_subagent_settings = (  # type: ignore[attr-defined]
        lambda cwd, home=None: module.SubagentSettings(default_join_mode="async")
    )
    monkeypatch.setattr(module, "NUDGE_HOLD_SECONDS", 0.05)
    _patch_provider_factory(module, FakeProvider([_text_stream("done")]))

    agent_tool = _agent_tool(runtime)
    await agent_tool.execute(
        {"prompt": "x", "description": "x", "run_in_background": True}
    )

    await _wait_for(lambda: session.followed_up)
    assert "<task-notification>" in session.followed_up[0]


async def test_shutdown_cancels_pending_nudges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _load_runtime(tmp_path)
    session = RecordingSession(tmp_path)
    runtime.bind(session)
    module = _extension_module()
    module.load_subagent_settings = (  # type: ignore[attr-defined]
        lambda cwd, home=None: module.SubagentSettings(default_join_mode="async")
    )
    monkeypatch.setattr(module, "NUDGE_HOLD_SECONDS", 0.5)
    _patch_provider_factory(module, FakeProvider([_text_stream("done")]))

    agent_tool = _agent_tool(runtime)
    await agent_tool.execute(
        {"prompt": "x", "description": "x", "run_in_background": True}
    )
    # The record is persisted just before the nudge is scheduled.
    await _wait_for(
        lambda: any(
            namespace == "subagents:record"
            for namespace, _data in session.custom_entries
        )
    )

    await runtime.emit_session_shutdown("new")
    await asyncio.sleep(0.7)
    assert session.followed_up == []


def test_extension_loads(tmp_path: Path) -> None:
    runtime = _load_runtime(tmp_path)

    assert runtime.extension_names == ("tau_subagents",)
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
