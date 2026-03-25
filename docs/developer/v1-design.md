# Agent Relay V1 Design

## Problem

Coding agents can make progress inside a repository, but their working state is fragmented across vendor-specific chat history, terminal outputs, diffs, and unstated assumptions. When an agent hits a rate limit or context limit, the next agent usually starts from a lossy summary.

Agent Relay is a handoff layer for coding and research workflows. It captures durable operational state, preserves session memory in explicit artifacts, prepares a structured continuation packet, and lets a different agent continue work with minimal loss.

The design target is operational continuity, not hidden-state transfer. We can preserve what the agent did, decided, observed, and planned. We cannot transfer proprietary internal reasoning state.

## V1 Goals

- Support `Claude Code` and `Codex` as the first two agents, with handoff in either direction.
- Store handoff state locally in the target repository.
- Capture enough state for both research and implementation workflows.
- Prepare a target-specific resume packet for the next agent.
- Model handoffs as a unified session history rather than isolated prompts.
- Keep the initial implementation simple enough to validate the workflow quickly.

## V1 Non-Goals

- Perfect transfer of hidden model state.
- Deep provider-specific automation for every agent.
- Team collaboration or cloud sync.
- IDE plugins or browser integrations.
- Background daemons that require long-lived services.

## Recommendation Summary

Package the product as an installable CLI. The CLI writes repo-local session state into `.agent-relay/`. Optional cloud sync can be added later, but it should not be the foundation of v1.

This split gives us:

- a reusable install surface
- inspectable repo-local state
- easy local debugging
- vendor-independent handoff artifacts

## Core Concepts

### Session

A session is the long-lived unit of work across one or more agents. A session can include research, implementation, validation, and multiple failovers.

### Checkpoint

A checkpoint is a durable snapshot of the session at a specific point in time. It contains structured state plus the evidence needed to justify that state.

### Handoff

A handoff is a checkpoint prepared for a different target agent. It includes a rendered resume packet that tells the next agent what happened, where to start, and what constraints apply.

### Adapter

An adapter is the provider-specific layer for launching an agent, shaping its resume prompt, and later capturing session events from that agent.

## Architecture

V1 should be divided into four layers.

### 1. Session Store

The session store is the durable local state in the target repository.

Suggested layout:

```text
.agent-relay/
  sessions/
    <session-id>/
      state.json
      summary.md
      resume/
        claude.md
        codex.md
      artifacts/
        commands.ndjson
        logs/
        patches/
```

### 2. Core CLI

The CLI owns session lifecycle actions such as:

- start a session
- checkpoint a session
- inspect a session
- prepare a failover
- launch the next agent

The CLI should be the control plane. Agents should not directly orchestrate each other.

### 3. Resume Renderer

The renderer turns structured state into target-specific continuation packets. The rendered packet should be concise, explicit, and optimized for the target agent's workflow style.

The best resume packet is layered:

1. concise objective and current status
2. exact stopping point and next action
3. key decisions, blockers, and risks
4. touched files, patches, tests, and supporting evidence

### 4. Agent Adapters

Adapters encapsulate vendor-specific behavior. In v1, they can start shallow:

- identify the agent name and profile
- render a launch command
- render a target-specific resume prompt

Later they can grow into:

- direct process launch
- session event capture
- automated checkpoint hooks
- rate-limit detection

## Data Model

V1 should model both coding and research in the same session schema.

Suggested top-level fields for `state.json`:

```json
{
  "schema_version": 1,
  "session_id": "20260324-120000-abc123",
  "repo_root": "/path/to/repo",
  "objective": "Move a coding task between Claude Code and Codex without losing state",
  "workstream_kind": "mixed",
  "current_agent": "claude",
  "current_status": "active",
  "created_at": "2026-03-24T12:00:00Z",
  "updated_at": "2026-03-24T12:00:00Z",
  "next_action": "Draft the checkpoint schema and failover flow",
  "decisions": [],
  "blockers": [],
  "research_notes": [],
  "implementation_notes": [],
  "touched_files": [],
  "validation": {
    "status": "not_run",
    "summary": ""
  },
  "handoffs": []
}
```

### Why This Schema

- `objective` keeps the new agent aligned with the original task.
- `workstream_kind` distinguishes research, implementation, or mixed work.
- `next_action` makes continuation concrete instead of abstract.
- `decisions` and `blockers` preserve why the work looks the way it does.
- `validation` prevents the next agent from assuming work is already verified.
- `handoffs` gives us an audit trail across agents.

## Local-First vs Cloud

V1 should be local-first.

Reasons:

- the repo is already local
- patches and command outputs are easiest to capture locally
- privacy and secret handling are simpler
- offline usage is possible
- debugging is much easier early on

Cloud sync becomes useful after the core workflow is proven. It should be an optional backend, not the default architecture.

## Resume Packet Strategy

The target agent should not receive a raw transcript dump by default. The best handoff includes:

- distilled operational summary
- exact next action
- structured metadata
- supporting artifacts
- selectively included transcript or command excerpts when needed

This gives the new agent the best context density per token.

## Bidirectional Claude Code <-> Codex Failover Flow

The first end-to-end support target is the Claude Code and Codex pair, in either direction.

1. User starts a session in a repository with either Claude or Codex as the active agent.
2. Agent Relay creates a session directory and initial `state.json`.
3. As work progresses, the session accumulates checkpoints and artifacts.
4. The active agent hits a rate limit or the user decides to hand off.
5. Agent Relay captures the latest durable checkpoint.
6. Agent Relay renders the target-specific resume packet from the latest state.
7. Agent Relay records a handoff entry with source agent, target agent, time, and reason.
8. Agent Relay launches the target agent with the rendered resume packet and the same repository context.
9. The target agent continues work and writes new checkpoints into the same session.

The handoff record should include:

- `from_agent`
- `to_agent`
- `reason`
- `prepared_at`
- `resume_packet_path`
- `launch_status`

## Orchestration Boundary

Launching the next agent is part of the product goal, but deep provider automation should not be hard-coded into the session store.

V1 orchestration should be simple:

- map an agent profile to a launch command template
- provide the repo path and resume packet path
- record success or failure

This keeps the launch layer replaceable while letting the product validate the handoff workflow.

## Security and Redaction

Local-first does not remove security risk. Handoff artifacts can leak secrets if raw terminal outputs or environment values are written indiscriminately.

V1 should include at least:

- secret-aware artifact filters
- environment variable allowlists
- an option to omit raw command outputs
- explicit markers for redacted values

## Initial Build Plan

The safest first implementation slice is:

1. Local session creation
2. Checkpoint file creation
3. Resume packet rendering
4. Handoff record creation
5. Basic launch command templating

That gives us a testable spine before deeper provider integration.

## Open Questions

- Should launch commands be defined globally, per machine, or per repository?
- Do we want automatic checkpointing in v1 or only explicit checkpoints?
- Should resume packets be pure Markdown, JSON plus Markdown, or both?
- How much raw transcript content should be preserved by default?
- What is the cleanest adapter surface for Claude Code and Codex process invocation?
