# Agent Relay Roadmap Status

This document is the user-facing progress tracker for the project.

It answers three practical questions:

1. What are we building?
2. Which phase are we on?
3. Which phases are complete versus still pending?

## Current Status

- Current phase: `V1 Baseline Complete`
- Completed phases: `Phase 1`, `Phase 2`, `Phase 3`, `Phase 4`, `Phase 5`, `Phase 6`, `Phase 7`
- In progress: `None`
- Not started: `None`

## V1 Goal

V1 is complete when Agent Relay can:

1. Start a session under Claude Code or Codex.
2. Record durable checkpoints during work.
3. Prepare a failover to the other agent.
4. Launch the target agent from Agent Relay.
5. Continue work in the same session after handoff.
6. Preserve a coherent audit trail under `.agent-relay/`.

## Phase Tracker

### Phase 1: Reliable Session Core

Status: `Completed`

What this phase covers:

- typed models for session, handoff, checkpoint, and validation state
- centralized storage and validation
- schema-safe session loading and saving

What is done:

- `models.py` exists
- `storage.py` exists
- CLI now uses typed session state instead of raw dict mutation
- tests cover model validation and storage behavior

### Phase 2: First-Class Checkpoints

Status: `Completed`

What this phase covers:

- append-only checkpoint files
- `latest_checkpoint_id` in session state
- `summary.md` generated from the latest checkpoint

What is done:

- `checkpoints.py` exists
- `summary.py` exists
- `start` creates an initial checkpoint and summary
- `checkpoint` creates a new checkpoint and refreshes summary
- `failover` records the checkpoint id it is handing off from

### Phase 3: Stronger Resume Packets

Status: `Completed`

What this phase covers:

- stronger target-specific handoff packets
- richer operational context in Claude Code and Codex resumes
- cleaner separation into a dedicated resume module

What is done:

- Claude Code and Codex resume packets are different
- resume packets include checkpoint id, decisions, blockers, touched files, validation, and handoff history
- resume rendering now lives in `resume.py`
- resume rendering supports configurable evidence depth

### Phase 4: Launch Execution

Status: `Completed`

What this phase covers:

- dry-run and execute launch flows
- recording launch result in session state
- a cleaner launch module boundary

What is done:

- `launch` subcommand exists
- dry-run prints launch target, resume path, command, and instructions
- `--execute` runs the prepared command
- launch success and failure state are recorded
- launch execution now lives in `launcher.py`
- launcher behavior has direct module-level tests

### Phase 5: Adapter Boundaries

Status: `Completed`

What this phase covers:

- turning basic agent profiles into a cleaner adapter interface
- keeping provider-specific behavior out of the session core

What is done:

- `agents.py` now exposes an explicit adapter contract
- Claude Code and Codex adapters are separate explicit adapter instances
- launch behavior now resolves through the adapter registry instead of loose profile helpers
- resume and launcher modules use adapter lookups without touching the session core
- adapter behavior has direct tests

### Phase 6: Lightweight Capture Hooks

Status: `Completed`

What this phase covers:

- faster checkpoint ergonomics
- optional autosave helpers
- cleaner pause or prepare flows before handoff

What is done:

- `capture.py` now centralizes shared session update and checkpoint capture behavior
- `checkpoint` supports richer capture flags for notes, validation, and touched files
- `pause` writes a final paused checkpoint quickly
- `prepare` writes a paused pre-handoff checkpoint and requires an explicit next action
- optional git-based touched-file capture is available
- optional autosave file hooks exist for research notes, implementation notes, and validation summary

### Phase 7: Validation and Demo Flow

Status: `Completed`

What this phase covers:

- a reproducible demo walkthrough
- end-to-end validation in a real repository
- integration-style confidence beyond unit tests

What is done:

- the demo walkthrough now validates both `claude -> codex` and `codex -> claude` in one session
- the walkthrough uses the installed CLI path and current `pause` and `prepare` flow
- a bidirectional integration test now exercises session continuity across multiple handoffs
- a developer-facing release checklist now exists for the local-first MVP

## What You Can Use Right Now

The CLI already supports:

- `start`
- `checkpoint`
- `pause`
- `prepare`
- `failover`
- `launch`
- `inspect`

That means the current local-first v1 baseline is implemented, usable, and validated locally.

## Recommended Next Step

The next engineering step is:

1. package the CLI more cleanly for distribution
2. run the walkthrough in additional real repositories
3. decide which post-v1 hardening work belongs in the next milestone

That moves the project from v1 completion into packaging and broader real-world validation.
