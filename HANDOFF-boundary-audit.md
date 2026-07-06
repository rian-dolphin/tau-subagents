# Handoff: audit the tau ↔ tau-subagents boundary

> **RESOLVED (2026-07-06).** The audit ran, a fresh-context reviewer
> pressure-tested it, and the changes were applied. Verdict: no logic leaks
> — no `tau_subagents` imports in core, no tool-name special-casing, status
> handling stays inside the seam enum with safe fallbacks. The agent-first
> naming in core (AgentStrip etc.) was kept as product language (option 2);
> the pi-style component seam (option 3) was rejected — it would reopen the
> strings-not-widgets Ruling. One accepted coupling to record: core's strip
> hint text hardcodes `/agents` and the word "agents" (`_render_agent_strip`)
> — presentation-level coupling to an extension-owned command name.
> Applied: removed the `show_transcript` modal seam + `TranscriptScreen`
> from tau (5ae8dec) and the extension's fallback path; /agents now degrades
> straight to the action submenu on hosts without the strip seam. Known gap
> logged in HANDOFF.md: Steer/Stop submenu is unreachable for running agents
> on strip-capable hosts (pre-existing, not a removal regression).

## The question you are being asked to answer

Recent sessions built subagent UX (friendly tool lines, an agents strip with
in-place conversation views, spinners/timers) and most of the code landed in
**tau core**, not in the tau-subagents extension. Rian's question: **has
subagent logic leaked into tau that shouldn't live there? What should move,
be renamed, or be re-seamed — and what is legitimately core?**

Deliverable: a concrete verdict per item (keep / rename / move / re-seam),
then apply the agreed changes. Use a fresh-context reviewer subagent to
pressure-test your plan before implementing (established workflow here).

## Repos and branches

| What | Where |
|---|---|
| This extension | `~/Documents/personal-projects/tau-subagents` (git: `rian-dolphin/tau-subagents`, branch `main`) |
| Tau fork worktree (ALL core edits here) | `~/.herdr/worktrees/tau/worktree-extensions`, branch `subagents-integration` |
| Tau main clone | `~/Documents/personal-projects/tau` |
| pi reference (the design blueprint) | `reference/pi` in this repo (read-only clone) |
| pi-subagents reference | `reference/pi-subagents` in this repo |
| General project handoff | `HANDOFF.md` in this repo (branch stack, conventions, force-push needs Rian) |

Tests: `uv run pytest` in each repo (tau ~794, tau-subagents ~81, both green
at handoff). Tau lint: `uv run ruff check src tests`.

## The intended design principle

Tau core must stay **subagent-agnostic**: extensions publish through generic
seams; the word "subagent" should not appear in core logic. The extension
(this repo) owns subagent semantics. Blueprint is pi — note the key
divergence: **pi keeps ALL of this UX in the extension** because pi has
`ctx.ui.custom` (extensions ship arbitrary UI components). Tau deliberately
rejected that: tau's renderer seams return *strings, never widgets* (see the
message-renderers Ruling in tau's dev-notes), so overlay/strip UX had to be
built host-side against generic data seams. That choice is the root of this
audit's question.

## Recent tau-core commits to audit (git log on `subagents-integration`)

