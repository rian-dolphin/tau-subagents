# Handoff: fleshing out tau-subagents

**Mission:** evolve this extension toward feature parity with
[pi-subagents](https://github.com/tintinweb/pi-subagents) (the extension it
ports), within the capabilities of Tau's extension system — and extend Tau
itself where a capability is genuinely missing.

Written 2026-07-03 at the end of the session that built Tau's extension
system and this initial port.

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

- `agent` tool — spawns a scoped in-process `CodingSession` (in-memory
  transcript, `extensions_enabled=False` so no recursive spawning). Params:
  `prompt`, `description`, `subagent_type`, `run_in_background`.
- `get_subagent_result` tool — poll, or `wait=true` to block; claiming a
  result suppresses the redundant background notification.
- `/agents` command — plain-text list of types and runs.
- Agent types from `~/.tau/agents/*.md` and `<cwd>/.tau/agents/*.md`
  (frontmatter: `description`, `tools`, `model`; body = system prompt).
  Built-ins: `general`, `explore`.
- Background completion → `<task-notification>` via
  `send_user_message(deliver_as="follow_up")`; when the parent session is
  idle this starts a new turn automatically.
- Tests in `tests/` run against a Tau checkout:
  `uv run --project ~/Documents/personal-projects/tau pytest tests/`

## Gap analysis vs pi-subagents

Buildable **now** with Tau's current extension API:

- **`steer_subagent` tool** — child sessions expose `steer()`; pi queues
  steers if the session isn't up yet (`pendingSteers`).
- **`resume` param on `agent`** — keep completed runs' sessions (or persist
  them) and allow follow-up prompts.
- **`max_turns` + grace steering** — count `turn_end` events; at the limit
  steer "wrap up now", hard-`cancel()` after N grace turns
  (pi: `agent-runner.ts:615`). Tau's harness has no `max_turns` seam on
  `CodingSessionConfig`, so do it via event counting.
- **Concurrency queue** — pi defaults to 4 concurrent, queues the rest
  (`agent-manager.ts`). Ours spawns unbounded.
- **Join modes** — batch multiple same-turn background completions into one
  consolidated notification (`group-join.ts`, "smart" mode with 30s partial
  timeout).
- **Worktree isolation** — `git worktree add` before the run, commit to a
  branch after (`worktree.ts`); plain subprocess git, no Tau support needed.
- **Skill preloading** (`skills:` frontmatter) — inject skill markdown into
  the child's system prompt; Tau exposes `load_skills`/`format_skill_invocation`.
- **Persistent per-agent memory** (`memory:` frontmatter) — pi keeps
  `MEMORY.md` + files under `.pi/agent-memory/<name>/` (`memory.ts`).
- **`prompt_mode: append`** — build the child prompt as parent system prompt
  + bridge + agent body for KV-cache prefix reuse; parent prompt available
  via `tau.context.system_prompt`.
- **Settings file** — `~/.tau/subagents.json` merged with
  `<cwd>/.tau/subagents.json` (concurrency, defaults), mirroring pi.
- **Output files** — stream child transcripts to a tmp dir so
  `get_subagent_result` can point at a full log (`output-file.ts`).
- **Persist run records** — `await tau.append_entry("subagents:record", …)`
  so runs survive resume (pi does this).

**Blocked on Tau capabilities** (would need changes in the tau repo first —
the design doc's non-goals list is the authoritative tracker):

- Live progress for foreground agents — needs partial tool-result streaming
  (`ToolExecutionUpdateEvent` emission in `tau_agent`'s loop + an
  `on_update` seam in executors).
- Custom rendering of the `<task-notification>` — needs message renderers.
- Interactive `/agents` menu, live agent widget, conversation viewer —
  needs extension UI surfaces (dialogs/widgets/overlays).
- Cron scheduling — buildable in-extension, but pi's UX leans on settings
  UI; decide scope first.

## Working agreement with the tau repo

- The extension must keep working against the `worktree-extensions` branch
  of the fork. If a bug traces back to Tau's extension runtime
  (`src/tau_coding/extensions/`), fix it **in the tau repo** on that branch
  (or a branch off it), with a test in `tests/test_extensions.py` — don't
  work around it here.
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
cd ~/Documents/personal-projects/tau  # or the worktree
uv run tau -x ~/Documents/personal-projects/tau-subagents
# "Spawn an explore subagent to summarize this repo's architecture."
# /agents  — check the run shows up
```

The default provider (`openai-codex`, stored OAuth) works for real runs.
