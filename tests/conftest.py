"""Shared fixtures for the tau-subagents test suite."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_transcript_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Point durable transcripts at a per-test directory (ADR 0003).

    Without this, any test that spawns a run would write JSONL transcripts
    into the developer's real `~/.tau/subagents/` — and the retention sweep
    on session_start could delete real files there.
    """
    root = tmp_path / "subagents-transcripts"
    monkeypatch.setenv("TAU_SUBAGENTS_DIR", str(root))
    return root


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point HOME at a per-test directory.

    Children run with extensions enabled and discover them from
    `Path.home()/.tau/extensions`, so without this every child spawned in a
    test would load the developer's real installed extensions.
    """
    home = tmp_path / "isolated-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home
