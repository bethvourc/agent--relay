# Changelog

All notable changes to Agent Relay are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Releases and downloadable artifacts live on the
[Releases page](https://github.com/bethvourc/agent--relay/releases).

## [Unreleased]

## [0.6.1] — 2026-05-20

### Removed
- **Intel macOS native binary** (`relay-darwin-x64`). GitHub deprecated the
  `macos-13` runner in 2026 and capacity collapsed — jobs targeting that
  label routinely queue for hours without starting. Intel-Mac users now
  fall through automatically to `install.sh`'s `uv tool install` fallback;
  the curl one-liner still works on Intel Macs, it just takes the source
  path instead of a binary download. Apple Silicon, Linux x64, Linux
  arm64, and Windows x64 binaries are unaffected. Revisit when (if) we
  need cross-compile-from-arm64 to bring back the native binary.

### Fixed
- The v0.6.0 release shipped without the Homebrew bump PR (the
  `bump-homebrew` job depended on the cancelled darwin-x64 matrix entry).
  With darwin-x64 dropped from the matrix, v0.6.1 produces a clean
  Homebrew bump PR automatically, so `brew install bethvourc/tap/agent-relay`
  now works.

## [0.6.0] — 2026-05-20

### Added
- **Always-on layer**: a small local daemon plus four adapters captures
  context from every AI coding agent on the machine and hands off
  automatically when one rate-limits. Drive it from the CLI, or let it
  run in the background. See [the always-on guide](https://agent-relay.dev/always-on).
- **`relay install` / `uninstall` / `doctor`** — detects installed agents
  (Claude Code, Cursor, Antigravity, Windsurf, VS Code, Codex CLI, aider,
  Gemini CLI, Warp), wires hooks/extensions/configs, and registers the
  daemon for auto-start via launchd / systemd user units / Windows
  Startup folder. `doctor` runs six health checks.
- **`relay daemon start|stop|status|tail`** — manages the background
  process; `tail` streams live events from every adapter.
- **`relay wrap <cmd>`** — PTY-wraps any CLI agent (codex, aider,
  gemini-cli, sgpt, llm) so its rate-limits and lifecycle are captured
  without disturbing colours, prompts, or `^C`.
- **`relay resume <snapshot-id>`** + **`relay snapshots`** — list and
  reopen handoff snapshots produced by the daemon.
- **`relay dashboard`** — local web UI showing live sessions, snapshots,
  and a handoff trigger. Built into the binary; no external service.
- **`relay proxy start|status|cert`** — opt-in HTTPS proxy (requires
  `pip install agent-relay-tool[proxy]`) for lossless rate-limit capture
  from Anthropic / OpenAI / Google response headers.
- **`relay mcp serve`** — MCP server that lets Warp's native agent (or
  any MCP-aware client) feed events into the relay log.
- **`relay self-update`** — pulls the latest binary release and replaces
  the running executable atomically.
- **`relay` short command** — declared alongside `agent-relay` in the
  PyPI package, so the canonical short name works regardless of install
  method.
- **`--version` / `-V`** flag on the root parser.
- **VS Code-family extension** — one extension published to Open VSX and
  the VS Code Marketplace covers Cursor, Antigravity, VS Code, Windsurf,
  Trae, Void, and any future VS Code fork. Includes a `Relay: Hand off
  this session` command on `Cmd+Shift+R`.
- **Native binary distribution** — PyInstaller bundles for macOS arm64 /
  macOS x64 / Linux x64 / Linux arm64 / Windows x64, published on every
  release via GitHub Actions. The curl one-liner at agent-relay.dev now
  detects platform and pulls the right binary, with a `uv tool` fallback.
- **Homebrew tap** at `bethvourc/homebrew-tap` — `brew install
  bethvourc/tap/agent-relay` installs the native binary.
- **Docs site**: new pages at `/always-on`, `/architecture`, `/privacy`,
  `/adapters/{claude-code,cursor,warp,cli}`.

### Changed
- `pyproject.toml` cleaned up — `[project.optional-dependencies]` was
  previously nested inside `[project]`, which silently dropped
  `authors` / `keywords` / `classifiers` under the wrong section.
- Release pipeline split: `publish.yml` keeps PyPI ownership;
  `release.yml` owns platform binaries (uploaded to the public mirror
  via a scoped PAT), VS Code extension publishing, and the Homebrew bump
  PR. Source code stays on the private origin; only compiled artifacts
  surface publicly.

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

[Unreleased]: https://github.com/bethvourc/agent--relay/compare/v0.6.1...HEAD
[0.6.1]: https://github.com/bethvourc/agent--relay/releases/tag/v0.6.1
[0.6.0]: https://github.com/bethvourc/agent--relay/releases/tag/v0.6.0
[0.5.6]: https://github.com/bethvourc/agent--relay/releases/tag/v0.5.6
[0.5.5]: https://github.com/bethvourc/agent--relay/releases/tag/v0.5.5
[0.5.0]: https://github.com/bethvourc/agent--relay/releases/tag/v0.5.0
[0.4.0]: https://github.com/bethvourc/agent--relay/releases/tag/v0.4.0
[0.3.0]: https://github.com/bethvourc/agent--relay/releases/tag/v0.3.0
[0.2.0]: https://github.com/bethvourc/agent--relay/releases/tag/v0.2.0
[0.1.0]: https://github.com/bethvourc/agent--relay/releases/tag/v0.1.0
