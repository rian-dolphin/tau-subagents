# tau-subagents

Claude Code-style autonomous subagents for [Tau](https://github.com/rian-dolphin/tau),
ported from [pi-subagents](https://github.com/tintinweb/pi-subagents).

Gives the model an `agent` tool that delegates a task to a **subagent** — a
second, scoped Tau coding session running in-process with its own system
prompt, its own tool allow-list, and an in-memory transcript. Foreground
subagents block and return their final report as the tool result. Background
subagents return an id immediately and inject a `<task-notification>` into the
parent conversation when they finish, which starts a new turn automatically.

Also registers:

- `get_subagent_result` — poll a background agent, or `wait=true` to block for it
- `steer_subagent` — inject a message into a running (or queued) agent
- `/agents` — interactive menu over agent types, runs, and scheduled jobs
- an **agents strip** under the prompt (Tau builds with the component seam):
  live subagent list with an embedded conversation viewer (see "The agents
  strip")

## Install

The implementation lives in `src/tau_subagents/`; `pyproject.toml` declares
the entry point via Tau's extension manifest (`[tool.tau]
extensions = ["src/tau_subagents/extension.py"]`), so the repo itself is
loadable as a Tau extension. Either try it per-run:

```bash
tau -x /path/to/tau-subagents
```

or install it permanently by symlinking the clone into Tau's user extensions:

```bash
git clone git@github.com:rian-dolphin/tau-subagents.git ~/code/tau-subagents
ln -s ~/code/tau-subagents ~/.tau/extensions/tau-subagents
```

Update with `git pull`, then restart `tau` (or `/reload`).

Beyond Tau, this branch takes a direct dependency on `textual`: on the
`component-seam-experiment` branch the agents strip and conversation viewer are
extension-owned Textual widgets (see "The agents strip").

## Use

Ask the model to delegate:

> Use a subagent to summarize this repository's architecture.

> Spawn an explore subagent to find where slash commands are registered,
> run it in the background.

`/agents` manages agents. On Tau builds with the `ui-dialogs` seam it opens
an interactive menu (ported from pi's showAgentsMenu): selecting a run opens
its conversation viewer (see "The agents strip"), falling back to a dialog
submenu (view result / steer / stop) on hosts without the component seam. pi's
create wizard and settings menu are not ported yet. Without the seam — or
headless — it prints the plain-text list instead.

## The agents strip

> **Experimental (branch `component-seam-experiment`).** On this branch the
> whole agent UI is owned by the extension as real Textual widgets
> (`src/tau_subagents/ui/`), mounted through Tau's generic *component seam*
> (`context.ui.components`: `set_slot_widget` / `open_main_view` /
> `register_key_interceptor`) rather than Tau core's older transcript-sources
> seam. Tau core stays agent-agnostic — it only hosts widgets.

On component-capable Tau builds, spawning a subagent shows a strip under the
prompt (`AgentStripWidget`) listing `main` plus every queued/running agent (the
Claude Code pattern), a braille spinner for running runs and each finished
run's own status glyph. `←`/`↓` in an empty prompt enters the strip; once
focused it owns `↑`/`↓` navigation, `Enter` opens the selected agent's
conversation viewer, and `Esc` (or `↑` past the top) hands focus back to the
prompt. Clicking a row opens it directly.

`Enter` opens the conversation viewer (`ConversationViewer`) in the main area
as a display-toggled view — the strip stays visible for peripheral fleet
awareness. It renders the run's live conversation (reusing Tau's own transcript
rendering), carries a header with label/status/detail, and embeds a steer
composer: `Enter` opens it, type + `Enter` sends a steering message, `Esc`
cancels the composer. `x` twice stops the run (a two-press guard), and `Esc`/`q`
closes the viewer. Live updates are push-based — the viewer subscribes to the
run's change listeners rather than polling. Finished agents leave the strip
after a short linger; `/agents` still reaches their transcripts. The agent
tool-call row in the main transcript shows a braille spinner and a live elapsed
timer while the run executes (Tau core behavior, driven by this extension's
`render_call` lines).

## Agent types

Two built-in types ship with the extension:

- `general` — full coding toolset, for research and multi-step tasks
- `explore` — read-only (`read` + `bash`), for searching and summarizing

Add your own as markdown files at `.tau/agents/<name>.md` (project) or
`~/.tau/agents/<name>.md` (user). The filename is the type name, the body is
the subagent's system prompt, and frontmatter supports:

```markdown
---
description: Reviews code for security issues.
tools: read, bash
model: gpt-5.2
---
You are a security reviewer. Investigate the code you are pointed at and
report vulnerabilities with file references.
```

Project definitions win over user definitions with the same name.

Agent-type frontmatter also supports:

- `max_turns` — non-negative integer soft turn limit (`0` = unlimited)
- `skills` — comma-separated skill names to preload (see below)
- `prompt_mode` — `replace` (default) or `append` (see below)
- `memory` — `user`, `project`, or `local` persistent memory (see below)
- `isolation` — `worktree` to run in an isolated git worktree (see below)

## Worktree isolation

Pass `isolation: "worktree"` to the `agent` tool (or set it in agent-type
frontmatter, which wins) and the subagent runs inside a detached git worktree
of the current repo instead of your working tree. When the run finishes, any
changes are committed (`tau-agent: <description>`) and preserved on a
`tau-agent-<id>` branch in the base repo — the result tells you to
`git merge tau-agent-<id>`. Clean worktrees are removed without a trace.
Isolation is strict: if the cwd is not a committed git repo, the spawn fails
rather than silently running unisolated. Worktree agents cannot be resumed —
their working directory is removed when the run finishes.

## Output files

Every run streams its child transcript as JSONL to
`<tmpdir>/tau-subagents-<uid>/<encoded-cwd>/<session>/tasks/<agent-id>.jsonl`
(first entry is the prompt; new messages are flushed after each turn). The
path is shown in background spawn results (`Output file: ...`), in completion
notifications (`<output-file>` tag plus a transcript footer), and in
`get_subagent_result` output, so you can `tail` a long-running agent from
outside the conversation.

## Per-agent memory (`memory:` frontmatter)

Set `memory: user|project|local` on an agent type to give it a persistent
memory directory:

    user      ~/.tau/agent-memory/<name>/
    project   <cwd>/.tau/agent-memory/<name>/
    local     <cwd>/.tau/agent-memory-local/<name>/

At spawn, a memory block is injected into the child's system prompt (before
any preloaded skills) showing the first 200 lines of `MEMORY.md` and
instructions to keep it as an index linking to detail files. Agents whose
toolset can write (has `write` or `edit`) get read-write memory — the
directory is created and read/write/edit tools are ensured; read-only
toolsets get a read-only memory block and no directory is created.

## Run records

Every terminal transition (completed, steered, aborted, error, cancelled —
including resumes) appends a compact `subagents:record` entry to the parent
session log (id, type, description, status, result, error, turns, tool
calls), so subagent history survives session resume. Persistence is
best-effort and never fails a run.

## Skills (`skills:` frontmatter)

Children always inherit Tau's native skill discovery: every subagent session
discovers skills on its own (`~/.tau/skills/`, `~/.agents/`, and the child
cwd's `.tau/skills/`) and gets an `<available_skills>` index in its prompt
plus on-demand skill expansion. The `skills:` frontmatter layers on top of
that:

- `skills: foo, bar` — preloads the named skills' full bodies into the
  child's system prompt as `# Preloaded Skill: <name>` sections (a missing
  name becomes a `(Skill "<name>" not found)` placeholder), so the child
  doesn't have to read them on demand. Native discovery is turned off for
  such agents (matching pi, which sets `noSkills` for named preloads so the
  same skill isn't both preloaded and indexed) — the child gets exactly the
  named skills. Requires the `skills_enabled` seam; against an older Tau the
  index stays on alongside the preloaded bodies.
- `skills: true` (or `*` / `all`) — pins the child's resource discovery
  (skills **and** project context files such as AGENTS.md) to the **parent**
  working directory. This only makes a difference under
  `isolation: worktree`, where default discovery would otherwise resolve
  against the worktree copy — e.g. uncommitted project skills would be
  invisible to the isolated child.
- `skills: none` / `false` — disables the child's skill discovery entirely
  (no `<available_skills>` index, no `/skill:` expansion) when Tau supports
  the `CodingSessionConfig.skills_enabled` seam (tau fork branch
  `skills-seam`). Against an older Tau without the seam, the extension
  feature-detects and falls back to the previous behavior: native discovery
  stays on and this value only skips preloading.

In `prompt_mode: append` the parent prompt prefix already carries the
parent's skill index verbatim.

## Model and thinking overrides

The `agent` tool accepts `model` (fuzzy or full model selection for the
subagent; default is the agent type's `model`, else the parent's) and
`thinking` (one of `off`, `minimal`, `low`, `medium`, `high`, `xhigh`;
default `medium`). Agent-type frontmatter `model:` and `thinking:` win over
the tool params, matching pi's precedence. Note: a typo'd frontmatter
`thinking:` value is silently ignored (falls back to the param/default),
unlike the tool param, which errors.

## `prompt_mode: append`

By default (`replace`) the agent body becomes the child's base system prompt.
With `prompt_mode: append` the child instead keeps the parent session's full
system prompt as a byte-identical prefix, followed by a sub-agent bridge block
(tool-usage etiquette), an `<active_agent name="..."/>` tag, an environment
block (cwd, git branch, platform), the agent body wrapped in
`<agent_instructions>`, and any preloaded skill sections. This makes the child
behave like the parent with extra instructions layered on. If the parent
prompt is unavailable, append mode falls back to replace-mode assembly.

## Join modes (background notification batching)

`defaultJoinMode` in settings controls how background completion notifications
are delivered:

- `smart` (default) / `group` — background agents spawned within a 100ms
  window form a group; their completion notices are consolidated into one
  "Background agent group completed" message. If some members are still
  running 30s after the first finishes, a partial notification is sent
  (`(partial — others still running)`) and the stragglers re-group on a 15s
  cadence until everyone reports.
- `async` — never batched; every agent notifies individually (one
  `<task-notification>` follow-up each).

Foreground agents never join groups. Members whose results were already read
via `get_subagent_result` are skipped at delivery time. Groups need at least
two members — a lone background agent always notifies individually.

All completion notifications (individual and group) are held for 200ms before
delivery; reading the result with `get_subagent_result` inside that window
cancels the now-redundant notification.

## Usage reporting

Results and notifications include usage stats: tool uses, tokens, an
estimated context size, and run duration. They appear in the foreground
completion line (`Agent completed in <X>s (<N> tool uses, <K> tokens).`),
the `get_subagent_result` header (`Usage:`), the `<usage>` block of task
notifications (`<total_tokens>`), and the `steer_subagent` confirmation's
`Current state:` line.

Token figures come in two flavors:

- **Real billed tokens** (`<total_tokens>`, `<K> tokens`) — available when Tau
  has the `provider-usage` seam (branch `provider-usage`; usage fields on
  `AssistantMessage` populated by the provider adapters). Following pi's
  semantics, the lifetime total sums `input + output + cache_write` per
  assistant response; cache *reads* are excluded because each turn re-reads
  the whole cached prefix, so summing them would count the prefix once per
  turn (pi issue #38).
- **Context estimate** (`~<K> context tokens`, `<context_tokens>`) — Tau's
  deterministic chars/4 estimate of the child's current context size. Always
  available; the only token figure on Tau branches without the usage seam.

## Live activity

While an agent runs, its tool-call row in the transcript animates: a braille
spinner in place of the `▸` marker plus a live elapsed timer (Tau core's
pending-tool rendering). For richer live detail, open the agent's view from
the strip or `/agents` — per-event textual progress lines were deliberately
removed as transcript noise.

## Notification rendering

On Tau builds with the `message-renderers` seam, background completion
notifications render as pi-style cards (status icon, bold description, dim
stats line with turns/tools/tokens/duration, collapsed result preview,
transcript path) instead of raw `<task-notification>` XML bubbles — the raw
XML still enters the model's context unchanged. Group notifications stack
one card per run. Without the seam, notifications arrive as plain user
messages exactly as before.

## Concurrency and the background queue

Only **background** agents count toward a concurrency limit (`maxConcurrent`,
default 4). Foreground agents always start immediately. When the limit is
reached, further background spawns are **queued** FIFO — the tool reports the
agent as `queued`, and the run starts automatically as soon as a slot frees up.
Queued runs show up in `/agents` with status `queued`.

## Steering a running agent

`steer_subagent` sends a message to a running agent; it is delivered after the
agent's current tool execution and appears as a user message in the agent's
conversation. Messages sent to a **queued** agent (one with no live session
yet) are held and flushed the moment its session initializes. Typing inside
an agent's in-place view (see "The agents strip") and the `/agents` steer
dialog use the same path.

## Turn limits (`max_turns`)

Pass `max_turns` to the `agent` tool (or set it in agent-type frontmatter, or
`defaultMaxTurns` in settings) to cap how many turns an agent runs. When the
limit is hit, the agent is steered with a wrap-up message asking it to give its
final answer. If it keeps going past a grace period (`graceTurns`, default 5),
it is hard-cancelled. An agent that wraps up within grace finishes with status
`steered` (treated as success); one that has to be cancelled ends `aborted`.

Precedence: agent-type frontmatter wins over the tool `max_turns` param, which
wins over `defaultMaxTurns` from settings.

## Resuming a finished agent

Call the `agent` tool with `resume=<id>` and a new `prompt` to continue a
finished agent's session — its full conversation history is kept alive. Resume
always runs in the foreground and returns the new final answer inline. (Turn
limits are not re-enforced on resume.)

## Scheduling agents (`schedule`)

Pass `schedule` on the `agent` tool to run it later or repeatedly, ported
from pi's scheduler. Formats: 5-field cron (`0 9 * * 1` — numeric fields
only; `*`, lists, ranges, `*/n`; minimum granularity one minute), intervals
(`5m`, `1h`; minimum 5s), and one-shots (`+10m` relative or an ISO
timestamp). Scheduled spawns are always background, bypass the concurrency
queue, and deliver the normal completion notification; `schedule` is
incompatible with `resume`, `inherit_context`, and `run_in_background:
false`, matching pi. Jobs persist per session in
`<cwd>/.tau/subagent-schedules/<session_id>.json` (PID-locked, atomic);
missed fires are skipped, past one-shots are disabled. Manage jobs via
`/agents → Scheduled jobs` (list + cancel). Times are naive local time —
across a DST transition a fire can land up to an hour off.

## Inheriting the parent conversation (`inherit_context`)

By default a subagent starts with fresh context. Pass `inherit_context: true`
on the `agent` tool (or set it in agent frontmatter; the param wins) to
prepend a digest of the parent conversation to the child's prompt, following
pi's design: user and assistant text turns as `[User]:` / `[Assistant]:`
lines (tool results are dropped as too verbose), wrapped in pi's verbatim
`# Parent Conversation Context` framing. The digest is captured at spawn
time, so queued background runs see the conversation as of the tool call.
Compaction summaries appear as `[User]` turns (Tau folds them into user
messages during replay) rather than pi's `[Summary]:` framing.

Requires a Tau build with the `parent-context` seam
(`tau.context.transcript`); without it the tool call fails with an
explanatory error rather than silently spawning without context.

## Settings

Settings are read from two JSON files and shallow-merged, project overriding
user (missing or malformed files are ignored):

    ~/.tau/subagents.json          user defaults
    <cwd>/.tau/subagents.json      project overrides

| key | type | default | meaning |
|---|---|---|---|
| `maxConcurrent` | int 1–1024 | 4 | max concurrent background agents |
| `defaultMaxTurns` | int 0–10000 | unlimited | default turn limit (`0` = unlimited) |
| `graceTurns` | int 1–1000 | 5 | extra turns allowed after the soft limit |
| `defaultJoinMode` | `async`\|`group`\|`smart` | `smart` | background notification batching (see Join modes) |

Out-of-range or wrong-typed values are silently dropped and the field keeps its
default.

## Notes and limits

- Subagents run with `extensions_enabled=False`, so they cannot spawn
  subagents recursively. This also means children never receive extension or
  MCP tools — pi's `isolated` param (which strips them) has nothing to strip
  here and is deliberately not ported.
- Live activity while a subagent works is the spinner + elapsed timer on its
  tool row and the in-place view (see "Live activity"); on older Tau builds
  without those seams, runs are silent while they work — prefer background
  mode for long tasks there.
- `/reload` rebuilds extension state; background runs in flight at reload
  time are orphaned.

## Tests

The tests need Tau's packages importable. The repo's own environment resolves
Tau via the path source in `pyproject.toml` (`[tool.uv.sources]` — edit it if
your Tau checkout lives elsewhere):

```bash
uv run pytest
```

Borrowing a Tau checkout's environment also still works:

```bash
uv run --project /path/to/tau pytest /path/to/tau-subagents/tests
```
