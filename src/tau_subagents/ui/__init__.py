"""Extension-owned Textual widgets for the subagents fleet UI.

This package is the extension side of tau's *component seam*: the agents strip
and the conversation viewer are real Textual ``Widget``s that the extension
mounts through ``context.ui.components``. It imports ``textual`` (and, in the
viewer, a couple of tau TUI internals) directly — a deliberate coupling to the
host's rendering, called out where it happens.
"""

from __future__ import annotations

from .controller import SubagentUiController
from .strip import AgentStripWidget
from .viewer import ConversationViewer

__all__ = ["AgentStripWidget", "ConversationViewer", "SubagentUiController"]
