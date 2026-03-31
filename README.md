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

## Best results

The best way to get the most out of Agent Relay is to start the work inside Relay instead of only using Relay after an agent stops.

- Use `agent-relay run ...`, `agent-relay chat ...`, or `agent-relay race ...` when you want Relay to manage the session live from the beginning. This gives Relay the strongest handoff because it saves the recent turns, the current plan, the next step, and any structured state it can capture while the work is happening.
- Use `agent-relay claude ...` or `agent-relay codex ...` when you want a one-command handoff to another agent. This flow prepares the packet and, unless you pass `--no-launch`, also launches the target agent.
- If an agent hits a limit outside Relay, open a new terminal in the same repo and run a one-command handoff such as `agent-relay codex --task "Claude hit its limit; continue from the current state"`.

In simple terms: if Relay was there while the work was happening, handoffs are much stronger. If Relay joins later, it can only hand off what it can still see.

## What Relay Can Recover

If Relay managed the session live, it can hand off much more than just changed files. It can carry:

- recent relay-owned conversation artifacts
- a saved summary of the current work
- the current plan and next step
- blockers and remaining work
- intended edits and proposed edits that may not be applied yet
- provider-exported session state when available

If Relay joins later, it can still hand off:

- the current working tree changes
- planning notes you give it
- proposed edits you give it
- anything the provider can export at handoff time

Relay cannot fully reconstruct work that only existed inside an unmanaged external session and was never saved or exported. In particular, it cannot reliably recover:

- private hidden reasoning
- UI-only drafts that were never saved anywhere
- proposed edits that were shown in a tool UI but never accepted, exported, or passed to Relay

## Recommended Workflows

### Start inside Relay for the strongest continuity

Use one of these when you want Relay to watch the session live:

```bash
# Single-agent managed run
agent-relay run c "Fix the failing tests"

# Turn-based handoff-friendly collaboration
agent-relay chat c x "Fix the failing tests"

# Concurrent work with tmux sessions
agent-relay race c x "Build the auth module"
```

Use `run` when one agent should stay in control from the first prompt. Use `chat` when multiple agents should take turns. Use `race` when multiple agents should work in parallel.

### Switch agents after one stops

Use the one-command handoff when one agent needs to stop and another should continue:

```bash
# Prepare the packet and launch the target agent
agent-relay codex --task "Continue the release prep"

# Only prepare the packet so you can inspect it first
agent-relay codex --task "Continue the release prep" --no-launch
```

### Preserve planning-only work

If there are no file changes yet, pass the planning and proposed-edit context explicitly:

```bash
agent-relay codex \
  --task "Continue from the saved plan" \
  --planning-note-file handoff-notes/planning.md \
  --proposed-edits-file handoff-notes/proposed.diff \
  --no-launch
```

This is the best fallback when an agent did useful planning but did not write code to disk.

## Commands

- `agent-relay <agent>`: relay to an agent and, by default, launch it (e.g. `agent-relay codex`, `agent-relay claude`)
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
- `AGENT_RELAY_AUTOSAVE_PLANNING_SNAPSHOT_FILE=<path>`
- `AGENT_RELAY_AUTOSAVE_PROPOSED_EDITS_FILE=<path>`

Launch template overrides:

- `AGENT_RELAY_CLAUDE_LAUNCH_TEMPLATE`
- `AGENT_RELAY_CODEX_LAUNCH_TEMPLATE`

Optional provider-export capture hooks:

- `AGENT_RELAY_CLAUDE_CAPTURE_TEMPLATE`
- `AGENT_RELAY_CODEX_CAPTURE_TEMPLATE`

Available placeholders in launch and capture templates:

- `{agent}`
- `{agent_name}`
- `{agent_cli}`
- `{repo_root}`
- `{repo_root_path}`
- `{resume_path}`
- `{resume_path_path}`
- `{session_id}`

The built-in packet-aware defaults are:

- `claude`: `cd {repo_root} && claude -p "$(cat {resume_path})"`
- `codex`: `cd {repo_root} && codex "$(cat {resume_path})"`

Custom launch templates must include `{resume_path}` or `{resume_path_path}`. If a template omits the packet input, `launch --execute` refuses to run it.

Capture templates are optional. If you set one, it should print JSON to stdout. Relay can use fields such as:

- `resumable_state`
- `planning_snapshot`
- `proposed_edits`
- `transcript`
- `session_metadata`
- `warnings`

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
