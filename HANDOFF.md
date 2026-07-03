# Handoff: fleshing out tau-subagents

**Mission:** evolve this extension toward feature parity with
[pi-subagents](https://github.com/tintinweb/pi-subagents) (the extension it
ports), within the capabilities of Tau's extension system — and extend Tau
itself where a capability is genuinely missing.

Written 2026-07-03 at the end of the session that built Tau's extension
system and this initial port. **Updated 2026-07-03 (second session):** all
"buildable now" features below have been implemented and committed
(4d866c6, 14ac035, b1b6f90); the remaining work is the Tau-capability-blocked
list. Implementation-ready notes from that session (a full pi-subagents
semantics spec extracted from source, and a Tau extension API reference) may
still exist in the session scratchpad under `notes/`, but the code + README
are now the source of truth.

## Where everything is

| Thing | Location |
|---|---|
| This repo | `~/Documents/personal-projects/tau-subagents` · [rian-dolphin/tau-subagents](https://github.com/rian-dolphin/tau-subagents) (private) |
| Tau fork (main clone) | `~/Documents/personal-projects/tau` · [rian-dolphin/tau](https://github.com/rian-dolphin/tau) |
| Tau extension-system branch | branch `worktree-extensions`; worktree at `~/.herdr/worktrees/tau/worktree-extensions` (worktrees are ephemeral — if gone, check out the branch in the main clone) |
| Tau upstream | [huggingface/tau](https://github.com/huggingface/tau) — roadmap is [issue #1](https://github.com/huggingface/tau/issues/1); Phase 21 (extensions) is what the fork implemented |
| Pi clone (read-only reference) | `reference/pi` in this repo (gitignored) · [earendil-works/pi](https://github.com/earendil-works/pi) |
| pi-subagents clone (read-only reference) | `reference/pi-subagents` in this repo (gitignored) · [tintinweb/pi-subagents](https://github.com/tintinweb/pi-subagents) |

If `reference/` is missing, restore it:

```bash
git clone --depth 50 https://github.com/earendil-works/pi.git reference/pi
git clone --depth 20 https://github.com/tintinweb/pi-subagents.git reference/pi-subagents
```

## Key reading, in order

1. `README.md` here — what the extension currently does.
2. Tau's extension docs: `<tau>/docs/extensions.md` (user guide) and
   `<tau>/dev-notes/architecture/phase-21-extensions.md` (design doc — the
   **Ruling:** notes record deliberate v1 decisions; don't re-litigate them
   casually, but they are the map of what Tau can't do yet).
3. Tau's extension internals: `<tau>/src/tau_coding/extensions/`
   (`api.py`, `loader.py`, `runtime.py`) and `<tau>/tests/test_extensions.py`
   (shows every hook exercised).
4. The original: `reference/pi-subagents/src/index.ts` (entry, ~2300 lines),
   `src/agent-runner.ts` (spawning), `src/agent-manager.ts` (concurrency),
   `src/custom-agents.ts` (frontmatter definitions), `src/group-join.ts`
   (notification batching).
5. Pi's extension API for comparison: `reference/pi/packages/coding-agent/docs/extensions.md`
   and `src/core/extensions/{types,loader,runner}.ts`.

## Current state of this extension

Modules: `extension.py` (orchestration: run lifecycle, queue, notifications),
`agents.py` (agent-type definitions), `settings.py`, `prompts.py` (child
prompt assembly + skills), `group_join.py` (notification batching),
`worktree.py`, `memory.py`, `output_file.py`. 36 tests.

- `agent` tool — spawns a scoped in-process `CodingSession` (in-memory
  transcript, `extensions_enabled=False` so no recursive spawning). Params:
  `prompt`, `description`, `subagent_type`, `run_in_background`,
  `max_turns`, `resume`, `isolation`.
- `get_subagent_result` tool — poll (queued runs report queued), or
  `wait=true` to block through queued→running→done; claiming a result
  suppresses the redundant background notification.
- `steer_subagent` tool — injects a user message into a running child;
  steers sent before the child session exists are queued and flushed.
- `/agents` command — plain-text list of types and runs.
- Agent types from `~/.tau/agents/*.md` and `<cwd>/.tau/agents/*.md`
  (frontmatter: `description`, `tools`, `model`, `max_turns`, `skills`,
  `prompt_mode`, `memory`, `isolation`; body = system prompt).
  Built-ins: `general`, `explore`.
- Settings: `~/.tau/subagents.json` merged with `<cwd>/.tau/subagents.json`
  (project wins): `maxConcurrent` (default 4), `defaultMaxTurns`,
  `graceTurns` (5), `defaultJoinMode` (`smart`).
- Concurrency: background runs beyond `maxConcurrent` queue FIFO and drain
  as slots free; foreground runs bypass.
- `max_turns` + grace: at the limit the child is steered to wrap up
  (status `steered` if it does); `graceTurns` later it is hard-cancelled
  (status `aborted`). Precedence: frontmatter ?? param ?? settings.
- `resume`: finished runs keep sessions open for follow-up prompts (always
  foreground; not for worktree runs). Stale consumed runs evicted after
  10 min (session closed, record kept — deviation from pi, which deletes).
- Join modes: same-100ms-window background spawns form a group; `smart`
  delivers one consolidated notification (partial after 30s, stragglers on
  a 15s cadence), `group` waits for all, `async` stays individual.
- Worktree isolation: child runs in a detached `git worktree`; dirty
  changes are committed to a `tau-agent-<id>` branch, surfaced in results
  (including error terminals). Strict failure if cwd isn't a committed repo.
- Output files: child transcripts stream as JSONL under
  `<tmpdir>/tau-subagents-<uid>/…/tasks/<id>.jsonl`; path surfaced in spawn
  results, notifications, and `get_subagent_result`.
- Per-agent memory: `memory: user|project|local` injects a MEMORY.md block
  (read-write if the agent has write/edit, else read-only).
- Skill preloading (`skills:` CSV) + `prompt_mode: append` (parent prompt +
  pi's verbatim bridge + env block + agent body; cache-friendly byte prefix).
  `skills: true` full inheritance is NOT yet supported.
- Run records: terminal transitions append a `subagents:record` custom
  entry (best-effort) so history survives session resume.
- Background completion → `<task-notification>` via
  `send_user_message(deliver_as="follow_up")`; when the parent session is
  idle this starts a new turn automatically.
- Tests must run against the extensions branch (the main clone is on
  `personal`, which lacks `tau_coding.extensions`):
  `uv run --project ~/.herdr/worktrees/tau/worktree-extensions pytest tests/`

## Gap analysis vs pi-subagents

Everything from the original "buildable now" list is done (see above), plus
per-call `model`/`thinking` params (7390b43). Important discovery from that
batch: Tau children **always** discover skills natively (Tau defaults
resource paths from the session cwd), so `skills:` CSV means *additional*
full-body preloading, `skills: true` pins discovery to the parent cwd
(only matters under worktree isolation), and `skills: none` cannot disable
discovery — see README.

Also done since (bdfc94b): usage reporting (context-token estimate +
tool uses + duration — honest labels, see below), pi's 200ms notification
debounce, and `skills: none` via a new Tau seam
(`CodingSessionConfig.skills_enabled`, commit 4aee134 on tau branch
**`skills-seam`**, stacked on `worktree-extensions` — PR-shaped, gate
green, **not yet merged or pushed**; the extension feature-detects it and
degrades gracefully, and named-CSV preloads also disable the native index
to match pi's `noSkills`).

Smaller parity gaps that remain, roughly in value order:

- **Real (billed) token usage** — Tau exposes NO provider usage anywhere
  (`ProviderResponseEndEvent` carries only message + finish_reason; no
  usage on `AssistantMessage` or session state — verified empirically).
  pi surfaces lifetime tokens/context %. Needs a Tau seam: usage fields on
  `ProviderResponseEndEvent`, populated by the provider adapters. Until
  then the extension reports Tau's chars/4 `context_token_estimate`,
  labeled `context_tokens`.
- **`isolated` / `inherit_context` params** — pi's no-extension-tools mode
  (near-moot: our children already load no extensions) and
  parent-conversation forking (blocked: `ExtensionContext` doesn't expose
  parent messages).
- **Cron scheduling** — buildable in-extension today, but pi's UX leans on
  settings UI; decide scope first.

**Currently blocked on Tau capabilities** — but not dead ends. If a feature
here is limited by Tau's extension implementation, **extend Tau**: branch
off `worktree-extensions` in the tau repo, make the edits that enable the
capability (with tests, keeping the `tau_agent` purity boundary and the
existing gate green), and build the feature in this repo against that
branch. Design such capabilities from Pi's implementation first (see the
working agreement below). The design doc's non-goals list is the tracker
for what's missing, not a fence. Known gaps and where they'd land:

- Live progress for foreground agents — needs partial tool-result streaming:
  emit the already-defined `ToolExecutionUpdateEvent` from `tau_agent`'s
  loop and add an `on_update` seam to executors, then surface it through
  the runtime's wrapper and the TUI adapter.
- Custom rendering of the `<task-notification>` — needs message renderers
  registered through the extension API and consumed by the TUI transcript.
- Interactive `/agents` menu, live agent widget, conversation viewer —
  needs extension UI surfaces on the `UiBridge` (dialogs first: `select` /
  `confirm` / `input`; widgets/overlays after).

## Working agreement with the tau repo

- The extension must keep working against the `worktree-extensions` branch
  of the fork (pushed to `origin`, so it survives worktree cleanup). If a
  bug traces back to Tau's extension runtime
  (`src/tau_coding/extensions/`), fix it **in the tau repo** on that branch
  (or a branch off it), with a test in `tests/test_extensions.py` — don't
  work around it here.
- The same applies to missing capabilities, not just bugs: when a
  pi-subagents feature can't be built on the current extension API, branch
  off `worktree-extensions` and extend Tau to enable it, rather than
  shipping a degraded version here. Keep such branches PR-shaped:
  `worktree-extensions` is a clean candidate for upstreaming (personal
  tweaks were deliberately rebased out), so capability work should stay
  cleanly stacked on it.
- **Pi is the blueprint for new Tau capabilities.** Before designing any
  Tau feature added to support this extension, read how Pi implements it
  (`reference/pi`, usually `packages/coding-agent/src/core/extensions/` and
  `docs/extensions.md`) and port that design unless there is a strictly
  better way — "different" is not "better", and Tau is deliberately a
  Python port of Pi's architecture, so API names, semantics (first-wins,
  chaining order, fail-safe blocking), and event shapes should match Pi's
  wherever they translate. Where Pi's approach genuinely doesn't translate
  (TypeScript-isms, its jiti loader, npm packaging), record the deviation
  and the reason in the tau design doc the way the existing **Ruling:**
  notes do.
- Tau's verification gate: `uv run pytest && uv run ruff check . && uv run mypy`
  (4 pre-existing mypy errors in `tui/widgets.py`/`tui/app.py` are known;
  2 pre-existing ruff errors in `tests/test_coding_session.py` /
  `tests/test_tui_app.py`).
- Useful quirk for tests here: the extension's provider factories
  (`load_provider_settings` / `resolve_provider_selection` /
  `create_model_provider`) are module globals, so tests monkeypatch them on
  the loaded synthetic module (`tau_extension_tau_subagents_*` in
  `sys.modules`, excluding submodules) to inject `FakeProvider` — see
  `tests/test_extension.py::_patch_fake_provider`.

## Live smoke test

```bash
# Interactive (must run against the extensions branch/worktree):
cd ~/.herdr/worktrees/tau/worktree-extensions
uv run tau -x ~/Documents/personal-projects/tau-subagents
# "Spawn an explore subagent to summarize this repo's architecture."
# /agents  — check the run shows up

# Headless (verified working 2026-07-03):
cd ~/Documents/personal-projects/tau-subagents
uv run --project ~/.herdr/worktrees/tau/worktree-extensions tau -x . \
  -p "Use the agent tool to spawn an 'explore' subagent to summarize this repo."
```

The default provider (`openai-codex`, stored OAuth) works for real runs.
