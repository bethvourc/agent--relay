# Agent Relay V1 Release Checklist

Use this checklist before calling the current local-first MVP ready.

## Environment

- `uv sync`
- `.venv/bin/agent-relay --help`

## Automated Validation

- `.venv/bin/python -m unittest discover -s tests`
- confirm the suite includes the bidirectional integration flow

## Manual Demo Validation

- follow [demo-walkthrough.md](/Users/bethvour/projects/agent-relay/docs/examples/demo-walkthrough.md)
- confirm the walkthrough succeeds in both directions:
  - `claude -> codex`
  - `codex -> claude`

## Session Artifact Checks

- verify one session contains multiple checkpoints
- verify both `resume/codex.md` and `resume/claude.md` exist
- verify `state.json` shows successful launch records for both handoffs
- verify the session still accepts a new checkpoint after the second launch

## Documentation Checks

- confirm [README.md](/Users/bethvour/projects/agent-relay/README.md) matches the current CLI surface
- confirm [roadmap-status.md](/Users/bethvour/projects/agent-relay/docs/developer/roadmap-status.md) reflects the current completed phase
- confirm example commands use the installed CLI path that new users should run

## Release Decision

The current V1 baseline is ready when:

- the automated suite is green
- the bidirectional walkthrough works from docs alone
- the resulting session history is coherent without transcript archaeology
