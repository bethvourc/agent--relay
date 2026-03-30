# Agent Relay

Agent Relay is a local-first CLI for handing work from one coding agent to another without losing context, decisions, or validation state.

It is built for the moment when one agent needs to stop because of rate limits, tool limits, or a manual handoff. Agent Relay captures a structured checkpoint, renders an immutable resume packet, and records the launch/resume flow in a repo-local session journal.

Built-in agent adapters currently support `Claude Code` and `Codex`.

## Why use it

- keep one durable session history across multiple agent handoffs
- capture checkpoints with decisions, blockers, touched files, and validation state
- render agent-specific resume packets from the latest checkpoint
- preview or execute a prepared launch command
- recover and inspect session state from on-disk journal data

## Installation

```bash
pip install agent-relay-tool
```

Python 3.11 or newer is required.

## Quick start

Run the CLI inside the repository whose work you want to hand off:

```bash
cd /path/to/your/repo

# One-command relay: hand off to another agent
agent-relay codex --task "Continue the release prep"

# Turn-based conversation between agents
agent-relay chat c x "Fix the failing tests"

# Concurrent agents working simultaneously (tmux)
agent-relay race c x "Build the auth module"

# See what agents are available
agent-relay discover

# View sessions
agent-relay status
```

Agent aliases: `c` = Claude, `x` = Codex. Use `agent-relay discover` to see all available agents and aliases.

## Commands

- `agent-relay <agent>`: relay to an agent (e.g. `agent-relay codex`, `agent-relay claude`)
- `agent-relay chat <agents> <task>`: turn-based agent conversation
- `agent-relay race <agents> <task>`: concurrent agents with live visibility (tmux)
- `agent-relay discover`: show available agents and aliases
- `agent-relay status`: show sessions in the current repo
- `agent-relay clean`: remove all sessions

Run `agent-relay --help` for the full command list and options.

## Configuration

Capture helpers:

- `AGENT_RELAY_AUTOSAVE_GIT_TOUCHED_FILES=1`
- `AGENT_RELAY_AUTOSAVE_RESEARCH_NOTE_FILE=<path>`
- `AGENT_RELAY_AUTOSAVE_IMPLEMENTATION_NOTE_FILE=<path>`
- `AGENT_RELAY_AUTOSAVE_VALIDATION_SUMMARY_FILE=<path>`

Launch template overrides:

- `AGENT_RELAY_CLAUDE_LAUNCH_TEMPLATE`
- `AGENT_RELAY_CODEX_LAUNCH_TEMPLATE`

Available placeholders in launch templates:

- `{agent}`
- `{agent_name}`
- `{agent_cli}`
- `{repo_root}`
- `{repo_root_path}`
- `{resume_path}`
- `{resume_path_path}`

The built-in packet-aware defaults are:

- `claude`: `cd {repo_root} && claude --resume {resume_path}`
- `codex`: `cd {repo_root} && codex --resume {resume_path}`

Custom launch templates must include `{resume_path}` or `{resume_path_path}`. If a template omits the packet input, `launch --execute` refuses to run it.

## Storage model

Agent Relay writes repo-local state under `.agent-relay/`.

Each session lives under `.agent-relay/sessions/<session-id>/` and uses a journal-plus-objects layout:

- `session.json`: immutable session manifest
- `journal/`: append-only event log
- `objects/checkpoints/<checkpoint-id>/`: checkpoint manifests, summaries, repo-state captures, and related artifacts
- `objects/handoffs/<handoff-id>/`: resume packet, packet hash, and launch specification
- `objects/launches/<launch-id>/`: launch receipts and captured stdout/stderr
- `refs/head.json`: latest derived head pointer
- `derived/view.json`: current materialized session view
- `recovery/`: pending transactions, quarantine, and repair reports

This keeps the session state inspectable and vendor-independent.

## What not to publish

Do not commit or publish `.agent-relay/`. Session artifacts can contain:

- absolute local paths
- research notes and implementation notes
- validation summaries
- captured Git status, workspace patches, and untracked-file manifests
- generated resume packets
- rendered launch commands and launch-template text

Two practical rules:

- add `.agent-relay/` to `.gitignore`
- do not put secrets or tokens into `AGENT_RELAY_CLAUDE_LAUNCH_TEMPLATE` or `AGENT_RELAY_CODEX_LAUNCH_TEMPLATE`, because the rendered command and template are recorded in handoff metadata
