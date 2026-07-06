"""Extension-owned Textual widgets for the subagents fleet UI.

This package is the extension side of tau's *component seam* (branch
``component-seam-experiment``): the agents strip and the conversation viewer
that used to live in tau core are now real Textual ``Widget``s that the
extension mounts through ``context.ui.components``. Importing ``textual`` (and,
in the viewer, a couple of tau TUI internals) directly is the deliberate
coupling this experiment measures.
"""

from __future__ import annotations

from .controller import SubagentUiController
from .strip import AgentStripWidget
from .viewer import ConversationViewer

__all__ = ["AgentStripWidget", "ConversationViewer", "SubagentUiController"]
