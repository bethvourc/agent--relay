# Agent Relay Demo Walkthrough

This walkthrough gives you one concrete end-to-end path you can run locally.

It covers:

1. creating a session
2. writing a checkpoint
3. preparing a failover
4. dry-running the launch step
5. executing the launch step safely
6. inspecting the resulting session files

## Goal

By the end of this walkthrough, you will have a real session under `.agent-relay/`, a target-specific resume packet, a recorded handoff, and a successful launch result in `state.json`.

## Why This Walkthrough Uses a Launch Override

The default launch commands are:

- `claude`: `cd {repo_root} && claude`
- `codex`: `cd {repo_root} && codex`

If you already have those CLIs installed, you can use the defaults.

This walkthrough uses `AGENT_RELAY_CODEX_LAUNCH_TEMPLATE` so you can test the full flow even if neither CLI is installed. The override writes a marker file instead of launching a real agent.

## Prerequisites

- Python 3.11+
- this repository checked out at `/Users/bethvour/projects/agent-relay`

## 1. Set Up Variables

Run:

```bash
export AGENT_RELAY_ROOT=/Users/bethvour/projects/agent-relay
export DEMO_REPO=/tmp/agent-relay-demo
mkdir -p "$DEMO_REPO"
cd "$AGENT_RELAY_ROOT"
```

## 2. Verify The CLI Surface

Run:

```bash
PYTHONPATH=src python3 -m agent_relay.cli --help
```

You should see these commands:

- `start`
- `checkpoint`
- `failover`
- `launch`
- `inspect`

## 3. Configure A Safe Launch Command

Run:

```bash
export AGENT_RELAY_CODEX_LAUNCH_TEMPLATE="cd {repo_root} && python3 -c 'from pathlib import Path; Path(\"launch-marker.txt\").write_text(\"ok\")'"
```

This makes the future `launch --execute` step write `launch-marker.txt` into the demo repo instead of requiring the real Codex CLI.

## 4. Start A Session

Run:

```bash
SESSION_ID=$(PYTHONPATH=src python3 -m agent_relay.cli start \
  --agent claude \
  --task "Demo handoff flow" \
  --repo "$DEMO_REPO" \
  | sed -n '1s/^Created session //p')

echo "$SESSION_ID"
```

What this should create:

- `state.json`
- an initial checkpoint under `checkpoints/`
- `summary.md`

## 5. Write A Checkpoint

Run:

```bash
PYTHONPATH=src python3 -m agent_relay.cli checkpoint "$SESSION_ID" \
  --next-action "Prepare a Codex handoff" \
  --decision "Keep session state local-first" \
  --touched-file "src/agent_relay/cli.py" \
  --repo "$DEMO_REPO"
```

This should append a new checkpoint and refresh `summary.md`.

## 6. Prepare Failover

Run:

```bash
PYTHONPATH=src python3 -m agent_relay.cli failover "$SESSION_ID" \
  --to-agent codex \
  --reason "demo walkthrough" \
  --repo "$DEMO_REPO"
```

What this should produce:

- `resume/codex.md`
- a new handoff record in `state.json`
- a rendered launch command based on the safe override

## 7. Dry-Run The Launch Step

Run:

```bash
PYTHONPATH=src python3 -m agent_relay.cli launch "$SESSION_ID" \
  --repo "$DEMO_REPO"
```

This should print:

- the launch target
- the resume packet path
- the launch command
- the launch instructions

It should not mutate the launch status yet.

## 8. Execute The Launch Step

Run:

```bash
PYTHONPATH=src python3 -m agent_relay.cli launch "$SESSION_ID" \
  --repo "$DEMO_REPO" \
  --execute
```

Because of the override, this should create:

- `$DEMO_REPO/launch-marker.txt`

and update the session state so the handoff launch result becomes `succeeded`.

## 9. Inspect What Was Written

Run:

```bash
find "$DEMO_REPO/.agent-relay/sessions/$SESSION_ID" -maxdepth 2 -type f | sort
cat "$DEMO_REPO/.agent-relay/sessions/$SESSION_ID/summary.md"
cat "$DEMO_REPO/.agent-relay/sessions/$SESSION_ID/resume/codex.md"
PYTHONPATH=src python3 -m agent_relay.cli inspect "$SESSION_ID" --repo "$DEMO_REPO"
cat "$DEMO_REPO/launch-marker.txt"
```

You should now see:

- at least two checkpoint files
- `summary.md`
- `resume/codex.md`
- `state.json` showing:
  - `current_agent: codex`
  - `current_status: active`
  - a handoff record with `launch_status: succeeded`
- `launch-marker.txt` containing `ok`

## 10. Return To Default Behavior

When you are done with the safe launch override, run:

```bash
unset AGENT_RELAY_CODEX_LAUNCH_TEMPLATE
```

After that, future Codex failovers will go back to the default launch command:

```text
cd {repo_root} && codex
```

## If You Want To Use A Real Agent CLI

If you already have `codex` or `claude` installed locally:

1. skip the override step
2. run `failover`
3. run `launch --execute`

That will use the real default launch template for the target agent instead of the safe demo command.
