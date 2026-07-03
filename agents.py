"""Agent type definitions for the subagents extension.

Agent types are markdown files with frontmatter, mirroring pi-subagents:

    .tau/agents/<name>.md          project agent types (win on conflicts)
    ~/.tau/agents/<name>.md        user agent types

The filename is the type name, the body is the subagent's system prompt, and
frontmatter supports `description`, `tools` (comma-separated allow-list of
built-in tool names, or `*`), `model`, `max_turns`, `skills` (comma-separated
skill names to preload), `prompt_mode` (`replace` or `append`), `memory`
(`user`, `project`, or `local`), and `isolation` (`worktree`).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tau_coding.resources import parse_markdown_resource


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    """One spawnable subagent type."""

    name: str
    description: str
    system_prompt: str | None = None
    tools: tuple[str, ...] | None = None
    model: str | None = None
    max_turns: int | None = None
    skills: tuple[str, ...] | None = None
    prompt_mode: str = "replace"
    memory: str | None = None
    isolation: str | None = None


DEFAULT_AGENT_TYPES: tuple[AgentDefinition, ...] = (
    AgentDefinition(
        name="general",
        description=(
            "General-purpose agent with the full coding toolset for research and"
            " multi-step tasks."
        ),
    ),
    AgentDefinition(
        name="explore",
        description="Read-only agent for searching and summarizing code without editing.",
        tools=("read", "bash"),
        system_prompt=(
            "You are a read-only exploration agent. Investigate the codebase and"
            " report findings. Never modify files; use bash only for read-only"
            " commands such as ls, grep, find, and git log."
        ),
    ),
)


def load_agent_definitions(cwd: Path, home: Path | None = None) -> dict[str, AgentDefinition]:
    """Load agent types: built-in defaults, then user files, then project files."""
    definitions = {definition.name: definition for definition in DEFAULT_AGENT_TYPES}
    home_dir = home if home is not None else Path.home()
    for agents_dir in (home_dir / ".tau" / "agents", cwd / ".tau" / "agents"):
        if not agents_dir.is_dir():
            continue
        for path in sorted(agents_dir.glob("*.md")):
            definition = _load_definition(path)
            if definition is not None:
                definitions[definition.name] = definition
    return definitions


def format_agent_type_list(definitions: dict[str, AgentDefinition]) -> str:
    """Format agent types for tool descriptions and the /agents command."""
    lines = []
    for name in sorted(definitions):
        definition = definitions[name]
        lines.append(f"- {name}: {definition.description}")
    return "\n".join(lines)


def _load_definition(path: Path) -> AgentDefinition | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    metadata, body = parse_markdown_resource(raw)
    tools = _parse_tools(metadata.get("tools"))
    return AgentDefinition(
        name=path.stem,
        description=metadata.get("description") or f"Custom agent type from {path}",
        system_prompt=body.strip() or None,
        tools=tools,
        model=metadata.get("model") or None,
        max_turns=_parse_max_turns(metadata.get("max_turns")),
        skills=_parse_skills(metadata.get("skills")),
        prompt_mode="append" if metadata.get("prompt_mode") == "append" else "replace",
        memory=(
            metadata.get("memory")
            if metadata.get("memory") in ("user", "project", "local")
            else None
        ),
        isolation="worktree" if metadata.get("isolation") == "worktree" else None,
    )


def _parse_tools(raw: str | None) -> tuple[str, ...] | None:
    if raw is None or raw.strip() in ("", "*", "all"):
        return None
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _parse_skills(raw: str | None) -> tuple[str, ...] | None:
    """Parse the `skills:` CSV; full inheritance (`*`/`all`/`true`) not yet supported."""
    if raw is None:
        return None
    stripped = raw.strip().lower()
    if stripped in ("", "none", "false", "*", "all", "true"):
        return None
    return tuple(part.strip() for part in raw.split(",") if part.strip()) or None


def _parse_max_turns(raw: str | None) -> int | None:
    """Parse a non-negative int turn limit (0 = unlimited); invalid → None."""
    if raw is None:
        return None
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None
