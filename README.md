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
