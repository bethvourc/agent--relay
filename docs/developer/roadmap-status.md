# Agent Relay Roadmap Status

This document is the user-facing progress tracker for the project.

It answers three practical questions:

1. What are we building?
2. Which phase are we on?
3. Which phases are complete versus still pending?

## Current Status

- Current phase: `Phase 4`
- Completed phases: `Phase 1`, `Phase 2`, `Phase 3`
- In progress: `Phase 4`
- Not started: `Phase 5`, `Phase 6`, `Phase 7`

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

Status: `In Progress`

What this phase covers:

- dry-run and execute launch flows
- recording launch result in session state
- a cleaner launch module boundary

What is done:

- `launch` subcommand exists
- dry-run prints launch target, resume path, command, and instructions
- `--execute` runs the prepared command
- launch success and failure state are recorded

What is still open:

- move launch execution out of `cli.py` into `launcher.py`
- finalize the long-term configuration model for launch templates

### Phase 5: Adapter Boundaries

Status: `Not Started`

What this phase covers:

- turning basic agent profiles into a cleaner adapter interface
- keeping provider-specific behavior out of the session core

Current note:

- `agents.py` exists and provides a useful starting point, but the adapter contract is not finished yet

### Phase 6: Lightweight Capture Hooks

Status: `Not Started`

What this phase covers:

- faster checkpoint ergonomics
- optional autosave helpers
- cleaner pause or prepare flows before handoff

### Phase 7: Validation and Demo Flow

Status: `Not Started`

What this phase covers:

- a reproducible demo walkthrough
- end-to-end validation in a real repository
- integration-style confidence beyond unit tests

## What You Can Use Right Now

The CLI already supports:

- `start`
- `checkpoint`
- `failover`
- `launch`
- `inspect`

That means the local-first skeleton is real and usable today, even though the remaining refactor and hardening phases are still in progress.

## Recommended Next Step

The next engineering step is:

1. extract launch execution into `launcher.py`
2. keep the CLI as a thin orchestration layer
3. finalize the launch configuration boundary before Phase 5

That closes out Phase 4 cleanly before moving on to adapter boundaries and capture hooks.
