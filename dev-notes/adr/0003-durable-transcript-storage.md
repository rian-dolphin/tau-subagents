# ADR 0003 — Store subagent transcripts in a durable location

## Status

Accepted (2026-08-17). Amended 2026-08-24 to retain transcripts indefinitely by default.

## Context

Each run streams its child transcript as JSONL to a file.
The current path is in the system temp directory:
`<tmpdir>/tau-subagents-<uid>/<encoded-cwd>/<parent-session>/tasks/<agent-id>.jsonl`.

The temp directory has the wrong lifetime for this data.
macOS removes temp files after some days.
Many Linux systems clear the temp directory at boot.
The transcript is most useful after time passes, for example when you examine a failed scheduled job.

The upstream project stores child session files in a durable location.
It puts them under the parent session directory: `~/.pi/agent/sessions/<parent-id>/<run-id>/`.
It puts only disposable run state in the temp directory.
It removes old data with a 30-day retention worker.

The temp-only storage in this port is a regression, not a faithful port.

## Decision

Move the transcript files to a durable directory under the Tau home:
`~/.tau/subagents/<cwd-slug>-<hash>/<parent-session-id>/tasks/<agent-id>.jsonl`.

Use the same slug method as `tau_coding.paths.project_session_dir`.
Do not write into `~/.tau/sessions/`.
The Tau session index must not see these files.

Add a retention sweep:

- Run the sweep on the `session_start` hook, in a thread.
- Delete transcript files that are older than the retention period.
- Remove the directories that become empty. Keep the root.
- Make the period a setting, `transcriptRetentionDays`. By default it is unset
  and transcripts are retained indefinitely; a positive value enables the sweep.
- A value of 0 keeps the old temp-directory behavior and stops the sweep.

Add the environment variable `TAU_SUBAGENTS_DIR`.
It overrides the durable root.
The test suite uses it to keep test transcripts out of the real user home.
Upstream pi-subagents has the same seam, `PI_SUBAGENTS_TEMP_ROOT`.

## Known limit

The extension assumes the `.tau` name under the user home.
A host can move the Tau home with `TauPaths(home=...)`.
The extension cannot see that move.
Tau does not expose the resolved `TauPaths` in the `ExtensionContext`.
The files `agents.py`, `memory.py`, and `prompts.py` have the same limit.
We track the fix in an upstream Tau issue: expose `paths` on `ExtensionContext`.
Until then, `TAU_SUBAGENTS_DIR` is the manual escape hatch.

## Consequences

- Transcripts of scheduled and background jobs survive a reboot.
- Disk use is unbounded by default, matching regular Tau sessions. Users can
  opt into age-based cleanup with `transcriptRetentionDays`.
- The `<output-file>` tag, the spawn result, and the `get_subagent_result` header show the new path.
- The files stay out of the Tau session pickers because they are not in `~/.tau/sessions/`.
