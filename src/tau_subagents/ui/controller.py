"""Wires the extension's Textual widgets onto tau's component seam.

Holds the fleet strip and the (at most one) open conversation viewer, registers
the strip slot widget + the pre-dispatch key interceptor that owns the strip's
whole navigation state machine (pi's fleet-list model; the strip never takes
focus), and repoints the manager's change signal at a push that refreshes the
strip and any open viewer. All host access goes through the :class:`ComponentBridge`
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
        """Mount the strip slot and register the nav key interceptor."""
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
            # Nav state resets when a viewer opens: the interceptor yields while a
            # viewer is up, so leaving nav active would strand a highlight.
            self._strip.deactivate_nav()
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

    # ---- Key interceptor (owns the whole nav state machine) ---------------

    def _intercept_key(self, event: events.Key, prompt_text: str) -> bool:
        """Drive strip navigation, pi's fleet-list model, from the prompt.

        The host consults this pre-dispatch, before its app-level priority
        bindings and the focused widget, so the strip never needs focus. Gated
        on: a mounted strip with agents, no viewer open (a viewer/composer owns
        real focus and its own keys), and an empty prompt (typing flows through).

        Keys: while inactive, ``down``/``left`` activate nav (highlight ``main``).
        While active, ``down``/``up`` move the selection (up past the top
        deactivates), ``enter`` activates the row (``main`` → deactivate; an agent
        → open its viewer), ``escape`` deactivates. Any other key deactivates and
        is NOT consumed, so it flows to the prompt naturally (pi parity).
        """
        strip = self._strip
        # Yield entirely while a viewer/composer is open — it owns focus + keys.
        if strip is None or self._viewer is not None or not strip.has_agents():
            return False
        if prompt_text != "":
            # The user is typing; make sure a stale nav highlight is cleared.
            if strip.nav_active:
                strip.deactivate_nav()
            return False

        key = event.key
        if not strip.nav_active:
            if key in ("down", "left"):
                strip.activate_nav()
                return True
            return False

        # Nav is active.
        if key == "down":
            strip.move_selection(1)
            return True
        if key == "up":
            if strip.selected_index == 0:
                strip.deactivate_nav()
            else:
                strip.move_selection(-1)
            return True
        if key == "escape":
            strip.deactivate_nav()
            return True
        if key == "enter":
            run = strip.selected_run()
            if run is None:
                strip.deactivate_nav()  # main row: just return to the prompt
            else:
                self.open_conversation(run)
            return True

        # Any other key: cancel nav and let it flow to the prompt (pi parity).
        strip.deactivate_nav()
        return False
