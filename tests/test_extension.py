"""Tests for the tau-subagents extension.

Requires Tau's packages on the import path; run from a Tau checkout:

    uv run --project /path/to/tau pytest tests/
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tau_agent.messages import AssistantMessage
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


def test_extension_loads(tmp_path: Path) -> None:
    runtime = _load_runtime(tmp_path)

    assert runtime.extension_names == ("tau-subagents",)
    assert {tool.name for tool in runtime.extension_tools} == {
        "agent",
        "get_subagent_result",
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
