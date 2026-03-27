# Agent Relay V1 Release Checklist

Use this checklist before calling the current local-first MVP ready.

## Environment

- `uv sync`
- `.venv/bin/agent-relay --help`

## Automated Validation

- `.venv/bin/python -m unittest discover -s tests`
- confirm the suite includes the bidirectional integration flow

## Package Validation

- `uv build`
- inspect `dist/agent_relay-*.tar.gz` and confirm the source distribution only contains publishable package files
- inspect `dist/agent_relay-*.whl` and confirm the wheel only contains runtime package code and metadata
- confirm internal planning material is not part of published artifacts:
  - `docs/agent/`
  - `docs/developer/`
  - `docs/features/`
- confirm generated state and build artifacts are not tracked:
  - `.agent-relay/`
  - `build/`
  - `dist/`

## Manual Demo Validation

- follow [demo-walkthrough.md](../examples/demo-walkthrough.md)
- confirm the walkthrough succeeds in both directions:
  - `claude -> codex`
  - `codex -> claude`

## Session Artifact Checks

- verify one session contains multiple checkpoints
- verify at least two `objects/handoffs/<handoff-id>/packet.md` files exist
- verify `agent-relay inspect <session>` shows successful launch records for both handoffs
- verify the session still accepts a new checkpoint after the second launch

## Documentation Checks

- confirm [README.md](../../README.md) matches the current CLI surface and storage model
- confirm [roadmap-status.md](roadmap-status.md) reflects the current completed phase
- confirm example commands use the installed CLI path that new users should run
- confirm public docs do not contain absolute local-machine paths

## Release Decision

The current V1 baseline is ready when:

- the automated suite is green
- the bidirectional walkthrough works from docs alone
- the resulting session history is coherent without transcript archaeology
