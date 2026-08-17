# ADR 0001 — Run subagents in the parent process

## Status

Accepted (2026-08-17).

## Context

The upstream project, pi-subagents, starts each subagent as a `pi` CLI child process.
It does this because a pi extension cannot host a second session in its own process.
The subprocess model needs much support code: a watchdog, status files, event logs, orphan cleanup, and startup retries.

Tau is a Python library.
An extension can create a `CodingSession` object directly.
The extension can then read the child event stream in the same process.

## Decision

We run each subagent in the parent process.
Each subagent is an asyncio task that owns one in-memory `CodingSession`.
We do not start child processes.

## Consequences

Good:

- The live viewer reads `run.session.messages` directly.
- Steering puts messages into the child session without serialization.
- Cancellation is exact: `run.session.cancel()` and `task.cancel()`.
- Turn limits, usage, and live stats come from in-memory event objects.
- We do not need the process-management layer that upstream has.

Bad:

- Background agents stop when the parent process stops.
- Child sessions are not real Tau session files.
- A child cannot use `tau --resume`.
- The extension must write its own transcript files (see ADR 0003).

## Future option

If scheduled jobs must continue after the TUI closes, add a detached mode for scheduled runs only.
That mode starts a headless `tau -p ...` process with a real session directory.
Keep interactive foreground and background agents in-process.
Do not move the whole extension to the subprocess model.
