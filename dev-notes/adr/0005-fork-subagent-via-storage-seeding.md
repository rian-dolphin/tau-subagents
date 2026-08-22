# ADR 0005 — Fork subagents via session-storage seeding

## Status

Accepted (2026-08-21).

## Context

Claude Code added a `fork` subagent type: a child that inherits the entire
conversation — system prompt, model, tools, and the full message history as
real messages — instead of starting fresh. Its value is a side task with
complete context whose tool calls stay out of the parent transcript, and a
shared prompt cache because the child's prefix is byte-identical to the
parent's.

The extension already has two partial mechanisms: `inherit_context` (a text
digest of user/assistant turns, tool results dropped) and
`prompt_mode: append` (parent system prompt as a prefix plus a bridge
block). Neither gives the child real history, and the bridge block breaks
the byte-identical prefix.

Tau has no API for handing a new session an existing history. But
`CodingSession.load` replays whatever entries its storage already holds —
sessions are derived state over an append-only entry log.

## Decision

Implement fork by seeding the child's in-memory storage before
`CodingSession.load`: a `SessionInfoEntry` plus one `MessageEntry` per
parent transcript message, chained by `parent_id` (the chain is load-bearing:
the child's first persisted turn replays root-to-leaf, and unchained entries
are silently dropped by missing-parent detachment).

Details, each chosen over an alternative:

- **Built-in type with a `fork: bool` field**, not a frontmatter feature.
  Fork-ness overrides tools, model, thinking, and prompt assembly; a `.md`
  file declaring it would have every other key silently ignored. The name
  `fork` is reserved — user files with that stem are skipped.
- **No `ModelChangeEntry`/`ThinkingLevelChangeEntry` in the seed.**
  `state.model` would override the provider instance the manager builds;
  model fidelity lives in provider selection instead, which resolves
  against the parent's provider name and model explicitly (the default
  provider may not declare the parent's model). The parent's thinking
  level is not exposed to extensions, so forks pass no override and the
  provider's persisted per-model level applies — the same source the
  parent's session used, keeping the thinking config in the fork's
  requests identical (a differing thinking config costs prompt cache;
  exposing the live level on `ExtensionContext` is a small upstream ask).
- **Dangling tool calls are closed at capture time** with a neutral
  non-error filler result. Tau's own repair would fill them with an
  is-error "interrupted" message — the fork's first sight of its origin
  would read as a failure — and rewrite the branch with a repair entry.
- **Fork framing rides on the task prompt**, wrapped in `<fork_task>`, not
  the system prompt: appending anything to the system block breaks the
  shared cache. Under worktree isolation it warns that absolute paths from
  the conversation point at the parent checkout. (Cache sharing also
  requires the tool pool to match — see ADR 0004, which enables extensions
  in children; forks ignore the `isolated` param for the same reason.)
- **The output file skips seeded messages** (`inherited=N` on the writer);
  otherwise every fork would dump the whole parent transcript into
  durable storage under ADR 0003 retention.
- **Guards:** model/thinking params ignored (parent wins), resume and
  schedule rejected, `inherit_context` ignored as redundant. Foreground
  forks are allowed — a deliberate divergence from Claude Code's
  background-only forks; there is no reason to forbid blocking on one.

## Consequences

Fork token figures are not comparable to other agent types: the whole
inherited prefix bills as input on turn one, and `context_tokens` starts
large. The existing cache-read exclusion keeps this from compounding.

Storage seeding is honest but knows too much about Tau's replay mechanics.
The principled upstream seam is a `CodingSessionConfig` seed-entries (or
initial-messages) field; if upstream adds one, the seeding helper collapses
into a config argument.
