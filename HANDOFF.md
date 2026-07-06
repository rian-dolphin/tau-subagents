# Handoff: fleshing out tau-subagents

**Mission:** evolve this extension toward feature parity with
[pi-subagents](https://github.com/tintinweb/pi-subagents) (the extension it
ports), within the capabilities of Tau's extension system — and extend Tau
itself where a capability is genuinely missing.

Written 2026-07-03 at the end of the session that built Tau's extension
system and this initial port. **Updated 2026-07-04 (third session):** the
entire remaining gap list is done. Every pi-subagents feature that needed a
Tau capability got one, as a stacked chain of PR-shaped branches on the tau
fork, and the extension consumes each seam behind feature detection (it
still loads and passes tests against plain `worktree-extensions`).

## Where everything is

| Thing | Location |
|---|---|
| This repo | `~/Documents/personal-projects/tau-subagents` · [rian-dolphin/tau-subagents](https://github.com/rian-dolphin/tau-subagents) (private) |
| Tau fork (main clone) | `~/Documents/personal-projects/tau` · [rian-dolphin/tau](https://github.com/rian-dolphin/tau) |
| Tau capability stack | branches (each pushed, PR-shaped, stacked in order): `worktree-extensions` → `skills-seam` → `provider-usage` → `tool-progress` → `parent-context` → `ui-dialogs` → `message-renderers` → `extension-manifest` → `session-start-ui-order`; integration branch `subagents-integration` at the tip, checked out in the worktree at `~/.herdr/worktrees/tau/worktree-extensions` (worktrees are ephemeral — if gone, check out `subagents-integration` anywhere). NOTE (2026-07-04): `subagents-integration` is the chain cherry-picked onto the current upstream/main tip (not a merge — integrate new seam branches by cherry-pick). It gets rebased locally (e.g. fixup autosquashes), so pushing it usually needs `git push --force-with-lease` — which sessions cannot run; ask Rian. A provider-usage fixup (openai_compatible.py usage) exists only squashed into this branch, not on the standalone `provider-usage` chain branch. |
| Tau upstream | [huggingface/tau](https://github.com/huggingface/tau) — roadmap is [issue #1](https://github.com/huggingface/tau/issues/1); Phase 21 (extensions) is what the fork implemented |
| Pi clone (read-only reference) | `reference/pi` in this repo (gitignored) · [earendil-works/pi](https://github.com/earendil-works/pi) |
| pi-subagents clone (read-only reference) | `reference/pi-subagents` in this repo (gitignored) · [tintinweb/pi-subagents](https://github.com/tintinweb/pi-subagents) |

If `reference/` is missing, restore it:

```bash
git clone --depth 50 https://github.com/earendil-works/pi.git reference/pi
git clone --depth 20 https://github.com/tintinweb/pi-subagents.git reference/pi-subagents
```

## Key reading, in order

1. `README.md` here — what the extension does (now covers every feature).
2. Tau's extension docs: `<tau>/docs/extensions.md` (user guide, includes the
   new `ctx.ui` dialogs and message renderers) and
   `<tau>/dev-notes/architecture/phase-21-extensions.md` (design doc — the
   **Ruling:** notes record deliberate decisions, including all deviations
   from Pi made this session).
3. Tau's extension internals: `<tau>/src/tau_coding/extensions/`
   (`api.py`, `loader.py`, `runtime.py`) and `<tau>/tests/test_extensions.py`.
4. The original: `reference/pi-subagents/src/index.ts` (entry, ~2300 lines),
   `src/agent-runner.ts`, `src/agent-manager.ts`, `src/custom-agents.ts`,
   `src/group-join.ts`, `src/schedule.ts` + `src/schedule-store.ts`,
   `src/context.ts`, `src/usage.ts`.
5. Pi's extension API for comparison: `reference/pi/packages/coding-agent/docs/extensions.md`
   and `src/core/extensions/{types,loader,runner}.ts`.

## Current state of this extension

The implementation is the `src/tau_subagents/` package (uv/src layout since
2026-07-04; the root `pyproject.toml` resolves Tau from the integration
worktree via a `[tool.uv.sources]` path — update that path if the worktree
moves). The entry point is declared through Tau's extension manifest
(`[tool.tau] extensions = ["src/tau_subagents/extension.py"]` in
`pyproject.toml` — the tau branch `extension-manifest`, a port of pi's
`package.json` `pi.extensions`), so `tau -x` works on the repo root or on
`src/tau_subagents` directly, with no root shim. On tau branches without the
manifest seam, point `-x` at `src/tau_subagents`.

Modules: `extension.py` (orchestration), `agents.py` (agent-type
definitions), `agents_menu.py` (interactive /agents menu), `settings.py`,
`prompts.py` (child prompt assembly + skills + parent-context digest),
`group_join.py`, `worktree.py`, `memory.py`, `output_file.py`, `cron.py`
(vendored 5-field matcher), `schedule.py` + `schedule_store.py` (pi's
scheduler), `notification_render.py` (pi-style notification cards).
81 tests.

Added 2026-07-06 (this session): `render_call` friendly tool lines on all
three tools; runs published as `TranscriptSource`s (agents strip, in-place
views, steer-by-typing — see the not-yet-extracted seams below); /agents
run selection jumps into the in-place view (modal → submenu fallbacks);
the per-event foreground progress relay (`on_update`/`_emit_progress`) was
REMOVED — the spinner+timer on the tool row is the live signal now, so the
`tool-progress` seam currently has no consumer in this extension.

Everything in the 2026-07-03 handoff's feature list still holds, plus, all
new this session (each feature-detects its Tau seam and degrades on plain
`worktree-extensions`):

- **Real billed token usage** — sums `input + output + cache_write` per
  assistant response (cache reads excluded, pi issue #38 semantics);
  surfaced as `<total_tokens>`, in the foreground completion line,
  `get_subagent_result`, `steer_subagent` state line, and run records;
  totals survive resume. Falls back to the chars/4 `context_tokens`
  estimate without the seam.
- ~~Live foreground progress~~ — REMOVED 2026-07-06: the `on_update`
  relay's per-event text lines were transcript noise once tau core grew
  the pending-tool spinner + elapsed timer and the in-place agent views.
- **`inherit_context`** (param or frontmatter, param wins) — pi's verbatim
  parent-context digest (`[User]:`/`[Assistant]:`, tool results dropped)
  prepended to the child prompt, captured at spawn time. Errors clearly
  without the seam. pi's `isolated` is deliberately not ported (children
  never get extension tools — nothing to strip).
- **Interactive `/agents` menu** — pi's showAgentsMenu navigation on
  select/confirm/input dialogs: runs (view result / steer / stop), agent
  types, scheduled jobs. Text-list fallback headless/without the seam.
- **Cron scheduling** — `schedule` param (5-field cron, intervals ≥5s,
  one-shots), session-scoped PID-locked store under
  `<cwd>/.tau/subagent-schedules/`, queue-bypassing background spawns,
  skip-on-misfire. Naive local time (DST divergence documented).
- **Notification rendering** — completions render as pi-style cards via a
  registered `subagent-notification` renderer + `send_custom_message`
  (raw XML still enters model context); groups stack cards.

## The Tau capability stack (all pushed to the fork)

Each branch is one PR-shaped commit (plus fixes), designed from Pi first,
gate green at every tip, purity boundary (`tau_agent` ⊬ `tau_coding`) held:

- `skills-seam` (4aee134) — `CodingSessionConfig.skills_enabled`.
- `provider-usage` (d00b600) — Pi's `Usage` on `AssistantMessage`, populated
  by all three provider adapters (with `supports_usage_in_streaming` compat
  gate); cost always None (no pricing table — the one remaining sub-gap).
- `tool-progress` (605c2d3 + 124a76f) — `on_update` executor seam
  (inspect-gated opt-in) bridged to `ToolExecutionUpdateEvent` via an
  asyncio queue/task race; **loop-thread-only contract** (documented; do
  not "fix" with call_soon_threadsafe — it would defer puts past the
  end-of-run drain and drop updates).
- `parent-context` (07baa7e) — `ExtensionContext.transcript` (deep copies).
- `ui-dialogs` (a6b61e5 + baa61f2) — async `select`/`confirm`/`input` on
  `UiBridge`/`ctx.ui`, Textual modals. The fix commit matters: TauTuiApp's
  app-priority completion bindings route arrow keys through an isinstance
  allowlist in `action_completion_next/previous/accept_completion` — **any
  future picker-style screen must be added to those allowlists** or arrows
  silently don't work (tests must use real `pilot.press`, not
  `action_cursor_down()`, which masks the bug).
- `extension-manifest` (d7faaa3) — `[tool.tau] extensions = [...]` in an
  extension directory's `pyproject.toml` declares entry files (port of pi's
  `package.json` `pi.extensions`); manifest wins over sibling `extension.py`,
  entries load as packages rooted at their parent dir. Lets src-layout
  extension repos (like this one) load from the repo root without a shim.
- `session-start-ui-order` (672e093) — `session_start` is deferred out of
  `CodingSession.load`; hosts release it via the idempotent
  `session.emit_pending_session_start()` after `set_ui_bridge()` (TUI
  `on_mount`, print mode after `StderrUiBridge`). Pi's ordering — UI first —
  so `session_start` handlers can notify/open dialogs. Any session fake used
  with the TUI must grow an async `emit_pending_session_start()`.
- `message-renderers` (9af29e6 + 19cf9fc) — `register_message_renderer` +
  `send_custom_message`; `custom_type`/`details` metadata on `UserMessage`
  (lighter than Pi's separate custom role — Ruling records why); renderers
  return Rich-markup strings, never widgets; consumed by all render paths
  incl. resume and print mode.

NOT yet extracted to stack branches (2026-07-06, live only on
`subagents-integration` — extract before upstreaming):

- **tool-call renderers** (aa96ed8) — `AgentTool.render_call` (pi's
  `renderCall`): friendly one-line invocations. Resolution is LAZY at render
  time via `runtime.render_tool_call` reading the unwrapped registry —
  session messages load before the runtime connects, so eager resolution
  misses restored calls.
- **show_transcript modal** (aa96ed8) — `ui.show_transcript(title, messages,
  poll=)`: modal TranscriptScreen. Now the /agents FALLBACK path only
  (superseded by the strip); candidate for removal once the strip proves out.
- **agents strip / transcript sources** (0fbba26, 2c9303c) —
  `TranscriptSource` + `set_transcript_source_provider` +
  `notify_transcript_sources_changed` + `ui.view_transcript(id)`. Strip under
  the prompt; in-place agent views on a second display-toggled
  TranscriptView; input steers the viewed agent; Esc precedence strip → view
  → cancel; push-for-membership, 0.5s revision-gated poll for the open view;
  finished agents leave the strip; rows clickable.
- **pending-tool spinner + timer** (bd89ad6, a7e936b, this session) — braille
  spinner replaces the `→`/`▸` marker on the executing tool row plus a live
  elapsed `(1m 23s)` after 1s, driven by the existing activity timer; rows
  update IN PLACE (`refresh_invocation`) — remounting per tick caused visible
  transcript flicker (regression-tested via widget identity).

An adversarial review pass ran on every seam and extension batch this
session; all confirmed findings were fixed (dialog arrow-nav, cron
zero-interval hot loop, steer-line token parity, and the renderers
forward-compat break — 19cf9fc makes unset custom metadata absent on the
wire so plain new sessions stay old-binary-readable; downgrade with custom
messages present is explicitly unsupported per the corrected Ruling).

## Remaining gaps (all small or blocked on new UI surfaces)

- **Real cost reporting** — `Usage.cost` is always None; Tau has no
  per-model pricing table (Pi's `models.ts` `calculateCost`). Port one if
  billed-dollar figures are wanted.
- **pi /agents surfaces not ported**: create-agent wizard and settings
  submenu (incl. the scheduling on/off toggle). The conversation viewer IS
  ported and superseded: runs open as in-place agent views (agents strip /
  `view_transcript`), with the modal `show_transcript` as fallback.
- **Agent-view v2 ideas**: typing at a *finished* agent could resume it
  (the resume plumbing exists); the modal `show_transcript` seam can be
  removed once the strip has proven out.
- **Pi renderer sub-features**: `display:false` (hide-but-keep-in-context)
  and `registerEntryRenderer`/`appendEntry` cards — deliberately out of
  scope of the message-renderers seam v1.
- Minor documented divergences to revisit if they ever bite: scheduler
  naive-local DST behavior; parent-context deep-copies-before-filter cost
  on huge transcripts; dialog timeout under a covered screen leaves a
  stale (result-discarded) modal visible.
- **Upstreaming**: the stack is PR-shaped for huggingface/tau once Phase 21
  lands; keep new work stacked the same way.

## Working agreement with the tau repo

Unchanged from the previous handoff, and it worked well this session:

- Fix Tau bugs / add missing capabilities **in the tau repo** on branches
  stacked on the chain above (now: stack on `message-renderers`), with
  tests, keeping the gate green — don't work around them here.
- **Pi is the blueprint.** Read Pi's implementation before designing any
  Tau capability; port names/semantics where they translate; record
  deviations as **Ruling:** notes in the phase-21 design doc.
- Tau's verification gate: `uv run pytest && uv run ruff check . && uv run mypy .`
  — pre-existing failures only: 2 ruff (`tests/test_coding_session.py`,
  `tests/test_tui_app.py`) and 4 documented mypy errors in `src/`
  (`tui/widgets.py`/`tui/app.py`; the repo-wide mypy total also counts
  untyped tests — compare against the tip's own baseline, it has drifted
  down as branches fixed fixture typing).
- Test-fake note: the message-renderers seam widened
  `BoundSession.queue_follow_up_message`/`queue_steering_message` with
  keyword `custom_type`/`details` — any new session fake must accept them.
- The extension's provider factories are module globals monkeypatched in
  tests via the loaded synthetic module (`tau_extension_tau_subagents_*`
  in `sys.modules`) — see `tests/test_extension.py::_patch_fake_provider`.

## Tests

```bash
cd ~/Documents/personal-projects/tau-subagents
uv run pytest          # repo's own env; Tau comes from the pyproject path source
# or, borrowing Tau's env (still supported):
uv run --project ~/.herdr/worktrees/tau/worktree-extensions pytest tests/
```

That worktree now holds `subagents-integration` (the full seam stack), so
all 73 run; against plain `worktree-extensions` the seam-dependent tests
skip and the rest must still pass — that compatibility is a hard
requirement.

## Live smoke test

```bash
# Interactive (integration worktree = full stack):
cd ~/.herdr/worktrees/tau/worktree-extensions
uv run tau -x ~/Documents/personal-projects/tau-subagents
# "Spawn an explore subagent to summarize this repo's architecture."
# /agents — interactive menu; watch live progress on a foreground spawn;
# background completion should render as a card, not raw XML.

# Headless:
cd ~/Documents/personal-projects/tau-subagents
uv run --project ~/.herdr/worktrees/tau/worktree-extensions tau -x . \
  -p "Use the agent tool to spawn an 'explore' subagent to summarize this repo."
```

The default provider (`openai-codex`, stored OAuth) works for real runs.
