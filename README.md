# Agent Relay

Agent Relay is a local-first CLI for handing work from one coding agent to another without losing operational context.

The core idea is simple: when an agent stops because of rate limits, tooling limits, or a manual pause, Agent Relay captures a structured checkpoint, prepares a resume packet, and hands the session to a new agent.

This initial scaffold includes:

- a docs index in `docs/README.md`
- a developer-facing design doc in `docs/developer/v1-design.md`
- a developer-facing progress tracker in `docs/developer/roadmap-status.md`
- a runnable demo walkthrough in `docs/examples/demo-walkthrough.md`
- an end-to-end implementation plan in `docs/agent/implementation-plan.md`
- an execution-grade plan in `docs/agent/execution-plan.md`
- a milestone spec in `docs/agent/milestones/milestone-1-reliable-session-core.md`
- a minimal Python CLI package in `src/agent_relay`
- basic tests for local session creation and handoff preparation
- built-in `Claude Code` and `Codex` agent profiles for handoff rendering
- launch command templating recorded in handoff metadata

The project is intentionally local-first. Session data lives in a repo-local `.agent-relay/` directory so state is inspectable, durable, and independent from any single model vendor.

## Current CLI Spine

The CLI currently supports:

- `start` to create a new session under `.agent-relay/sessions/<session-id>/`
- `checkpoint` to update the structured session state and write a new checkpoint
- `failover` to render a target-specific resume packet and prepare handoff metadata
- `launch` to print or execute the latest prepared handoff
- `inspect` to print the persisted session state

`start` now creates the initial `state.json`, an initial checkpoint under `checkpoints/`, and `summary.md`. Every later `checkpoint` updates the canonical session state, writes a new append-only checkpoint record, and refreshes `summary.md`.

Resume rendering now lives in `src/agent_relay/resume.py`. `failover` accepts `--resume-evidence-depth` with `minimal`, `standard`, or `full` to control how much latest-checkpoint evidence appears in the target packet.
Launch execution now lives in `src/agent_relay/launcher.py`, while `cli.py` stays focused on command orchestration.

Failover now records a rendered launch command for the target agent profile. The built-in defaults are intentionally shallow:

- `claude` launches with `cd {repo_root} && claude`
- `codex` launches with `cd {repo_root} && codex`

`agent-relay launch <session>` prints the latest prepared resume path, launch command, and launch instructions without mutating session state. Add `--execute` to actually run the command and record whether launch succeeded or failed.

If your local agent CLI supports a richer invocation flow, override the launch template with environment variables:

- `AGENT_RELAY_CLAUDE_LAUNCH_TEMPLATE`
- `AGENT_RELAY_CODEX_LAUNCH_TEMPLATE`

Available placeholders in those templates:

- `{agent}`
- `{agent_name}`
- `{agent_cli}`
- `{repo_root}`
- `{repo_root_path}`
- `{resume_path}`
- `{resume_path_path}`

## Session Layout

Each session lives under `.agent-relay/sessions/<session-id>/` with:

- `state.json` as the canonical typed session state
- `checkpoints/` as append-only operational snapshots
- `summary.md` as the latest human-readable summary
- `resume/` for target-specific handoff packets
- `artifacts/` for supporting evidence
