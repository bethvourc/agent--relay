# Agent Relay Demo Walkthrough

This walkthrough is the Phase 7 validation path.

It proves one session can move:

1. `Claude Code -> Codex`
2. `Codex -> Claude Code`

without losing the same repo-local session history.

## Goal

By the end of this walkthrough, you will have:

- one real session under `.agent-relay/`
- multiple checkpoints in the same session
- at least two handoff packets under `objects/handoffs/`
- two successful handoff launch records visible through `agent-relay inspect`
- a final session that continues after both handoffs

## Why This Walkthrough Uses Safe Launch Overrides

The default launch commands are:

- `claude`: `cd {repo_root} && claude --resume {resume_path}`
- `codex`: `cd {repo_root} && codex --resume {resume_path}`

This walkthrough uses safe overrides so you can validate the full orchestration path even if neither real CLI is installed locally.

## Prerequisites

- Python 3.11+
- `uv`
- this repository checked out locally

## 1. Set Up The Tooling

Run:

```bash
export AGENT_RELAY_ROOT=/path/to/agent-relay
export DEMO_REPO=/tmp/agent-relay-demo

mkdir -p "$DEMO_REPO"
cd "$AGENT_RELAY_ROOT"
uv sync
```

## 2. Configure Safe Launch Commands

Run:

```bash
export AGENT_RELAY_CODEX_LAUNCH_TEMPLATE="cd {repo_root} && python3 -c 'from pathlib import Path; Path(\"codex-launch.txt\").write_text(\"ok\")' {resume_path}"
export AGENT_RELAY_CLAUDE_LAUNCH_TEMPLATE="cd {repo_root} && python3 -c 'from pathlib import Path; Path(\"claude-launch.txt\").write_text(\"ok\")' {resume_path}"
```

These override the real launches, keep the commands packet-aware, and write marker files into the demo repo instead.

## 3. Verify The CLI Surface

Run:

```bash
.venv/bin/agent-relay --help
```

You should see these commands:

- `start`
- `checkpoint`
- `pause`
- `prepare`
- `failover`
- `launch`
- `resume`
- `repair`
- `inspect`
- `status`

## 4. Start The Session

Run:

```bash
SESSION_ID=$(.venv/bin/agent-relay start \
  --agent claude \
  --task "Validate bidirectional handoff flow" \
  --repo "$DEMO_REPO" \
  --quiet)

echo "$SESSION_ID"
```

This creates:

- `session.json`
- an initial checkpoint under `objects/checkpoints/`
- derived session state under `refs/` and `derived/`

## 5. Claude Code -> Codex

Run:

```bash
.venv/bin/agent-relay checkpoint "$SESSION_ID" \
  --next-action "Prepare the Codex handoff" \
  --decision "Use safe launch overrides for the demo" \
  --capture-git-changes \
  --repo "$DEMO_REPO"

.venv/bin/agent-relay prepare "$SESSION_ID" \
  --next-action "Hand off to Codex and continue implementation" \
  --validation-status partial \
  --validation-summary "The launch path still needs end-to-end validation" \
  --repo "$DEMO_REPO"

.venv/bin/agent-relay failover "$SESSION_ID" \
  --to-agent codex \
  --reason "demo walkthrough step one" \
  --resume-evidence-depth full \
  --repo "$DEMO_REPO"

.venv/bin/agent-relay launch "$SESSION_ID" \
  --repo "$DEMO_REPO" \
  --execute

.venv/bin/agent-relay resume "$SESSION_ID" \
  --repo "$DEMO_REPO"
```

What this should produce:

- a handoff packet under `objects/handoffs/`
- a handoff record from `claude -> codex`
- `$DEMO_REPO/codex-launch.txt`
- session state with `current_agent: codex`

## 6. Continue In The Same Session Under Codex

Run:

```bash
.venv/bin/agent-relay checkpoint "$SESSION_ID" \
  --next-action "Prepare a return handoff to Claude" \
  --implementation-note "Codex completed the implementation slice and wants review" \
  --repo "$DEMO_REPO"
```

This proves the session continues instead of starting over after the first handoff.

## 7. Codex -> Claude Code

Run:

```bash
.venv/bin/agent-relay prepare "$SESSION_ID" \
  --next-action "Return to Claude for validation and close-out" \
  --validation-status partial \
  --validation-summary "Implementation is complete but final review is still pending" \
  --repo "$DEMO_REPO"

.venv/bin/agent-relay failover "$SESSION_ID" \
  --to-agent claude \
  --reason "demo walkthrough return step" \
  --resume-evidence-depth full \
  --repo "$DEMO_REPO"

.venv/bin/agent-relay launch "$SESSION_ID" \
  --repo "$DEMO_REPO" \
  --execute

.venv/bin/agent-relay resume "$SESSION_ID" \
  --repo "$DEMO_REPO"
```

What this should produce:

- another handoff packet under `objects/handoffs/`
- a second handoff record from `codex -> claude`
- `$DEMO_REPO/claude-launch.txt`
- session state with `current_agent: claude`

## 8. Finish With One More Checkpoint

Run:

```bash
.venv/bin/agent-relay checkpoint "$SESSION_ID" \
  --next-action "Ship the validated demo flow" \
  --decision "The same session survives multiple handoffs" \
  --validation-status passed \
  --validation-summary "Bidirectional handoff demo completed successfully" \
  --repo "$DEMO_REPO"
```

This final checkpoint proves the session still works after both launches.

## 9. Inspect What Was Written

Run:

```bash
find "$DEMO_REPO/.agent-relay/sessions/$SESSION_ID" -maxdepth 4 -type f | sort
.venv/bin/agent-relay inspect "$SESSION_ID" --repo "$DEMO_REPO"
cat "$DEMO_REPO/codex-launch.txt"
cat "$DEMO_REPO/claude-launch.txt"
```

You should now see:

- multiple checkpoint files in one session
- both target-specific handoff packets
- `agent-relay inspect` showing:
  - `current_agent: claude`
  - `current_status: active`
  - two handoff records
  - both `launch_status: succeeded`
- `codex-launch.txt` containing `ok`
- `claude-launch.txt` containing `ok`

## 10. Return To Default Behavior

When you are done with the safe overrides, run:

```bash
unset AGENT_RELAY_CODEX_LAUNCH_TEMPLATE
unset AGENT_RELAY_CLAUDE_LAUNCH_TEMPLATE
```

After that, future failovers will return to the default real-agent launch commands.
