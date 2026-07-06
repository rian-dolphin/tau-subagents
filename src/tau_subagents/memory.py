"""Per-agent persistent memory, ported from pi-subagents.

Agent types opt in with frontmatter `memory: user|project|local`, mapping to:

    user      ~/.tau/agent-memory/<name>/
    project   <cwd>/.tau/agent-memory/<name>/
    local     <cwd>/.tau/agent-memory-local/<name>/

The agent maintains its memory with the ordinary read/write/edit tools; a
prompt block injected at spawn describes the directory and shows the first
200 lines of MEMORY.md. Agents whose toolset cannot write get a read-only
variant and no directory is created for them.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

MEMORY_SCOPES = ("user", "project", "local")
MAX_MEMORY_LINES = 200
MEMORY_TRUNCATION_SUFFIX = "\n... (truncated at 200 lines)"

MEMORY_INSTRUCTIONS = (
    "## Memory instructions\n"
    "- MEMORY.md is your index. Keep it under 200 lines.\n"
    "- Store detailed memories in separate files in the memory directory and"
    " link them from MEMORY.md.\n"
    "- Start each memory file with frontmatter: name, description, and type"
    " (user|feedback|project|reference).\n"
    "- Update or remove outdated memories when you encounter them.\n"
    "- Maintain these files with the read/write/edit tools."
)

_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


def is_valid_agent_name(name: str) -> bool:
    """Return True when name is safe to use as a memory directory component."""
    return len(name) <= 128 and _NAME_PATTERN.match(name) is not None


def resolve_memory_dir(
    scope: str, agent_name: str, cwd: Path, home: Path | None = None
) -> Path | None:
    """Map a memory scope to its directory; None for invalid scope or name."""
    if scope not in MEMORY_SCOPES or not is_valid_agent_name(agent_name):
        return None
    home_dir = home if home is not None else Path.home()
    if scope == "user":
        base = home_dir / ".tau" / "agent-memory"
    elif scope == "project":
        base = cwd / ".tau" / "agent-memory"
    else:
        base = cwd / ".tau" / "agent-memory-local"
    memory_dir = base / agent_name
    if memory_dir.is_symlink():
        return None
    return memory_dir


def read_memory_file(memory_dir: Path) -> str | None:
    """Read MEMORY.md capped at MAX_MEMORY_LINES; None when absent/unreadable."""
    try:
        text = (memory_dir / "MEMORY.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    lines = text.splitlines()
    if len(lines) > MAX_MEMORY_LINES:
        return "\n".join(lines[:MAX_MEMORY_LINES]) + MEMORY_TRUNCATION_SUFFIX
    return text


def build_memory_block(memory_dir: Path, scope: str, existing: str | None) -> str:
    """Build the read-write memory prompt block."""
    header = (
        "# Agent Memory\n\n"
        f"You have a persistent memory directory at: {memory_dir}/\n"
        f"Memory scope: {scope}\n\n"
        "This memory persists across sessions. Use it to build up knowledge"
        " over time."
    )
    if existing:
        current = f"\n\n## Current MEMORY.md\n{existing}"
    else:
        current = (
            f"\n\nNo MEMORY.md exists yet. Create one at {memory_dir}/MEMORY.md"
            " to start building persistent memory."
        )
    return f"{header}{current}\n\n{MEMORY_INSTRUCTIONS}"


def build_read_only_memory_block(
    memory_dir: Path, scope: str, existing: str | None
) -> str:
    """Build the read-only memory prompt block."""
    header = (
        "# Agent Memory (read-only)\n\n"
        f"Memory directory: {memory_dir}/\n"
        f"Memory scope: {scope}\n\n"
        "You have read-only access to this memory; do not modify it."
    )
    if existing:
        return f"{header}\n\n## Current MEMORY.md\n{existing}"
    return f"{header}\n\nNo memory is available yet."


async def prepare_memory(
    agent_name: str,
    scope: str,
    cwd: Path,
    *,
    read_write: bool,
    home: Path | None = None,
) -> str | None:
    """Resolve, optionally create, and render the memory block for one spawn."""
    return await asyncio.to_thread(
        _prepare_memory_blocking, agent_name, scope, cwd, read_write, home
    )


def _prepare_memory_blocking(
    agent_name: str, scope: str, cwd: Path, read_write: bool, home: Path | None
) -> str | None:
    memory_dir = resolve_memory_dir(scope, agent_name, cwd, home)
    if memory_dir is None:
        return None
    if read_write:
        try:
            memory_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        if memory_dir.is_symlink():
            return None
        existing = read_memory_file(memory_dir)
        return build_memory_block(memory_dir, scope, existing)
    existing = read_memory_file(memory_dir)
    return build_read_only_memory_block(memory_dir, scope, existing)
