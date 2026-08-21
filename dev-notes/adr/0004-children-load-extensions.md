# ADR 0004 — Children load extensions (subagents can spawn subagents)

## Status

Accepted (2026-08-21).

## Context

Child sessions were created with `extensions_enabled=False`, with the stated
rationale that subagents should not spawn recursively. That diverged from
pi, where children do receive extension and MCP tools — pi's `isolated`
param exists precisely to strip them on request. The README acknowledged
the divergence ("pi's `isolated` param has nothing to strip here").

A child without the extension's tools also sends a different serialized
tools array than the parent. Anthropic-protocol providers key the cached
prompt prefix on tools → system → messages, so any child meant to share
the parent's cache prefix is defeated at position zero by the missing
tools, regardless of how faithfully its prompt matches.

## Decision

Set `extensions_enabled=True` for child sessions. Children discover
extensions natively (`~/.tau/extensions` plus explicit paths), the same
discovery the parent went through, so subagents can spawn subagents. Each
child's extension instance has its own manager; nested completions notify
the child's conversation, and the child's `session_shutdown` (emitted by
`aclose`) cancels grandchildren.

Port pi's `isolated` tool param as the opt-out: `isolated: true` spawns
the child with `extensions_enabled=False` — core tools only, no further
spawning. This is cheaper and simpler than pi's load-then-strip, with the
same outcome.

No depth limit: there is no in-process channel to communicate depth to a
child's separately-imported extension instance without an env-var race,
and pi does not limit depth either. The model's judgment and `isolated`
are the controls.

## Consequences

Child sessions never fire `session_start` (hosts emit it explicitly; the
manager does not), so a child's extension instance registers tools but
starts no scheduler, UI, or retention sweep — scheduled spawns from inside
a child report the scheduler as inactive.

Tests isolate `HOME` (conftest), because children now discover extensions
from the real `~/.tau/extensions` — on a developer machine every test
child would otherwise load the installed extension.

Tool-pool parity with the parent holds when child discovery yields the
same extensions as the parent's session. A parent started with `-e`
extension flags that discovery does not reproduce would still diverge;
explicit paths are not observable through the extension API today.
