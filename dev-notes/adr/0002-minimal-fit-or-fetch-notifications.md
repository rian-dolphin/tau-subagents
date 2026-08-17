# ADR 0002 — Keep completion notifications minimal and fit-or-fetch

## Status

Accepted (2026-08-17). Implemented in commit `bbc2c6d`.

## Context

When a background subagent completes, the extension puts a `<task-notification>` block into the parent conversation.
The old block contained the agent type, the turn count, a `<usage>` block, and a truncated result preview.

The stats were duplicates.
The `get_subagent_result` header shows the same stats.
The TUI card shows the same stats from the `details` payload, outside the model context.

The truncated preview was harmful.
A partial preview can cause the parent model to act on incomplete information.
The parent model can decide to not call `get_subagent_result`.

## Decision

The notification block contains only the agent id, the description, the output file path, the status, and the result.

The result is fit-or-fetch:

- If the result is not more than the cap, include the full result.
- If the result is more than the cap, do not include any part of it.
  Include a `<result-pending>` pointer that tells the parent to call `get_subagent_result` before it acts.

The caps are 500 characters for one agent and 300 characters for each agent in a group notice.
We never truncate a result.

## Consequences

- A short result does not need a tool call. This keeps the fast path cheap.
- A long result always causes a `get_subagent_result` call. The parent always acts on full information.
- `get_subagent_result` returns the full result with no cap. This does not change.
- The TUI card keeps its stats and its preview. The card data is display-only.