- `6341404` — in-place tool progress updates (ChatItem.update_text).
- `aa96ed8` — `AgentTool.render_call` seam (pi's renderCall) + modal
  `ui.show_transcript` (TranscriptScreen).
- `0fbba26` — transcript-sources seam (`TranscriptSource`,
  `set_transcript_source_provider`, `notify_transcript_sources_changed`,
  `ui.view_transcript`) + the agents strip + in-place agent views +
  steer-by-typing.
- `2c9303c` — strip drops finished agents; clickable rows (AgentStrip).
- `bd89ad6` — braille spinner on the executing tool row.
- `a7e936b` — flicker fix: tool rows re-render in place (refresh_invocation).
- `c10a3de` — elapsed timer on executing tool row; one filled strip dot.

Corresponding extension commits (this repo): `915876d`, `0ebcbd9`,
`d0d5ac8`, `f43c63b`.

## Pre-assessment (verify, don't trust)

**Looks legitimately core (generic mechanism, no subagent knowledge):**

- `AgentTool.render_call` + `runtime.render_tool_call` (any tool can have a
  friendly line; lazy render-time resolution).
- Pending-tool spinner + elapsed timer (`tui/state.py`: `tool_spinner`,
  `apply_tool_spinner`, `ChatItem.started_at`) — benefits every tool.
- `TranscriptSource` dataclass + provider aggregation + changed-callback
  (`extensions/api.py`, `runtime.py`) — vocabulary is generic
  (id/label/status/messages/revision/steer).
- `ChatItem.tool_name/tool_arguments`, in-place row updates.

**Gray zone — generic mechanism, agent-flavored vocabulary/UX in core
(`tui/app.py`, grep these):**

- `AgentStrip` widget, `#agent-strip`, `#agent-transcript-pane`,
  `AGENT_STRIP_MAX_ROWS`, `AGENT_STRIP_STATUS_GLYPHS`, `_agent_view_*` /
  `_agent_strip_*` state, `_strip_*` methods, the `← agents` hint text.
- `_steer_viewed_agent`, the prompt placeholder `"Steer {label}…  Esc
  returns to main"`, `"{label} finished · Esc returns to main"` — steer
  semantics expressed generically (`source.steer`) but worded for agents.
- `TranscriptScreen` (modal) docstring names pi-subagents; the extensions
  guide section is literally titled "Transcript sources (the agents strip)"
  (`website/content/guides/extensions.md`).
- Esc-precedence and input-routing policy (first Esc = leave view, typing
  targets the viewed source, completions suppressed in a view) — host
  policy, but designed specifically for the subagent experience.

**Options to weigh for the gray zone (not prejudged):**

1. **Rename to neutral vocabulary** (e.g. "source strip" / "session views"),
   keeping the architecture — cheapest, makes core honestly generic for
   upstreaming (huggingface/tau Phase 21).
2. **Accept agent-first naming as product language** — Claude Code names the
   concept "agents" in its host UI too; if upstream tau wants first-class
   agent UX, the naming is a feature, not a leak.
3. **Re-seam toward pi**: add a `ctx.ui.custom`-style component seam so the
   extension owns strip/view UX. This contradicts tau's strings-not-widgets
   Ruling — reopening that Ruling is a big decision; flag it, don't assume.

**Things to actively check for real logic leaks (not just naming):**

- Does core special-case any tool NAME (e.g. `"agent"`) anywhere? (It
  shouldn't; the `skill` special-case in `TuiState.add_tool_call` predates
  this work.)
- Does core assume source statuses beyond the seam enum
  (`queued|running|done|error|cancelled`)? The extension maps its richer
  statuses (`steered`, `aborted`) down at the boundary
  (`extension.py: SOURCE_STATUS`) — confirm nothing upstream of that leaks.
- Does core import or reference tau_subagents anywhere? (Must be no.)
- Is the modal `show_transcript` seam still pulling its weight? It is now
  only the /agents fallback for hosts with dialogs but no strip — HANDOFF.md
  already marks it a removal candidate.

## Constraints

- Keep both suites green; the pilot tests in
  `tau/tests/test_tui_app.py` (`agent_strip*`, `agent_view*`, spinner,
  escape-precedence tests) pin the UX — renames must update them.
- The extension must keep loading against older tau branches (getattr /
  ImportError guards around every new seam — see `extension.py` setup).
- `subagents-integration` is a cherry-pick stack; new work should stay
  PR-shaped for later extraction (see HANDOFF.md "capability stack" — the
  four capabilities from these sessions are NOT yet extracted to stack
  branches). Pushing that branch needs Rian (`--force-with-lease`).
- Commit messages in both repos narrate the reasoning — read them
  (`git log`) before re-deriving intent.
