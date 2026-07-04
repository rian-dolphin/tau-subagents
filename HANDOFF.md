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
| Tau capability stack | branches (each pushed, PR-shaped, stacked in order): `worktree-extensions` → `skills-seam` → `provider-usage` → `tool-progress` → `parent-context` → `ui-dialogs` → `message-renderers`; integration branch `subagents-integration` at the tip, checked out in the worktree at `~/.herdr/worktrees/tau/worktree-extensions` (worktrees are ephemeral — if gone, check out `subagents-integration` anywhere) |
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

Modules: `extension.py` (orchestration), `agents.py` (agent-type
definitions), `agents_menu.py` (interactive /agents menu), `settings.py`,
`prompts.py` (child prompt assembly + skills + parent-context digest),
`group_join.py`, `worktree.py`, `memory.py`, `output_file.py`, `cron.py`
(vendored 5-field matcher), `schedule.py` + `schedule_store.py` (pi's
scheduler), `notification_render.py` (pi-style notification cards).
73 tests.

Everything in the 2026-07-03 handoff's feature list still holds, plus, all
new this session (each feature-detects its Tau seam and degrades on plain
`worktree-extensions`):

- **Real billed token usage** — sums `input + output + cache_write` per
  assistant response (cache reads excluded, pi issue #38 semantics);
  surfaced as `<total_tokens>`, in the foreground completion line,
  `get_subagent_result`, `steer_subagent` state line, and run records;
  totals survive resume. Falls back to the chars/4 `context_tokens`
  estimate without the seam.
- **Live foreground progress** — the `agent` tool opts into `on_update`;
  one update per child tool start / completed turn with structured data.
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
- `message-renderers` (9af29e6) — `register_message_renderer` +
  `send_custom_message`; `custom_type`/`details` metadata on `UserMessage`
  (lighter than Pi's separate custom role — Ruling records why); renderers
  return Rich-markup strings, never widgets; consumed by all render paths
  incl. resume and print mode.

An adversarial review pass ran on every seam and extension batch this
session; all confirmed findings were fixed (dialog arrow-nav, cron
zero-interval hot loop, steer-line token parity). A review of
`message-renderers` (the broadest seam) was still in flight at handoff
time — check for unapplied verdicts before building on it.

## Remaining gaps (all small or blocked on new UI surfaces)

- **Real cost reporting** — `Usage.cost` is always None; Tau has no
  per-model pricing table (Pi's `models.ts` `calculateCost`). Port one if
  billed-dollar figures are wanted.
- **pi /agents surfaces not ported**: create-agent wizard, settings
  submenu (incl. the scheduling on/off toggle), and the full
  conversation-viewer overlay — the viewer needs Pi's `ctx.ui.custom`
  (arbitrary component overlay), a bigger Tau UI surface than dialogs.
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
