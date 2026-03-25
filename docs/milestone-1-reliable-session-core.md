# Milestone 1: Reliable Session Core

## Goal

Replace ad hoc session mutation with a durable, testable session core that future handoff and launch features can trust.

This milestone is intentionally limited to:

- typed models
- storage module
- checkpoint artifacts
- latest summary rendering

This milestone must be finished before deeper launch automation.

## Why This Milestone Comes First

The current implementation already prepares handoffs, but it still relies on direct dict mutation inside the CLI. That is fine for an early spike, but it is not stable enough for a product that needs durable state across multiple agents.

Without this milestone:

- session state can drift
- failovers are harder to trust
- adding launch execution will compound technical debt
- testing remains too shallow

## Deliverables

### 1. `models.py`

Create typed dataclass models for:

- `ValidationState`
- `HandoffRecord`
- `CheckpointRecord`
- `SessionState`

Each model must support:

- `from_dict`
- `to_dict`
- validation of required fields

### 2. `storage.py`

Create a storage layer that owns:

- repo path resolution
- session directory creation
- session load
- session save
- checkpoint save
- latest summary write

Prefer atomic writes where practical.

### 3. `checkpoints.py`

Create a checkpoint module that:

- derives a `CheckpointRecord` from `SessionState`
- assigns checkpoint ids
- writes checkpoint files under `checkpoints/`
- updates the session’s `latest_checkpoint_id`

### 4. `summary.py`

Create a summary renderer that writes `summary.md` from the latest checkpoint and current session state.

The summary must include:

- objective
- current agent
- current status
- next action
- validation state
- recent decisions
- blockers
- touched files
- latest checkpoint id

### 5. `cli.py` Refactor

Refactor the CLI so it:

- loads typed state
- calls storage and checkpoint helpers
- stops mutating raw dicts directly

## Target File Changes

### New Files

- `src/agent_relay/models.py`
- `src/agent_relay/storage.py`
- `src/agent_relay/checkpoints.py`
- `src/agent_relay/summary.py`
- `tests/test_models.py`
- `tests/test_storage.py`
- `tests/test_checkpoints.py`

### Existing Files To Update

- `src/agent_relay/cli.py`
- `tests/test_cli.py`

## Proposed Data Structures

### `ValidationState`

Fields:

- `status: str`
- `summary: str`

Constraints:

- allowed statuses: `not_run`, `passed`, `failed`, `partial`

### `HandoffRecord`

Fields:

- `from_agent: str`
- `to_agent: str`
- `reason: str`
- `prepared_at: str`
- `checkpoint_id: str`
- `resume_packet_path: str`
- `launch_status: str`
- `launch_profile: str`
- `launch_cwd: str`
- `launch_command: str`
- `launch_template: str`
- `launch_template_source: str`
- `launch_instructions: str`

### `CheckpointRecord`

Fields:

- `checkpoint_id: str`
- `session_id: str`
- `created_at: str`
- `status: str`
- `next_action: str`
- `decisions: list[str]`
- `blockers: list[str]`
- `research_notes: list[str]`
- `implementation_notes: list[str]`
- `touched_files: list[str]`
- `validation: ValidationState`
- `artifacts: dict[str, str | list[str]]`

### `SessionState`

Fields:

- `schema_version: int`
- `session_id: str`
- `repo_root: str`
- `objective: str`
- `workstream_kind: str`
- `current_agent: str`
- `current_status: str`
- `created_at: str`
- `updated_at: str`
- `next_action: str`
- `decisions: list[str]`
- `blockers: list[str]`
- `research_notes: list[str]`
- `implementation_notes: list[str]`
- `touched_files: list[str]`
- `validation: ValidationState`
- `handoffs: list[HandoffRecord]`
- `latest_checkpoint_id: str | None`

## Filesystem Contract

For a session `<id>`, Milestone 1 must produce:

```text
.agent-relay/
  sessions/
    <id>/
      state.json
      summary.md
      checkpoints/
        <checkpoint-id>.json
      resume/
      artifacts/
```

Rules:

- `state.json` is canonical current state
- each checkpoint is append-only
- `summary.md` reflects the latest checkpoint

## Command Behavior Changes

### `start`

Must:

- create a typed `SessionState`
- save `state.json`
- create an initial checkpoint
- write `summary.md`

### `checkpoint`

Must:

- update the session state
- create a new checkpoint file
- refresh `summary.md`

### `failover`

For Milestone 1, `failover` should still work, but it should now rely on the typed storage layer and latest checkpoint id.

It does not need launch execution yet.

## Acceptance Tests

### `test_models.py`

Must verify:

- valid model serialization round-trips
- invalid statuses fail clearly
- missing required fields fail clearly

### `test_storage.py`

Must verify:

- session directories are created correctly
- saving and loading state preserves content
- checkpoint files are written in the right location

### `test_checkpoints.py`

Must verify:

- checkpoint ids are generated
- checkpoint creation updates `latest_checkpoint_id`
- checkpoint content mirrors current session state

### `test_cli.py`

Must verify:

- `start` creates initial state, initial checkpoint, and summary
- `checkpoint` creates a second checkpoint and updates summary
- `failover` still works on top of the new storage model

## Implementation Sequence

Implement in this exact order:

1. `models.py`
2. `storage.py`
3. `checkpoints.py`
4. `summary.py`
5. refactor `cli.py`
6. add and update tests

Do not start with `cli.py`. Start by building the underlying domain objects and storage contract.

## Close Criteria

Milestone 1 is complete only when all of the following are true:

- `cli.py` no longer performs raw session dict mutation
- starting a session creates an initial checkpoint automatically
- checkpointing appends a new checkpoint artifact
- `summary.md` is always aligned with the latest checkpoint
- the relevant tests pass
- failover still works after the refactor

## Hand-Off Prompt For A Fresh Coding Agent

If a new agent is taking over implementation for this milestone, the prompt should be:

Implement Milestone 1 from `docs/milestone-1-reliable-session-core.md`. Create typed models and centralized storage, make checkpoints append-only artifacts, generate `summary.md` from the latest checkpoint, refactor the CLI to use those modules, and update the tests. Do not implement launch execution yet.
