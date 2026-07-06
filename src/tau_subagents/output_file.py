"""JSONL output files for subagent transcripts, ported from pi-subagents.

Each run streams its child transcript to
`<tempdir>/tau-subagents-<uid>/<encoded-cwd>/<parent-session>/tasks/<id>.jsonl`
so the transcript can be inspected outside the parent conversation. The
initial entry is the prompt; each `turn_end` flushes new session messages.
Write errors are swallowed and all IO runs in a thread.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path


def encode_cwd(cwd: str) -> str:
    """Encode a working directory into a single, never-empty path component."""
    return cwd.replace("/", "-").replace("\\", "-").lstrip("-") or "root"


def output_file_path(cwd: Path, parent_session_id: str | None, agent_id: str) -> Path:
    """Return the transcript path for one run."""
    uid = os.getuid() if hasattr(os, "getuid") else 0
    return (
        Path(tempfile.gettempdir())
        / f"tau-subagents-{uid}"
        / encode_cwd(str(cwd))
        / (parent_session_id or "no-session")
        / "tasks"
        / f"{agent_id}.jsonl"
    )


class OutputFileWriter:
    """Appends transcript entries for one run to its JSONL output file."""

    def __init__(self, path: Path, agent_id: str, cwd: Path) -> None:
        self.path = path
        self._agent_id = agent_id
        self._cwd = cwd
        self._written = 1  # index 0 is the prompt, written by write_initial

    async def write_initial(self, prompt: str) -> None:
        await asyncio.to_thread(self._write_initial_blocking, prompt)

    async def flush(self, messages: Sequence[object]) -> None:
        await asyncio.to_thread(self._flush_blocking, list(messages))

    def _write_initial_blocking(self, prompt: str) -> None:
        try:
            root = self.path.parents[3]
            root.mkdir(parents=True, exist_ok=True)
            os.chmod(root, 0o700)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._append(
                self._entry("user", {"role": "user", "content": prompt})
            )
        except OSError:
            pass

    def _flush_blocking(self, messages: list[object]) -> None:
        try:
            new_messages = messages[self._written :]
            if not new_messages:
                return
            for message in new_messages:
                self._append(
                    self._entry(_entry_type(message), _serialize_message(message))
                )
            self._written = len(messages)
        except OSError:
            pass

    def _entry(self, entry_type: str, message: object) -> dict[str, object]:
        return {
            "isSidechain": True,
            "agentId": self._agent_id,
            "type": entry_type,
            "message": message,
            "timestamp": datetime.now(UTC).isoformat(),
            "cwd": str(self._cwd),
        }

    def _append(self, entry: dict[str, object]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


def _entry_type(message: object) -> str:
    role = getattr(message, "role", None)
    if role in ("assistant", "user"):
        return str(role)
    return "toolResult"


def _serialize_message(message: object) -> object:
    dump = getattr(message, "model_dump", None)
    if dump is not None:
        try:
            return dump(mode="json")
        except Exception:  # noqa: BLE001 - transcripts degrade to repr
            pass
    return str(message)
