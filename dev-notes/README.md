# Dev notes (contributor build-log)

This folder holds the internal design records for tau-subagents.
We do not publish these notes.
They help contributors trace how we made the important decisions.

We write these notes in ASD-STE100 (Simplified Technical English).
Keep sentences short.
Use the active voice.
Write one idea in each sentence.

## Contents

- `adr/` — architecture decision records.
  Each record has a status, a context, a decision, and the consequences.

## How to add a record

1. Copy the format of an existing record in `adr/`.
2. Give the record the next free number.
3. Set the status to `Proposed`, `Accepted`, or `Rejected`.
4. Write the record in ASD-STE100.
5. Update the status when the decision changes.

## Reference

The upstream project is `pi-subagents` (nicobailon/pi-subagents).
This project is a Python port of it for Tau.
Many decisions compare our design with the upstream design.
The upstream source is at `/Users/rian/Documents/personal-projects/pi-subagents`.
The Tau source is at `/Users/rian/Documents/personal-projects/tau`.
