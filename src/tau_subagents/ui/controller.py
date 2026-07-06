"""Wires the extension's Textual widgets onto tau's component seam.

Holds the fleet strip and the (at most one) open conversation viewer, registers
the strip slot widget + the key interceptor that ENTERS the strip, and repoints
the manager's change signal at a push that refreshes the strip and any open
viewer. All host access goes through the :class:`ComponentBridge`
(``context.ui.components``); nothing here touches tau internals directly beyond
the widgets it mounts.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Callable

from .strip import AgentStripWidget
from .viewer import ConversationViewer

if TYPE_CHECKING:
    from textual import events
    from tau_coding.extensions import ComponentBridge
    from tau_coding.tui.config import TuiTheme

    from ..extension import AgentRun, SubagentManager

STRIP_KEY = "subagents-fleet"


class SubagentUiController:
    """Owns the fleet strip + conversation viewer against the component bridge."""

    def __init__(self, manager: SubagentManager, components: ComponentBridge) -> None:
        self._manager = manager
        self._components = components
        self._strip: AgentStripWidget | None = None
        self._viewer: ConversationViewer | None = None
        self._viewing_id: str | None = None
        self._unsub_interceptor: Callable[[], None] | None = None

    # ---- Install / teardown ----------------------------------------------

    def install(self) -> None:
        """Mount the strip slot and register the strip-entry key interceptor."""
        self._components.set_slot_widget(
            STRIP_KEY, self._build_strip, placement="below_prompt"
        )
        self._unsub_interceptor = self._components.register_key_interceptor(
            self._intercept_key
        )

    def teardown(self) -> None:
        """Remove the strip, close any viewer, and drop the interceptor."""
        if self._unsub_interceptor is not None:
            with contextlib.suppress(Exception):
                self._unsub_interceptor()
            self._unsub_interceptor = None
        with contextlib.suppress(Exception):
            self._components.set_slot_widget(STRIP_KEY, None, placement="below_prompt")
        self._strip = None
        self._viewer = None
        self._viewing_id = None

    def _build_strip(self, theme: TuiTheme) -> AgentStripWidget:
        strip = AgentStripWidget(
            self._manager, theme, open_conversation=self.open_conversation
        )
        strip.viewing_id = self._viewing_id
        self._strip = strip
        return strip

    # ---- Push -------------------------------------------------------------

    def on_change(self) -> None:
        """Manager change signal: refresh the strip and any open viewer."""
        if self._strip is not None:
            self._strip.refresh_roster()
        if self._viewer is not None:
            self._viewer.on_external_change()

    # ---- Viewer -----------------------------------------------------------

    def open_conversation(self, run: AgentRun) -> bool:
        """Open the run's conversation in the main-area view. False if unsupported."""
        if not self._components.supports_components:
            return False
        self._viewing_id = run.agent_id
        if self._strip is not None:
            self._strip.viewing_id = run.agent_id
            self._strip.refresh_roster()

        def build(handle, theme: TuiTheme) -> ConversationViewer:
            viewer = ConversationViewer(
                run,
                handle,
                self._manager,
                theme,
            )
            # Identity-checked close: a superseded viewer's (deferred) unmount
            # must not clobber a newer viewer opened in its place.
            viewer.on_close = lambda: self._on_viewer_closed(viewer)
            self._viewer = viewer
            return viewer

        self._components.open_main_view(build)
        return True

    def _on_viewer_closed(self, viewer: ConversationViewer) -> None:
        if self._viewer is not viewer:
            return
        self._viewer = None
        self._viewing_id = None
        if self._strip is not None:
            self._strip.viewing_id = None
            self._strip.refresh_roster()

    # ---- Key interceptor (enter the strip only) ---------------------------

    def _intercept_key(self, event: events.Key, prompt_text: str) -> bool:
        """Enter the strip on left/down at an empty prompt (pi's activation gate).

        The interceptor fires only while the prompt is focused, so it is used
        solely to hand focus INTO the strip; once focused the strip owns its own
        navigation via its ``on_key``.
        """
        strip = self._strip
        if strip is None or prompt_text != "":
            return False
        if event.key in ("down", "left") and strip.has_agents():
            strip.enter_strip()
            return True
        return False
