# Agent Relay Execution Plan

## Purpose

This document is the implementation-grade plan for Agent Relay. It is written so a fresh coding agent can begin work without needing to reconstruct product intent from prior conversation.

It answers four questions:

1. What exactly are we building?
2. In what order should it be built?
3. Why are we choosing this language and structure?
4. What must be true for each milestone to be considered complete?

## Short Answer

The current high-level design is enough to explain the product, but not enough to reliably hand to a fresh agent for implementation without extra steering.

This execution plan is intended to be the missing layer. A new Codex session should be able to start from:

- [v1-design.md](/Users/bethvour/projects/agent-relay/docs/developer/v1-design.md)
- [implementation-plan.md](/Users/bethvour/projects/agent-relay/docs/agent/implementation-plan.md)
- [execution-plan.md](/Users/bethvour/projects/agent-relay/docs/agent/execution-plan.md)
- [milestone-1-reliable-session-core.md](/Users/bethvour/projects/agent-relay/docs/agent/milestones/milestone-1-reliable-session-core.md)

## Product Definition

Agent Relay is a local-first CLI that preserves operational continuity across coding agents.

The first supported failover pair is:

- `Claude Code -> Codex`
- `Codex -> Claude Code`

The product must support both:

- implementation workflows
- research and planning workflows

The product does not attempt to transfer hidden model state. It transfers durable working state:

- objective
- current status
- exact next action
- durable session memory captured as structured notes, summaries, and handoff history
- decisions already made
- blockers and risks
- validation state
- touched files
- recent handoffs
- supporting artifacts such as summaries, notes, command logs, and patches

## Language Decision

### Recommendation

Build v1 in Python.

### Why Python Is The Better Choice Right Now

- the project already exists in Python
- this is a CLI and local orchestration tool, not a high-throughput service
- most of the work is file I/O, JSON/Markdown rendering, subprocess execution, and config handling
- Python is faster for iteration while the protocol is still changing
- shell integration is straightforward
- testing the initial workflow is simpler

### When Go Would Be Better

Go becomes more compelling if the product shifts toward:

- a long-running daemon
- multi-process orchestration with high concurrency
- a distributed sync service
- one-file static binary distribution as a top priority
- plugin isolation and stronger runtime guarantees

### Decision

Do not switch to Go for v1.

Revisit the language decision only after:

- the handoff protocol is stable
- the launch flow is proven
- the local-first CLI has been used enough to expose scaling pain

## End-to-End Scope for V1

V1 is complete when a user can do the following in a repository:

1. Start a session under either Claude Code or Codex.
2. Record checkpoints during work.
3. Prepare a failover to the other agent.
4. Launch the target agent using Agent Relay output.
5. Continue work in the target agent using the same session.
6. Preserve the combined session history under `.agent-relay/`.

V1 does not require:

- automatic transcript ingestion from every provider
- cloud sync
- team collaboration
- IDE integrations
- perfect provider abstraction

## System Boundaries

### In Scope

- local session state
- checkpoint artifacts
- summary rendering
- handoff preparation
- target-specific resume packets
- launch metadata
- launch execution or launch-ready command generation
- minimal adapter layer for Claude Code and Codex

### Out of Scope

- cloud sync backend
- event streaming infrastructure
- browser tooling
- provider SDK integrations if local CLI launch is enough
- generalized support for every agent vendor

## Proposed Repository Structure

This is the target Python layout for v1 after the refactor.

```text
src/agent_relay/
  __init__.py
  cli.py
  agents.py
  models.py
  storage.py
  checkpoints.py
  resume.py
  summary.py
  launcher.py
  paths.py
  constants.py

tests/
  test_models.py
  test_storage.py
  test_checkpoints.py
  test_resume.py
  test_launcher.py
  test_cli.py

docs/
  README.md
  developer/
    v1-design.md
    roadmap-status.md
  examples/
    demo-walkthrough.md
  agent/
    implementation-plan.md
    execution-plan.md
    milestones/
      milestone-1-reliable-session-core.md
```

Not every file must exist immediately, but this should be the guiding structure.

## State Model

The current dict-based session handling should be replaced with typed domain models.

Minimum models for v1:

- `SessionState`
- `ValidationState`
- `HandoffRecord`
- `CheckpointRecord`

Required fields:

### SessionState

- `schema_version`
- `session_id`
- `repo_root`
- `objective`
- `workstream_kind`
- `current_agent`
- `current_status`
- `created_at`
- `updated_at`
- `next_action`
- `decisions`
- `blockers`
- `research_notes`
- `implementation_notes`
- `touched_files`
- `validation`
- `handoffs`
- `latest_checkpoint_id`

### CheckpointRecord

- `checkpoint_id`
- `session_id`
- `created_at`
- `status`
- `next_action`
- `decisions`
- `blockers`
- `research_notes`
- `implementation_notes`
- `touched_files`
- `validation`
- `artifacts`

### HandoffRecord

