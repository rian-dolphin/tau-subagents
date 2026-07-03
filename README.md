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
- `/agents` — list agent types and every run's status

## Install

The extension is this directory (Tau loads any directory containing an
`extension.py`). Either try it per-run:

```bash
tau -x /path/to/tau-subagents
```

or install it permanently by symlinking the clone into Tau's user extensions:

```bash
git clone git@github.com:rian-dolphin/tau-subagents.git ~/code/tau-subagents
ln -s ~/code/tau-subagents ~/.tau/extensions/tau-subagents
```

Update with `git pull`, then restart `tau` (or `/reload`).

No dependencies beyond Tau itself — the extension only imports `tau_agent` /
`tau_coding` and the standard library.

## Use

Ask the model to delegate:

> Use a subagent to summarize this repository's architecture.

> Spawn an explore subagent to find where slash commands are registered,
> run it in the background.

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

Agent-type frontmatter also supports `max_turns` (a non-negative integer soft
turn limit; `0` means unlimited).

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
yet) are held and flushed the moment its session initializes.

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
| `defaultJoinMode` | `async`\|`group`\|`smart` | `smart` | reserved for notification batching |

Out-of-range or wrong-typed values are silently dropped and the field keeps its
default.

## Notes and limits

- Subagents run with `extensions_enabled=False`, so they cannot spawn
  subagents recursively.
- Foreground subagents are silent while they work — Tau does not yet stream
  partial tool results. Prefer background mode for long tasks.
- `/reload` rebuilds extension state; background runs in flight at reload
  time are orphaned.

## Tests

The tests need Tau's packages importable. From a Tau checkout's environment:

```bash
uv run --project /path/to/tau pytest /path/to/tau-subagents/tests
```
