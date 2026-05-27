# Agent Relay

[![PyPI Downloads](https://static.pepy.tech/personalized-badge/agent-relay-tool?period=total&units=INTERNATIONAL_SYSTEM&left_color=BLACK&right_color=GREEN&left_text=downloads)](https://pepy.tech/projects/agent-relay-tool)
[![Buy Me A Coffee](https://img.shields.io/badge/Buy%20Me%20A%20Coffee-support-ffdd00?logo=buymeacoffee&logoColor=black)](https://buymeacoffee.com/buildwithbeth)

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

Python 3.11 or newer is required.

**macOS / Linux**

```bash
curl -fsSL https://agent-relay.dev/install.sh | sh
```

**Windows (PowerShell)**

```powershell
irm https://agent-relay.dev/install.ps1 | iex
```

**Already using `uv` or `pipx`?**

```bash
uv tool install agent-relay-tool   # or: pipx install agent-relay-tool
```

Full per-OS walkthroughs and verification steps:
[agent-relay.dev/installation](https://agent-relay.dev/installation).

## Quick start

Run the CLI inside the repository whose work you want to hand off:

```bash
cd /path/to/your/repo

# One-command relay: hand off to another agent
agent-relay codex --task "Continue the release prep"

# Single-agent managed run (best for handoffs — see Best Practices)
agent-relay run c "Fix the failing tests"

# Turn-based conversation between agents
agent-relay chat c x "Fix the failing tests"

# Concurrent agents working simultaneously (tmux)
agent-relay race c x "Build the auth module"

# Resume the latest unresolved concurrent conflict
agent-relay resolve --latest

# Inspect saved conflict artifacts
agent-relay inspect-conflicts <session-id>

# See what agents are available
agent-relay discover

# View sessions
agent-relay status

# Live view of an in-progress session (auto-picks newest active)
agent-relay watch

# Token / cost / duration rollup for a session
agent-relay metrics
```

Agent aliases: `c` = Claude, `x` = Codex. Use `agent-relay discover` to see all available agents and aliases.

## Best practices

### Start inside Relay for the strongest handoffs

The single most important thing you can do for better handoffs is **start the work inside Relay** instead of only calling Relay after an agent stops.

When Relay manages the session from the beginning, it runs the agent in `--print` mode and captures the full output stream — including the agent's reasoning, tool calls, decisions, and structured state. This means the handoff packet contains everything the next agent needs: recent conversation history, the current plan, blockers, remaining work, intended edits, and any provider-exported session state.

When Relay joins after the fact, it can only hand off what is still observable: the working tree changes, any notes you provide, and whatever the provider can export at that moment. Private reasoning, in-progress drafts, and tool-call history from an unmanaged session are lost.

**In simple terms: if Relay was there while the work was happening, handoffs are much stronger. If Relay joins later, it can only hand off what it can still see.**

### Use `run` as the default starting point

```bash
agent-relay run c "Fix the failing tests"
```

`run` is the recommended way to start any single-agent task when you know a handoff might happen later. It:

- runs the agent with live output capture (reasoning, tool calls, decisions)
- extracts structured state from each turn (status, remaining work, blockers)
- stores full turn artifacts (prompt, output, stderr, state) for later recovery
- builds continuation context automatically so the next agent picks up exactly where this one left off

If the agent finishes the work, great. If it hits a rate limit or needs to hand off, the session already has everything Relay needs to produce a strong resume packet.

### Use `chat` for multi-agent collaboration

```bash
agent-relay chat c x "Fix the failing tests"
```

`chat` runs agents in alternating turns. Each agent sees the full conversation history — what every other agent said, decided, and proposed. Use this when agents need to build on each other's work iteratively, like one agent investigating and another implementing.

### Use `race` for parallel work with enforced delegation

```bash
agent-relay race c x "Build the auth module"
```

`race` is a phased concurrent workflow, not just two agents launched side by side. It enforces a structured process:

1. **Planning**: every agent must claim a concrete slice of work before implementation begins.
2. **Implementation**: each agent works inside its own isolated git worktree, so changes cannot collide during execution.
3. **Merge and review**: Relay only merges in-scope work back to the main repo.
4. **Conflict handling**: Relay saves conflict artifacts, can run an automatic resolver/reviewer pass, and hands off to `resolve` when human judgment is still needed.

### Fall back to one-command relay when Relay was not managing the session

```bash
agent-relay codex --task "Claude hit its limit; continue from the current state"
```

If an agent was working outside of Relay and needs to hand off, open a new terminal in the same repo and run a one-command handoff. Relay will capture what it can (git changes, any notes you provide) and generate a packet for the target agent. This is less complete than a managed session but still far better than starting from scratch.

## What Relay can recover

### When Relay managed the session (run, chat, race)

- full conversation turn history (prompts, outputs, reasoning)
- structured state from each turn (status, plan, blockers, remaining work)
- intended and proposed edits (even if not yet applied)
- provider-exported session state when available
- current working tree changes
- verification items and validation results

### When Relay joins after the fact (one-command handoff)

- current working tree changes (git diff)
- planning notes you provide via `--planning-note-file`
- proposed edits you provide via `--proposed-edits-file`
- anything the provider can export at handoff time

### What Relay cannot recover

- private hidden reasoning from an unmanaged session
- UI-only drafts that were never saved anywhere
- proposed edits shown in a tool UI but never accepted, exported, or passed to Relay

## Commands

### Primary commands

| Command                               | Description                                                                          |
| ------------------------------------- | ------------------------------------------------------------------------------------ |
| `agent-relay run <agent> <task>`      | Single-agent managed session with live capture. Best starting point for any task.    |
| `agent-relay chat <agents...> <task>` | Turn-based agent conversation. Agents alternate with full history context.           |
| `agent-relay race <agents...> <task>` | Concurrent workflow with planning, isolated worktrees, and conflict recovery (tmux). |
| `agent-relay <agent>`                 | One-command relay to a target agent. Creates packet and optionally launches.         |

### Conflict resolution

| Command                                      | Description                                                       |
| -------------------------------------------- | ----------------------------------------------------------------- |
| `agent-relay resolve [session-id]`           | Resume unresolved race conflicts.                                 |
| `agent-relay resolve --latest`               | Resume the most recent unresolved conflict.                       |
| `agent-relay inspect-conflicts <session-id>` | Inspect saved conflict artifacts, versions, and resolution hints. |

### Session management

| Command                                 | Description                                                                                                                                                                                                                                                         |
| --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `agent-relay discover`                  | Show available agents, aliases, and CLI paths.                                                                                                                                                                                                                      |
| `agent-relay status`                    | List all relay sessions in the current repo.                                                                                                                                                                                                                        |
| `agent-relay watch [session-id]`        | Live TUI of an in-progress session. Auto-picks newest active when no id is given. `--json` streams JSONL events; `--quiet` streams one terse line per event; `--no-follow` prints a single snapshot and exits. Add `--metrics` for a token / cost / duration panel. |
| `agent-relay metrics [session-id]`      | Token / cost / latency rollup for a session. Use `--all` for cross-session totals, `--since YYYY-MM-DD` to filter, `--agent claude` (repeatable) to scope.                                                                                                          |
| `agent-relay metrics-tail [session-id]` | Stream metric events as JSONL — one line per `turn_completed`, plus a final session rollup. Optional `--webhook URL` POSTs each line.                                                                                                                               |
| `agent-relay metrics-serve`             | Run a metrics exporter. `--prometheus :9464` exposes `/metrics` in Prometheus text format; `--otlp http://collector:4318/v1/metrics` pushes OTLP/HTTP-JSON every 30s. Both can run together.                                                                        |
| `agent-relay clean`                     | Remove all sessions. Use `--all` to remove entire `.agent-relay/` directory.                                                                                                                                                                                        |

### Options

| Option                | Description                                                   |
| --------------------- | ------------------------------------------------------------- |
| `--task`, `-t`        | Task for agents (alternative to positional argument)          |
| `--continue`          | Continue from a prior relay session id                        |
| `-n`                  | Max turns for `run`/`chat` (default: 10)                      |
| `--max-time`          | Max seconds for `race` (default: 600)                         |
| `--open-terminals`    | Auto-open terminal windows/tabs for `race`/`resolve` on macOS |
| `--no-open-terminals` | Disable auto-open terminal behavior                           |
| `--from`              | Source agent for relay (auto-detected by default)             |
| `--no-launch`         | Create the handoff packet without launching the target agent  |
| `--yes`, `-y`         | Skip confirmation prompt                                      |
| `--json`              | Machine-readable JSON output                                  |
| `--quiet`, `-q`       | Minimal output                                                |

## Recommended workflows

### Single-agent task with potential handoff

```bash
# Start inside Relay for the strongest handoff later
agent-relay run c "Fix the failing tests"

# If the agent hits a limit, the session is already captured.
# Hand off to another agent:
agent-relay codex --task "Continue from the saved session"
```

### Turn-based collaboration

```bash
# Two agents alternating
agent-relay chat c x "Fix the failing tests"

# Three agents, 6 turns max
agent-relay chat c x c "Review and fix" -n 6
```

### Live monitoring of a running session

While a session is in flight, open a second terminal in the same repo and run:

```bash
# Auto-pick the newest active session
agent-relay watch

# Or pin to a specific session
agent-relay watch <session-id>

# Stream events as JSONL — pipe into other tools
agent-relay watch --json | jq -c '{ts: .timestamp, kind: .kind}'

# Print a single snapshot of current state and exit
agent-relay watch --no-follow
```

The live TUI surfaces journal events, workspace activity, the current turn's
elapsed time and progressive agent output, and the latest turn state — all in
real time. The watcher exits cleanly when the session reaches a terminal
status (`completed`, `ready_for_handoff`) or on Ctrl-C.

Add `--metrics` for a token / cost / duration panel that refreshes after every
turn:

```bash
agent-relay watch --metrics
```

### Cost & performance metrics

Relay computes metrics on read — no extra files in `.agent-relay/`, no cache
invalidation. The same data backs four surfaces:

```bash
# Per-session table (auto-picks the most recent session)
agent-relay metrics

# Cross-session aggregates: by agent, by day, totals
agent-relay metrics --all
agent-relay metrics --all --since 2026-05-01 --agent claude

# Machine-readable
agent-relay metrics --json
agent-relay metrics --quiet      # one TSV line per session

# Live JSONL stream — one line per turn_completed plus a final session rollup
agent-relay metrics-tail
agent-relay metrics-tail --webhook https://hooks.example.com/relay
```

Cost is best-effort. If an agent output includes an actual `total_cost_usd`
style field, Relay uses it. Codex `exec --json` normally emits token usage but
not a billed cost, so Relay estimates Codex cost from the captured model name.
Managed Codex turns save the model from the JSON stream when available, then
fall back to `AGENT_RELAY_CODEX_MODEL`, `CODEX_MODEL`, `OPENAI_MODEL`, or
`$CODEX_HOME/config.toml`. Uncached input tokens, cached input tokens, and
output tokens are priced separately. Estimates do not include account discounts,
Batch/Flex or priority processing, data residency uplift, long-context uplift,
or separately billed tool fees.

For dashboards and long-running collection, `metrics-serve` runs an exporter:

```bash
# Prometheus pull-based scrape endpoint (stdlib only)
agent-relay metrics-serve --prometheus :9464
# → curl http://localhost:9464/metrics

# OTLP push to a collector (HTTP/JSON, every 30s by default)
agent-relay metrics-serve --otlp http://localhost:4318/v1/metrics

# Both can run together
agent-relay metrics-serve --prometheus :9464 --otlp http://localhost:4318/v1/metrics
```

Metrics emitted (Prometheus naming; OTLP uses dotted equivalents):

| Metric                                        | Type    | Labels                                                                   |
| --------------------------------------------- | ------- | ------------------------------------------------------------------------ |
| `agent_relay_tokens_total`                    | counter | `agent`, `direction` (`input`, `output`, `cache_read`, `cache_creation`) |
| `agent_relay_cost_usd_total`                  | counter | `agent`                                                                  |
| `agent_relay_turn_duration_ms_sum` / `_count` | summary | `agent`                                                                  |
| `agent_relay_turns_total`                     | counter | `agent`, `result` (`success`, `error`)                                   |
| `agent_relay_session_active`                  | gauge   | —                                                                        |
| `agent_relay_sessions_total`                  | gauge   | `status`                                                                 |

### Alerts

Threshold-based alerts ride the `metrics-tail` channel. They are opt-in —
no config file, no alerts. Drop a `.agent-relay/config/alerts.toml`:

```toml
cost_per_turn_usd = 0.50
cost_per_session_usd = 5.00
duration_per_turn_ms = 300000
tokens_per_turn = 200000
error_rate_threshold = 0.4    # ratio 0..1; gated by error_rate_min_turns
error_rate_min_turns = 5
```

When a turn breaches a threshold, `metrics-tail` writes a colored line to
stderr and emits an extra JSONL line on stdout:

```json
{
  "kind": "metrics.alert",
  "rule": "cost_per_turn",
  "severity": "warning",
  "session_id": "...",
  "turn_number": 3,
  "threshold": 0.5,
  "observed": 0.71,
  "message": "turn 3 cost $0.7100 exceeds threshold $0.5000",
  "timestamp": "..."
}
```

Severity is `critical` at ≥ 2× threshold, `warning` otherwise. Webhook
delivery (when `--webhook` is set) carries alert lines too, so external
systems can react without polling.

### Concurrent work with conflict recovery

```bash
# Start a concurrent run
agent-relay race c x "Build the auth module"

# Continue an interrupted or timed-out concurrent session
agent-relay race --continue <session-id> c x "Continue the task"

# Inspect saved conflict artifacts and versions
agent-relay inspect-conflicts <session-id>

# Resume unresolved conflict resolution
agent-relay resolve <session-id>
agent-relay resolve --latest
```

Claim roles in `race`:

- `owner`: exclusive editor for that path or directory
- `shared`: multiple agents may edit that scope intentionally
- `reviewer`: review-only overlap; edits in reviewer-only scope are blocked

Notes:

- On macOS, Relay can auto-open one terminal window or tab per tmux session. Use `--open-terminals` or `--no-open-terminals` to control that behavior explicitly.
- If a concurrent run ends in `manual_resolution_required`, use `inspect-conflicts` first to see the saved versions, then `resolve` to continue the resolution workflow.
- If a concurrent run ends in `max_time`, `interrupted`, `incomplete`, or `agent_error`, use `race --continue <session-id> ...` to continue the broader task.

### REPL permission backends

Relay now keeps repo-local permission backend config in:

```text
.agent-relay/permissions.toml
```

From the REPL:

```text
/permissions
/permissions set claude mode dontAsk
/permissions set codex approval_policy on-request
/permissions set codex sandbox_mode danger-full-access
```

Important limitation: this currently controls launch-time permission behavior
for turn-mode agents like Claude and Codex. It does not yet provide generic
cross-agent per-tool approval unless the underlying CLI exposes pending tool
requests in a machine-readable way.

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

## Configuration

### Capture helpers

| Variable                                               | Description                         |
| ------------------------------------------------------ | ----------------------------------- |
| `AGENT_RELAY_AUTOSAVE_GIT_TOUCHED_FILES=1`             | Auto-save git diff of touched files |
| `AGENT_RELAY_AUTOSAVE_RESEARCH_NOTE_FILE=<path>`       | Auto-capture research notes         |
| `AGENT_RELAY_AUTOSAVE_IMPLEMENTATION_NOTE_FILE=<path>` | Auto-capture implementation notes   |
| `AGENT_RELAY_AUTOSAVE_VALIDATION_SUMMARY_FILE=<path>`  | Auto-capture validation summary     |
| `AGENT_RELAY_AUTOSAVE_PLANNING_SNAPSHOT_FILE=<path>`   | Auto-capture planning snapshot      |
| `AGENT_RELAY_AUTOSAVE_PROPOSED_EDITS_FILE=<path>`      | Auto-capture proposed edits         |

### Launch template overrides

| Variable                             | Description                      |
| ------------------------------------ | -------------------------------- |
| `AGENT_RELAY_CLAUDE_LAUNCH_TEMPLATE` | Custom launch command for Claude |
| `AGENT_RELAY_CODEX_LAUNCH_TEMPLATE`  | Custom launch command for Codex  |

### Permission backend overrides

| Variable | Description |
| --- | --- |
| `AGENT_RELAY_CLAUDE_PERMISSION_MODE` | Override Claude REPL/concurrent permission mode |
| `AGENT_RELAY_CODEX_APPROVAL_POLICY` | Override Codex approval policy (`-a`) |
| `AGENT_RELAY_CODEX_SANDBOX_MODE` | Override Codex sandbox mode (`-s`) |
| `AGENT_RELAY_CAPTURE_PERMISSION_PTY=1` | Capture raw PTY permission prompt output for adapter development |
| `AGENT_RELAY_PERMISSION_CAPTURE_DIR=<path>` | Override the PTY permission capture directory |

### Capture hook templates

| Variable                              | Description                            |
| ------------------------------------- | -------------------------------------- |
| `AGENT_RELAY_CLAUDE_CAPTURE_TEMPLATE` | Custom capture hook for Claude exports |
| `AGENT_RELAY_CODEX_CAPTURE_TEMPLATE`  | Custom capture hook for Codex exports  |

### Template placeholders

Available in both launch and capture templates:

| Placeholder          | Description                        |
| -------------------- | ---------------------------------- |
| `{agent}`            | Agent key (e.g. `claude`)          |
| `{agent_name}`       | Display name (e.g. `Claude Code`)  |
| `{agent_cli}`        | Shell-quoted CLI command           |
| `{repo_root}`        | Shell-quoted repo root path        |
| `{repo_root_path}`   | Unquoted repo root path            |
| `{resume_path}`      | Shell-quoted path to resume packet |
| `{resume_path_path}` | Unquoted path to resume packet     |
| `{session_id}`       | Session identifier                 |

### Built-in defaults

```
claude: cd {repo_root} && claude -p "$(cat {resume_path})"
codex:  cd {repo_root} && codex "$(cat {resume_path})"
```

Custom launch templates must include `{resume_path}` or `{resume_path_path}`. If a template omits the packet input, `launch --execute` refuses to run it.

Capture templates are optional. If set, they should print JSON to stdout with any of these fields:

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
- `turns/<turn-number>/`: turn artifacts from `run`/`chat` sessions (prompt, output, stderr, state)

This keeps the session state inspectable and vendor-independent.

## What not to publish

Do not commit or publish `.agent-relay/`. Session artifacts can contain:

- absolute local paths
- research notes and implementation notes
- validation summaries
- captured Git status, workspace patches, and untracked-file manifests
- generated resume packets
- rendered launch commands and launch-template text
- full agent conversation output (from `run`/`chat` sessions)

Two practical rules:

- add `.agent-relay/` to `.gitignore`
- do not put secrets or tokens into `AGENT_RELAY_CLAUDE_LAUNCH_TEMPLATE` or `AGENT_RELAY_CODEX_LAUNCH_TEMPLATE`, because the rendered command and template are recorded in handoff metadata
