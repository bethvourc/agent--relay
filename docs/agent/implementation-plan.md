# Agent Relay End-to-End Implementation Plan

## Purpose

This document turns the v1 design into a concrete implementation sequence for a working Claude Code <-> Codex failover. The goal is not just to store handoff metadata. The goal is to prove that one agent can stop, Agent Relay can prepare the session, and the next agent can be launched with enough context to continue productively.

## Definition of Done for V1

V1 is done when the following flow works reliably in a local repository:

1. Start a session with either Claude Code or Codex as the active agent.
2. Record meaningful checkpoints as the task progresses.
3. Prepare a failover to the other agent with a structured resume packet.
4. Produce a launch-ready command and launch instructions for the target agent.
5. Resume work in the target agent using the same session state.
6. Continue checkpointing in the same session after handoff.
7. Preserve a usable audit trail across the full session.

V1 does not need deep automatic capture of every vendor event. It does need a clean manual and semi-automated path that proves the workflow.

## Current Baseline

The repository already contains the following working pieces:

- repo-local session creation
- checkpoint updates
- handoff preparation
- target-specific resume packet rendering
- launch command templating per agent profile
- tests for basic Claude and Codex handoff preparation

This means the implementation plan should focus on closing the gap between `handoff prepared` and `handoff executed end to end`.

## End-to-End User Journey

The first production-worthy path should work the same way whether Claude Code or Codex is the source agent:

1. User enters a repository and starts a session.
2. Agent Relay creates `.agent-relay/sessions/<id>/state.json`.
3. The active agent works on the task.
4. The user or wrapper records checkpoints with next action, touched files, and blockers.
5. The active agent hits a limit or the user decides to switch.
6. Agent Relay prepares the target resume packet and records launch metadata.
7. Agent Relay resolves the target agent adapter and launch template.
8. Agent Relay launches the next agent in the same repository context.
9. The new agent reads the resume packet and continues.
10. The new agent updates the same session with new checkpoints.

The implementation sequence below is designed around making that flow real in the smallest number of steps.

## Phase 1: Harden the Session Model

### Goal

Make the session state stable enough that the rest of the system can depend on it.

### Tasks

- Move raw session dictionaries into typed models.
- Separate session state, checkpoint state, and handoff records.
- Define a consistent schema version strategy.
- Add validation when reading and writing state files.
- Normalize enum-like fields such as status, workstream kind, and validation status.

### Deliverables

- `models.py` for session and handoff records
- `storage.py` for load and save operations
- migration-safe schema version checks

### Acceptance Criteria

- Invalid state files fail with clear errors.
- Session writes are centralized in one module.
- Tests cover load, save, and schema validation behavior.

## Phase 2: Make Checkpoints First-Class

### Goal

Turn checkpointing from a light session update into a durable operational snapshot.

### Tasks

- Add explicit checkpoint records under each session.
- Capture the exact next action, current status, and validation state at every checkpoint.
- Allow checkpoint notes for both research and implementation work.
- Add optional command log and patch artifact references.
- Add a human-readable `summary.md` that mirrors the latest checkpoint.

### Deliverables

- `checkpoints/<timestamp>.json`
- latest summary renderer
- checkpoint creation command or helper

### Acceptance Criteria

- Every failover can point to a concrete latest checkpoint.
- The latest checkpoint can be inspected without reading raw JSON by hand.
- Tests verify checkpoint creation and latest-summary rendering.

## Phase 3: Strengthen Resume Packets

### Goal

Make the resume packet strong enough that the next agent can continue without re-planning from scratch.

### Tasks

- Split resume rendering into a dedicated module.
- Keep target-specific packet structure for Claude Code and Codex.
- Include latest checkpoint, recent decisions, blockers, touched files, and validation state.
- Include recent handoff history and failover reason.
- Add optional transcript excerpts and command summaries behind flags or config.

### Deliverables

- `resume.py`
- target-specific packet templates
- config flags for optional evidence depth

### Acceptance Criteria

- Claude and Codex packets are visibly different and optimized for their respective workflows.
- Packets stay concise while preserving the current operational state.
- Tests verify key packet sections for both agents.

## Phase 4: Implement Launch Execution

### Goal

Move from `launch-ready metadata` to an actual executable handoff path.

### Tasks

- Create a launch executor module.
- Decide whether launch configuration is per machine, per repo, or both.
- Support a dry-run mode and a real execution mode.
- Capture launch start time, exit status, and failure reason.
- Record the exact launch command used.

