"""Settings for the subagents extension, ported from pi-subagents.

Settings are read from two JSON files and shallow-merged, project overriding
user:

    ~/.tau/subagents.json          user defaults
    <cwd>/.tau/subagents.json      project overrides

Keys mirror pi's camelCase (`maxConcurrent`, `defaultMaxTurns`, `graceTurns`,
`defaultJoinMode`). Missing files are treated as empty; malformed JSON is
ignored rather than crashing the session. Out-of-range or wrong-typed values
are silently dropped and the field keeps its default.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

JOIN_MODES = ("async", "group", "smart")


@dataclass(frozen=True, slots=True)
class SubagentSettings:
    """Effective subagent settings for one Tau session."""

    max_concurrent: int = 4
    default_max_turns: int | None = None
    grace_turns: int = 5
    default_join_mode: str = "smart"


def load_subagent_settings(cwd: Path, home: Path | None = None) -> SubagentSettings:
    """Load and merge user then project settings into a SubagentSettings."""
    home_dir = home if home is not None else Path.home()
    merged: dict[str, object] = {}
    for path in (
        home_dir / ".tau" / "subagents.json",
        cwd / ".tau" / "subagents.json",
    ):
        merged.update(_read_file(path))
    return _from_dict(merged)


def _read_file(path: Path) -> dict[str, object]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _from_dict(data: dict[str, object]) -> SubagentSettings:
    settings = SubagentSettings()
    max_concurrent = _int_in_range(data.get("maxConcurrent"), 1, 1024)
    if max_concurrent is not None:
        settings = replace(settings, max_concurrent=max_concurrent)
    default_max_turns = _int_in_range(data.get("defaultMaxTurns"), 0, 10000)
    if default_max_turns is not None:
        settings = replace(
            settings,
            default_max_turns=None if default_max_turns == 0 else default_max_turns,
        )
    grace_turns = _int_in_range(data.get("graceTurns"), 1, 1000)
    if grace_turns is not None:
        settings = replace(settings, grace_turns=grace_turns)
    join_mode = data.get("defaultJoinMode")
    if isinstance(join_mode, str) and join_mode in JOIN_MODES:
        settings = replace(settings, default_join_mode=join_mode)
    return settings


def _int_in_range(value: object, low: int, high: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < low or value > high:
        return None
    return value