- `from_agent`
- `to_agent`
- `reason`
- `prepared_at`
- `checkpoint_id`
- `resume_packet_path`
- `launch_status`
- `launch_profile`
- `launch_cwd`
- `launch_command`
- `launch_template`
- `launch_template_source`
- `launch_instructions`

## Filesystem Layout

Target layout under a repo:

```text
.agent-relay/
  sessions/
    <session-id>/
      state.json
      summary.md
      checkpoints/
        <checkpoint-id>.json
      resume/
        claude.md
        codex.md
      artifacts/
        commands.ndjson
        notes/
        patches/
```

Rules:

- `state.json` contains the latest canonical session state
- `checkpoints/` stores durable snapshots
- `summary.md` mirrors the latest checkpoint for humans
- `resume/` stores target-specific handoff packets
- `artifacts/` stores evidence, not canonical state

## Module Responsibilities

### `models.py`

Owns:

- typed state models
- serialization and deserialization
- validation of required fields
- schema version checks

Must not own:

- CLI parsing
- filesystem writes
- subprocess launch

### `storage.py`

Owns:

- session path resolution
- atomic reads and writes
- session initialization
- session load and save
- checkpoint file persistence

Must not own:

- Markdown rendering
- agent-specific logic

### `checkpoints.py`

Owns:

- checkpoint creation
- deriving a checkpoint from current session state
- computing `latest_checkpoint_id`
- optional artifact references

### `summary.py`

Owns:

- rendering `summary.md`
- producing concise human-readable session views

### `resume.py`

Owns:

- Claude-specific resume packet rendering
- Codex-specific resume packet rendering
- resume packet assembly from state plus latest checkpoint

### `agents.py`

Owns:

- supported agent registry
- profile metadata
- launch template resolution
- adapter-facing configuration

### `launcher.py`

Owns:

- dry-run launch resolution
- subprocess invocation
- launch result capture
- writing launch outcomes back to state

### `cli.py`

Owns:

- command-line entry points
- argument parsing
- composition of domain modules

Must not contain:

- complex business logic
- hand-built state mutation logic

## CLI Roadmap

### Commands Required For V1

- `agent-relay start`
- `agent-relay checkpoint`
- `agent-relay inspect`
- `agent-relay failover`
- `agent-relay launch`

### Suggested Command Semantics

#### `agent-relay start`

Creates a new session and initializes repo-local storage.

#### `agent-relay checkpoint`

Creates a durable checkpoint artifact and updates canonical session state.

#### `agent-relay inspect`

Displays current session state and optionally latest checkpoint details.

#### `agent-relay failover`

Prepares a handoff packet and handoff record for a target agent.

#### `agent-relay launch`

Launches the selected target agent from the most recent prepared handoff.

## Milestones

### Milestone 1: Reliable Session Core

Scope:

- `models.py`
- `storage.py`
- checkpoint artifacts
- latest summary rendering

Close Criteria:

- session state is no longer managed as raw dict mutation inside `cli.py`
- checkpoints are durable files, not just overwritten fields
- `summary.md` is generated from the latest checkpoint
- tests cover session creation, load/save, checkpoint creation, and summary rendering

### Milestone 2: Strong Handoff Packets

Scope:

- `resume.py`
- packet rendering from latest checkpoint
- agent-specific packet layouts

Close Criteria:

- failover output contains enough context for continuation without transcript archaeology
- Claude and Codex packets are distinct and tested

### Milestone 3: Launch Execution

Scope:

- `launcher.py`
- dry-run and execution modes
- recording launch result status

Close Criteria:

- a prepared handoff can be launched from Agent Relay
- launch failures are recorded clearly

### Milestone 4: End-to-End Validation

Scope:

- real demo workflow
- integration checks
- regression suite updates

Close Criteria:

- a user can reproduce both Claude-to-Codex and Codex-to-Claude failover from docs alone

## Execution Order

Do the work in this order:

1. `models.py`
2. `storage.py`
3. `checkpoints.py`
4. `summary.py`
5. refactor `cli.py` to use those modules
6. `resume.py`
7. expand `agents.py`
8. `launcher.py`
9. end-to-end validation

Do not start launch automation before Milestone 1 is solid. Launching an agent with weak state is worse than not launching at all.

## What A Fresh Codex Session Should Be Told To Do

If starting a new Codex session from these docs, the implementation prompt should be:

1. Read the design docs first.
2. Implement only Milestone 1.
3. Do not begin launch automation yet.
4. Refactor the current code into `models.py`, `storage.py`, `checkpoints.py`, and `summary.py`.
5. Update `cli.py` to use those modules.
6. Add tests for the new state model and checkpoint behavior.

That is tight enough to begin productive work without reopening the full product debate.

## Decision On Risk Handling

The risk `vendor launch flows may differ more than expected` should not block Milestone 1.

Decision:

- keep that risk deferred until after the session core is complete
- do not expand provider automation during Milestone 1
- preserve agent launch behavior as simple templates until the core state is proven

This is the correct sequence because the launch path depends on reliable session data, not the other way around.