### Deliverables

- `launcher.py`
- `agent-relay launch <session>` or `agent-relay failover --launch`
- launch result recording in session state

### Acceptance Criteria

- A prepared handoff can be launched from Agent Relay in either direction between Claude Code and Codex.
- Launch failures are recorded without corrupting the session.
- Dry-run mode shows the exact command and resolved placeholders.

## Phase 5: Add Adapter Boundaries

### Goal

Prevent provider-specific logic from leaking into the core session store.

### Tasks

- Expand `agents.py` into a clearer adapter interface.
- Define a stable adapter contract for:
  - display name
  - launch template
  - resume packet target
  - future event capture hooks
- Keep Claude Code and Codex adapters minimal but explicit.
- Allow environment override templates without breaking defaults.

### Deliverables

- adapter contract
- agent registry
- clearer separation between profile data and executable behavior

### Acceptance Criteria

- New agents can be added without editing core session logic.
- Launch behavior and prompt rendering stay cleanly separable.

## Phase 6: Add Lightweight Capture Hooks

### Goal

Reduce the chance of losing state right before a handoff.

### Tasks

- Add an explicit `checkpoint` workflow that is fast enough to use often.
- Add optional autosave helpers for:
  - latest notes
  - touched files
  - validation summary
- Decide whether shell-command capture belongs in v1 or v1.1.
- Add a manual `pause` or `prepare` command that writes the cleanest possible final checkpoint before failover.

### Deliverables

- improved checkpoint ergonomics
- session update helpers
- optional autosave configuration

### Acceptance Criteria

- A user can capture meaningful session state in seconds.
- The failover path does not depend on perfect memory or a perfect final prompt.

## Phase 7: Validation and Demo Flow

### Goal

Prove that Agent Relay works in a real repository and not just in isolated unit tests.

### Tasks

- Create a demo script or reproducible manual walkthrough.
- Run the same task through Claude Code, then Codex, in one session.
- Validate that the second agent can continue from the recorded next action.
- Validate that the session history remains coherent after multiple handoffs.
- Add regression tests around state persistence and launch metadata.

### Deliverables

- demo workflow document
- integration tests where feasible
- release checklist for the local-first MVP

### Acceptance Criteria

- A new user can follow the demo and reproduce failover in both directions.
- The second agent does not require transcript archaeology to continue.

## Recommended Build Order

The fastest path to end to end is:

1. typed session models and centralized storage
2. explicit checkpoints and latest summary rendering
3. stronger resume packet rendering
4. launch executor with dry-run support
5. real launch path for Codex and Claude Code
6. lightweight checkpoint ergonomics
7. demo and integration validation

This order matters because launching the next agent is only valuable if the session state and resume packet are already reliable.

## Immediate Implementation Backlog

These are the next concrete tickets I would execute in order.

### Ticket 1

Refactor session state into `models.py` and `storage.py`.

Why first:

- it reduces future churn
- it gives every later phase a stable foundation

### Ticket 2

Implement explicit checkpoint artifacts and a latest-summary renderer.

Why second:

- the system needs durable checkpoint history before we automate launch

### Ticket 3

Extract resume rendering into its own module and add structured tests for Claude and Codex packets.

Why third:

- handoff quality depends more on packet quality than on launch automation

### Ticket 4

Add `launch` execution with dry-run mode and launch-result recording.

Why fourth:

- this is the first point where the product becomes truly end to end

### Ticket 5

Run real Claude-to-Codex and Codex-to-Claude demos in a sample repo and close the gaps surfaced by those runs.

Why fifth:

- the first real workflow will expose what unit tests miss

## Risks

### Risk 1

Vendor launch flows may differ more than expected.

Mitigation:

- keep adapters thin
- keep launch behavior templated
- treat deep integration as a later layer

### Risk 2

Resume packets may become too verbose.

Mitigation:

- keep a layered summary model
- default to concise operational context
- make deeper evidence opt-in

### Risk 3

Users may forget to checkpoint before failover.

Mitigation:

- make checkpointing fast
- add a `prepare` flow that captures the final operational state before handoff

## Suggested Next Milestone

The next milestone should be `Milestone 1: Reliable Session Core`.

Scope:

- typed models
- storage module
- checkpoint artifacts
- latest summary rendering

Why this milestone:

- it closes the main structural gap in the current implementation
- it keeps the next phase focused on real handoff quality instead of scattered dict updates
