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
pip install agent-relay
```

Python 3.11 or newer is required.

## Quick start

Run the CLI inside the repository whose work you want to hand off:

```bash
cd /path/to/your/repo

SESSION_ID=$(agent-relay start \
  --agent claude \
  --task "Prepare the package release" \
  --repo . \
  --quiet)

agent-relay checkpoint "$SESSION_ID" \
  --next-action "Prepare a Codex handoff for packaging cleanup" \
  --decision "README and publish metadata need to be updated" \
  --capture-git-changes \
  --repo .

agent-relay prepare "$SESSION_ID" \
  --next-action "Hand off the release-prep work to Codex" \
  --validation-status partial \
  --validation-summary "Packaging and docs still need review" \
  --repo .

agent-relay failover "$SESSION_ID" \
  --to-agent codex \
  --reason "Continue release prep" \
  --repo .

agent-relay launch "$SESSION_ID" --repo .
agent-relay inspect "$SESSION_ID" --repo .
```

If you want Agent Relay to dispatch the prepared command, use `agent-relay launch <session> --execute`.

`launch --execute` only starts the target agent process. Ownership transfers when the new agent accepts the handoff with:

```bash
agent-relay resume "$SESSION_ID" --repo .
```

## Command flow

- `start`: create a new session and initial checkpoint
- `checkpoint`: record progress without changing ownership
- `pause`: write a final checkpoint and pause the session
- `prepare`: capture a clean pre-handoff checkpoint
- `failover`: render a target-specific resume packet and launch metadata
- `launch`: preview or execute the prepared launch command
- `resume`: accept a prepared handoff and transfer ownership
- `repair`: repair session integrity explicitly
- `inspect`: print the current session view
- `dashboard` or `list`: show sessions in the current repo

Run `agent-relay --help` for the top-level command list or `agent-relay <command> --help` for command-specific flags.

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
