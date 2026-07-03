"""Child system prompt assembly for subagents, ported from pi-subagents.

Two modes, selected by agent frontmatter `prompt_mode`:

- `replace` (default): the agent body becomes the child's base prompt
  (`custom_system_prompt`), with any preloaded skill sections appended.
- `append`: the parent session's full system prompt is kept verbatim as a
  byte-identical prefix, followed by pi's sub-agent bridge, an
  `<active_agent/>` tag, an environment block, the agent body wrapped in
  `<agent_instructions>`, and skill sections. The result is a full system
  prompt override.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from tau_coding import TauResourcePaths
from tau_coding.skills import Skill, format_skill_invocation, load_skills

if TYPE_CHECKING:
    from .agents import AgentDefinition

GIT_TIMEOUT_SECONDS = 5

SUB_AGENT_BRIDGE = """<sub_agent_context>
You are operating as a sub-agent invoked to handle a specific task.
- Use the read tool instead of cat/head/tail
- Use the edit tool instead of sed/awk
- Use the write tool instead of echo/heredoc
- Use the find tool instead of bash find/ls for file search
- Use the grep tool instead of bash grep/rg for content search
- Make independent tool calls in parallel
- Use absolute file paths
- Do not use emojis
- Be concise but complete
</sub_agent_context>"""


def load_available_skills(cwd: Path, home: Path | None = None) -> list[Skill]:
    """Load skills visible from cwd (project `.tau/skills` overrides user)."""
    home_dir = home if home is not None else Path.home()
    return load_skills(TauResourcePaths(root=home_dir / ".tau", cwd=cwd))


def resolve_skill_blocks(
    names: tuple[str, ...] | None, cwd: Path, home: Path | None = None
) -> list[tuple[str, str]]:
    """Resolve named skills to (name, block) pairs; missing → placeholder."""
    if not names:
        return []
    try:
        available = {skill.name: skill for skill in load_available_skills(cwd, home)}
    except Exception:  # noqa: BLE001 - unreadable skill dirs degrade to placeholders
        available = {}
    blocks: list[tuple[str, str]] = []
    for name in names:
        skill = available.get(name)
        if skill is None:
            blocks.append((name, f'(Skill "{name}" not found)'))
        else:
            blocks.append((name, format_skill_invocation(skill)))
    return blocks


def build_child_system_prompt(
    definition: AgentDefinition,
    *,
    parent_prompt: str | None,
    environment: str,
    skill_blocks: list[tuple[str, str]],
    memory_block: str | None = None,
) -> str | None:
    """Assemble the child system prompt for one agent definition.

    Append mode (with a parent prompt available) returns a full system prompt
    override incorporating `environment` (from `detect_environment`).
    Otherwise returns a base prompt for `custom_system_prompt`, or None to
    keep the default coding prompt. Extras follow pi's order: memory block
    first, then skill sections.
    """
    if definition.prompt_mode == "append" and parent_prompt:
        custom_section = ""
        if definition.system_prompt:
            custom_section = (
                "\n\n<agent_instructions>\n"
                f"{definition.system_prompt}\n"
                "</agent_instructions>"
            )
        extras: list[str] = []
        if memory_block:
            extras.append(memory_block)
        extras.extend(
            f"\n# Preloaded Skill: {name}\n{block}" for name, block in skill_blocks
        )
        extras_suffix = "\n\n" + "\n".join(extras) if extras else ""
        return (
            f"{parent_prompt}\n\n{SUB_AGENT_BRIDGE}\n\n"
            f'<active_agent name="{definition.name}"/>\n\n'
            f"{environment}{custom_section}{extras_suffix}"
        )
    parts: list[str] = []
    if definition.system_prompt:
        parts.append(definition.system_prompt)
    if memory_block:
        parts.append(memory_block)
    parts.extend(f"# Preloaded Skill: {name}\n{block}" for name, block in skill_blocks)
    return "\n\n".join(parts) if parts else None


async def detect_environment(cwd: Path) -> str:
    """Build the append-mode environment block for cwd without blocking the loop."""
    return await asyncio.to_thread(_detect_environment_blocking, cwd)


def _detect_environment_blocking(cwd: Path) -> str:
    return (
        "# Environment\n"
        f"Working directory: {cwd}\n"
        f"{_git_status_line(cwd)}\n"
        f"Platform: {sys.platform}"
    )


def _git_status_line(cwd: Path) -> str:
    try:
        inside = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return "Not a git repository"
        branch = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return "Not a git repository"
    name = branch.stdout.strip() if branch.returncode == 0 else ""
    if name:
        return f"Git repository: yes\nBranch: {name}"
    return "Git repository: yes"
