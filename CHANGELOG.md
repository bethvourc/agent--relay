# Changelog

All notable changes to Agent Relay are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Releases and downloadable artifacts live on the
[Releases page](https://github.com/bethvourc/agent--relay/releases).

## [Unreleased]

## [0.5.6] — 2026-05-14

### Added
- `deactivate` command (alias `complete`) for marking a session as finished
  or inactive, giving you explicit control over session lifecycle instead of
  relying on implicit timeouts.
- Console feedback when a session is deactivated, so it's clear which session
  closed and what its final state was.
- Public community mirror at
  [github.com/bethvourc/agent--relay](https://github.com/bethvourc/agent--relay)
  for README, issues, discussions, and releases. Source remains private.

### Changed
- Project metadata (`pyproject.toml` Homepage/Source/Issues) now points at the
  public mirror, so PyPI sidebar links resolve for anonymous visitors.
- Docs site (`agent-relay.dev`) GitHub/issue links route to the public mirror.
- Installation scripts (`install.sh`, `install.ps1`) refined for clearer
  platform-specific guidance.

## [0.5.5] — 2026-05-10

### Changed
- Improved cost estimation and model handling in metrics + CLI; cost labels
  in the dashboard and alerts now read as "est. cost" to make clear that
  values are estimates rather than billed amounts.
- Last release before the public-mirror split.

## [0.5.0] — 2026-05-09

### Added
- `alerts` command for inspecting threshold breaches across sessions, with
  matching dashboard panel that integrates alerts into the session detail
  view.
- Live-update controls for the dashboard: opt-in soft-refresh with JSON
  payloads so the page can update in place without a full reload.
- `MetricsFilter` for scoped metric queries — filter dashboard views by
  session, agent, time window, and more.
- HTML dashboard surface for the Prometheus exporter so operators get a
  browsable view alongside the scrape endpoint.
- Chart features on the dashboard for session metrics (token, cost, and
  latency over turns).
- PyPI download badge on the README for visibility into install volume.

### Changed
- Standardised metric labels and heading styles across the UI for a more
  consistent look.
- Refactored UI colour themes to use a token-based styling system; surface
  rule applied consistently to metrics panels and other surfaces.
- Help command structure reworked for clearer navigation and grouping.
- Turn-status aliases introduced so metrics and watch output read more
  naturally.

### Removed
- Deprecated CLI and integration test files cleaned out as part of the
  metrics refactor.

## [0.4.0] — 2026-05-05

### Added
- `watch` command — live session monitoring that follows an in-progress
  session and auto-picks the latest active session when none is given.
  Includes a `--metrics` panel that refreshes per turn.
- `metrics` command for token / cost / latency rollups per session.
- `metrics-tail` command streams metric events as JSONL for ingestion into
  external pipelines.
- `metrics-serve` command exposes Prometheus and OTLP exporters from the
  local daemon, with alert evaluation hooks emitted into the JSONL stream.

### Changed
- Fallback logic in `watch` improved so the command picks the latest
  session when no id is supplied, instead of erroring out.

## [0.3.0] — 2026-04-03

### Added
- Gemini agent adapter — Agent Relay now drives Gemini alongside Claude and
  Codex.

### Changed
- Turn prompt logic refined to conditionally display the preamble,
  improving conversation flow when context is already loaded.
- Session snapshot rendering in `handoffs` cleaned up for a tighter output.

## [0.2.0] — 2026-03-31

### Added
- `converse` command for agent-to-agent turn-based interaction.
- `discover` command for detecting available agent CLIs on the host.
- `clean` command for removing all relay sessions.
- `resolve` command for resolving conflicts in concurrent agent runs,
  including capture-hook specifications and claim handling.
- Concurrent execution support with tmux session management and pane
  capture; phase management and control status reporting for multi-agent
  workflows.
- Agent aliases and tmux integration so existing tmux users can plug Agent
  Relay into their workflow.
- Verbose output option on the `claude` command.

### Changed
- Renamed PyPI package from `agent-relay` to `agent-relay-tool` to clear up
  naming conflicts; installation instructions updated to match.
- Codex output normalisation: trailing "done" markers stripped so handoff
  payloads don't carry noise.
- Conversation rendering in the CLI extended to include agent output
  inline.
- Workstream kind defaults to `mixed` in concurrent execution; schema
  validation added.

## [0.1.0] — 2026-03-27

### Added
- Initial release. Agent Relay ships as a local-first CLI for handing off
  coding sessions between AI agents.
- `agent-relay <agent>` — the one command that captures the current session
  state, generates a handoff packet, and launches the next agent with
  context preserved.
- v2 session model: per-repo storage at `<repo>/.agent-relay/` with
  manifests, journals, checkpoints, and content-addressed objects.
- `repair` command for fixing inconsistencies in v2 sessions, plus
  integrity checks on session load.
- Lifecycle management (active / completed / archived) with safety checks
  on launch commands and agent profiles.
- Status / dashboard rendering for inspecting sessions from the CLI.
- Migration path from legacy session files into v2 sessions.

[Unreleased]: https://github.com/bethvourc/agent--relay/compare/v0.5.6...HEAD
[0.5.6]: https://github.com/bethvourc/agent--relay/releases/tag/v0.5.6
[0.5.5]: https://github.com/bethvourc/agent--relay/releases/tag/v0.5.5
[0.5.0]: https://github.com/bethvourc/agent--relay/releases/tag/v0.5.0
[0.4.0]: https://github.com/bethvourc/agent--relay/releases/tag/v0.4.0
[0.3.0]: https://github.com/bethvourc/agent--relay/releases/tag/v0.3.0
[0.2.0]: https://github.com/bethvourc/agent--relay/releases/tag/v0.2.0
[0.1.0]: https://github.com/bethvourc/agent--relay/releases/tag/v0.1.0
