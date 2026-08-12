# Changelog

All notable changes to Agent Relay are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Releases and downloadable artifacts live on the
[Releases page](https://github.com/bethvourc/agent--relay/releases).

## [Unreleased]

### Added
- **`relay handoff plan` / `relay handoff arm`.** The rate-limit handoff
  decision — who continues, and why not everyone else — is now a command
  rather than something only the interactive REPL could reach. `plan` answers
  offline and immediately from the shared rate-limit ledger, the installed
  CLIs, and any successor already parked; `arm` is the predictive half that
  asks the running agent while it still has quota to answer.

  This exists so the desktop app can stop guessing. It had grown its own
  parallel failover in TypeScript that picked the next name off a hardcoded
  list, with no memory of which agents were already spent. Both now ask the
  same Python.

### Changed
- Rate-limit records now carry the provider's **real reset time** when a quota
  reading is cached, instead of always expiring after a flat hour. The ledger
  always promised this; nothing had been supplying it.

## [0.7.6] — 2026-08-10

### Added
- **Relay picks your next agent before you run out.** When the running agent
  crosses 90% of a usage window, Relay asks it — while it still has the quota
  to answer — what remains to finish the job and which of the *installed,
  authenticated, not-already-limited* agents should continue. The answer is
  parked on disk. When the limit actually hits, the handoff is immediate:
  no model call at the worst possible moment, just a decision that was already
  made. The remaining-scope half becomes the successor's `Next action`, so it
  picks up the whole job rather than the slice that happened to be in flight.

  One line on screen (`relay: claude at 91% — codex will continue if the limit
  hits`), and one when it fires. The analysis runs out-of-band on the post-turn
  worker, so it never blocks a turn, never enters your agent's conversation,
  and never puts raw output on screen. Every failure — no CLI, a timeout,
  unparseable output — leaves the feature disarmed and the existing reactive
  handoff untouched.

  Requires real quota telemetry, so it arms for Claude Code and Codex only;
  Gemini, Kimi and OpenCode keep exactly today's behaviour. Tune or disable
  with `handoff.successor_threshold` (0 disables).
- **Relay offers your preferred agent back.** When a rate-limited agent's
  window resets, Relay says so once — an offer, not an automatic switch.
- **`relay limits` / `/limits`.** Real subscription usage for the agent CLIs —
  Claude Code's 5-hour and weekly windows, and Codex's quota — read from each
  CLI's own authoritative source rather than estimated. Agents that expose no
  quota surface report that explicitly instead of being omitted, so "we cannot
  see it" is never mistaken for "it has room".

### Fixed
- **Automatic handoff could return to the agent that just ran out.** Fallback
  selection took the first agent in the configured order that differed from
  the current one, with no memory and no availability check — so with an order
  of `codex, claude`, a rate-limited claude went to codex, and a rate-limited
  codex went straight back to claude. Relay now remembers which agents are
  rate-limited (account-scoped, so it survives a restart and applies across
  repos) and skips agents whose CLI is not installed. When no agent can take
  over, the message names the actual obstacle — `gemini is not installed`,
  `claude is rate-limited` — instead of the old catch-all, and the checkpoint
  is preserved for `relay resume`. Explicit `/use` is unaffected: asking for a
  specific agent still switches to it.
- **Handoff packets told the next agent to hand off.** The packet's
  `Next action` line — the most directive thing the successor reads — was
  filled with Relay's own bookkeeping (`Hand off to Codex`), and new sessions
  recorded `Continue work in <the agent that just stopped>`. Both now carry the
  work itself: the task, or the session objective. Affects every handoff,
  including manual `/use` and `relay <agent>`.

## [0.7.5] — 2026-08-09

### Added
- **Account-specific models in `/model`.** The REPL picker now lists the models
  your signed-in account can actually use, asking each agent's own tooling where
  it can (Codex's model cache, `agy models`, `opencode models`) and falling back
  to Relay's curated suggestions otherwise. This brings the CLI level with the
  desktop model menu, and makes OpenCode model selection possible at all — its
  models are account-specific, so the curated-only picker could offer nothing
  but "default". Detection is cached for a few minutes; `/model --refresh`
  re-detects. An unrecognised name is still accepted, now with a warning and
  close-match suggestions.
- **Readable provider failures.** A turn that fails at the provider (an
  unavailable model, an expired login, a context overflow) now reports one
  actionable sentence instead of the agent's raw protocol JSON. Checked
  independently of the exit code, because some agent CLIs report the failure
  in-band and still exit zero. Rate limits are untouched and still drive
  automatic handoffs.
- **`relay discover` reports models.** `--json` now includes each agent's
  `models` and `model_flag` (already on the underlying result, but dropped by
  the command), the table shows a model count, and `--models` lists them. There
  was previously no way to enumerate models without opening the picker.
- **`relay run --model` warns on an unrecognised name.** Same wording and
  close-match suggestions as `/model`, since `--model` is sticky and a typo
  otherwise persists as the repo default. Advisory only: the model is still
  applied. Warnings go to stderr, so `--json` stdout stays parseable, and are
  repeated in the payload as `model_warnings`.
- **OpenCode adapter support.** Relay now includes OpenCode in agent
  discovery, aliases (`o`), REPL handoff routing, fallback-order inference,
  dashboard badges, and managed run/handoff flows.
- **OpenCode install and daemon plugin integration.** `relay install` now
  writes a Relay-owned OpenCode plugin when OpenCode is detected, routes
  selected OpenCode events through `relay hook opencode-event`, and removes
  only the Relay-owned plugin on uninstall.
- **OpenCode session export capture.** Relay-managed OpenCode sessions attach
  sanitized `opencode export` output to handoff packets when the native
  OpenCode session id is known; missing ids or export failures remain
  non-fatal and fall back to Relay's observable turn artifacts.
- **Gemini hook-based rate-limit ingestion.** `relay install` now wires a
  Gemini CLI `Notification` hook into `~/.gemini/settings.json` and routes
  quota/rate-limit notifications through `relay hook gemini-notification`.
- **`!` runs shell commands without leaving the session.** `!git status` at the
  prompt (or typed mid-turn, running between turns) executes in the repo root
  and streams its output into the transcript, so what it showed feeds handoffs.
  Each `!` is a fresh shell — no state carries between commands. Non-zero exits
  are reported, and `Ctrl+C` kills the whole pipeline.
- **Queue messages while an agent is working.** Typing during a turn used to go
  nowhere: the input panel is closed for the duration so agent output can stream
  into real terminal scrollback, which left no input surface at all. The REPL now
  reads keystrokes during a turn, shows a standing `› type to queue a follow-up`
  input line with what you are typing, lists what is already waiting, and runs
  queued messages in order when the turn ends — the way Claude Code and Codex
  do. `↑` on an empty line pulls the newest queued message back into the input
  to edit before it is sent; it goes back on the end when you press Enter.
  Enter is the contract: only submitted lines run. A line still half-typed when
  the turn ends pre-fills the next prompt instead of being sent, a multi-line
  paste stays one message rather than becoming one turn per line, and pasted
  code keeps its indentation. A failed or interrupted turn discards the queue
  and prints what was dropped, rather than running follow-ups against a state
  you did not expect. POSIX terminals only; Windows and piped stdin behave
  exactly as before.

### Changed
- **Agent reasoning is shown but never persisted.** Thinking text used to be
  written into the session transcript, which lands on disk and feeds the
  handoff packet the next agent receives. Reasoning is unbounded agent text —
  it quotes system prompts, files it just read, whatever it happened to see —
  and none of that belongs in a durable artifact handed onward. It still
  renders live; `/thinking off` hides it on screen too.
- **Claude model labels no longer pin a version.** The picker read `Opus 4.8`
  while the `opus` alias had already moved to `claude-opus-5`, so it advertised
  the wrong model. Labels are now bare tier names (`Fable`, `Opus`, `Sonnet`,
  `Haiku`), which cannot go stale — Anthropic repoints the aliases at each
  release and the ids follow automatically.
- **Edit previews render as a diff, not a raw patch.** An agent's edit now
  shows as `Update(README.md)` with a plain-language count (`Added 4 lines,
  removed 2 lines`), real file line numbers, surrounding context, and colour
  carrying the add/remove distinction. A `⋯` marks regions the diff skipped, so
  a jump in the line numbers cannot be misread as a deletion. Previously the raw
  unified diff was dumped into a markdown fence, where four of six lines were
  machinery (`--- a/…`, `+++ b/…`, `@@`) and the paths came out doubly-slashed
  (`a//Users/you/repo/README.md`).
- **Escape sequences in agent output can no longer restyle the UI.** File
  content shown in a diff, reasoning text, and spinner labels are now stripped
  of ANSI escapes and control characters before rendering. A file whose bytes
  contain `ESC[31m` was being passed through to the terminal — recolouring
  Relay's own UI and, because escapes are not zero-width, pushing rows past the
  console edge. (Streamed agent output is unchanged: the existing sanitizer
  still allows an agent to colour its own text.)
- **Agent reasoning renders as dimmed prose.** It was a Markdown blockquote,
  which Rich draws with a full-height `▌` bar and renders as Markdown — so the
  least important thing on screen also got boxed code blocks and highlighted
  spans. It is now quiet text that reads as secondary to the answer.
- **The turn transcript repeats itself less, and breathes.** An edit printed
  two lines naming the same file — `Editing /long/absolute/path` and then the
  diff's own header — so the tool line is gone and the diff header carries it.
  Reasoning that only restates the reply it precedes ("Done! The README header
  has been updated…", immediately followed by the agent saying exactly that) is
  no longer shown. And a blank line now separates reasoning, tool calls, diffs
  and the reply, instead of running them together.
- **The per-turn latency breakdown is no longer printed after each reply.** It
  was a development instrument that shipped in 0.7.0 by accident. The timings
  are still recorded in the session's `metadata.latency_ms`, so anything
  reading them off disk is unaffected.

### Fixed
- **Every slash command answers `--help`.** None of them did before: session
  commands fed the flag to their own argument parsing (`/use --help` failed
  with "unknown agent: --help") and registry commands rejected it as an unknown
  flag. Handled centrally in the dispatcher, so it covers both families and
  their aliases (`/c -h`), and prints the command's arguments and flags, or its
  example for a session command.
- **`/help` lists every command the shell answers to, with examples.** Seven
  working commands were missing, `/use` among them, because the listing was
  hand-maintained in two places that had drifted from the dispatcher and from
  each other. Session commands now come from one source shared by `/help`, the
  slash menu, and `docs/repl.md`, each with an example (`e.g. /use claude`), and
  a test pins that source to the dispatcher.
- **A provider error no longer crashes a run.** Claude and Codex stream
  normalization assumed every JSON line was an object with an object-valued
  `message`; a provider error puts a string there, which raised an
  `AttributeError` and took down the whole session with a traceback.

## [0.7.0] — 2026-06-02

### Added
- **Interactive REPL** (`relay` with no subcommand). Persistent slash
  shell modeled on Claude Code / Codex: every existing CLI command is
  also a `/`-prefixed slash command, bare text is forwarded to the
  active agent's PTY, and the inline Textual panel redraws on every
  keystroke. Driven by a single `SlashRegistry` so `/help`, the slash
  menu, and `docs/repl.md` never drift.
- **Tmux integration**. `relay --tmux` wraps the REPL in
  `relay-<sha256(cwd)[:8]>` for crash-safety; `relay --attach`
  reattaches. `/tmux status|detach|split <agent>` from inside the REPL.
  First-run prompt persists the choice to `tmux_auto` in
  `config.toml`.
- **Structured JSONL logging** at `~/.config/relay/repl.log`
  (rotating 5×1 MB). Captures session lifecycle, slash dispatch
  (cmd / duration_ms / ok / error), agent spawn/exit, signal events.
  `RELAY_LOG_LEVEL` honored.
- **`/diagnose`** — shareable bundle (version + redacted log tail)
  for bug reports.
- **Crash recovery** — pidfile reaper at startup. PID files live in
  `~/.config/relay/agents/`; on next launch we cross-check the live
  command line against the recorded agent and only signal genuine
  orphans. Reused PIDs are skipped silently.
- **Onboarding wizard** on first run (config file missing). Detects
  agents, picks a default, asks about tmux, persists to
  `config.toml`. Re-runnable via `/setup`. `--no-onboarding` skips it
  for CI/scripts.
- **`[repl.env]` config section** for opting in additional env
  forwards to spawned agents. Keys validated as proper env-variable
  names; values still scrubbed by the deny-list unless on the
  per-agent provider allowlist.
- **Performance budgets** + gated micro-benches in
  `tests/bench_repl.py` (`RELAY_BENCH=1`). 16 ms p99
  keystroke-to-render target documented in `docs/performance.md`.
- **Auto-generated slash-command reference** (`docs/repl.md`) plus a
  CI gate (`tests/test_repl_docs.py`) that fails the build if the
  registry and the doc drift.
- New docs: `docs/repl.md`, `docs/architecture.md`,
  `docs/performance.md`, `docs/security.md` (env contract +
  redaction pipeline + validation rules).
- `scripts/release.sh X.Y.Z` — one-command release prep. Bumps
  `__version__`, the extension `package.json`, regenerates the lockfile,
  rewrites `CHANGELOG.md`'s `[Unreleased]` header to a dated `[X.Y.Z]`,
  regenerates the docs-site changelog + search-index JSON, and stages
  everything for a PR. Doesn't commit/push/tag — leaves that to the
  user's branch workflow.
- **Hook-based automatic rate-limit handoff.** `relay install` now wires
  Claude Code `Notification` and Codex `Stop` hooks, normalizes hook
  payloads into daemon `rate_limited` events, and lets live REPL sessions
  execute a matching automatic handoff through the configured fallback
  order.
- **Install-time fallback-order setup.** `relay install` infers
  `[handoff].order` from detected Claude, Codex, and Gemini CLIs when no
  order exists. Use `relay install --handoff-order ...` to override or
  `--no-handoff-order` to skip.

### Changed
- **Per-agent env allowlist**. `build_agent_env` now takes an
  `agent=` parameter and only forwards a provider key (e.g.
  `ANTHROPIC_API_KEY`) when launching that agent. All other forwarded
  keys whose values match a known-secret prefix (`sk-`, `AKIA`,
  `ghp_`, `xox`, …) are replaced with `[REDACTED]` before reaching
  the child.
- **Slash parser hardening**. Every string / list-of-string argument
  passes through `validate_string` (rejects null bytes and control
  chars outside `\t\n\r`); `ArgSpec.choices` is now enforced; paths
  go through `resolve_safe_path` (rejects `..` traversal, absolute,
  `~`, and symlink escapes unless `--allow-outside-cwd`).
- **Docs now treat `relay` as the primary workflow.** README, web docs,
  public concept pages, and examples were updated around the interactive
  shell, repo-local session lineage, automatic handoff triggers, and the
  current command surfaces.

### Security
- **`logger.exception(...)` no longer leaks secrets to disk.**
  `_JsonlFormatter.format` now runs `redact()` over `exc_info`,
  pre-cached `exc_text`, and `stack_info` — the last hop before the
  JSONL file. Previously the redaction filter only touched the
  message and structured extras, so a traceback containing
  `key=sk-…` survived to disk.
- **`/diagnose --json` no longer leaks the working directory path.**
  The JSON payload's `cwd` field now goes through the same
  `redact()` call the text path uses, so output is safe to paste
  into a bug report from any project.

### Fixed
- Docs-site changelog page now renders `**bold**` markdown properly
  (was showing literal `**` characters in the v0.6.x entries).

## [0.6.3] — 2026-05-20

### Fixed
- **Homebrew bump PR**: `git push` from the rendered formula failed
  because `gh repo clone` authenticates via `GH_TOKEN` but `git push`
  falls back to the git credential helper which has no credentials in
  CI. Now we rewrite the tap remote URL to embed the PAT
  (`https://x-access-token:$GH_TOKEN@github.com/…`) right after clone so
  subsequent pushes succeed without any extra credential helper setup.

## [0.6.2] — 2026-05-20

### Changed
- **VS Code extension publish is no longer automated.** The Marketplace
  publisher Members / Azure DevOps identity dance proved brittle in CI;
  publishing the `.vsix` by hand from a laptop is a five-minute step per
  release and avoids surprises. The recipe lives as a comment in
  `release.yml`. The extension's `package.json` now tracks the main
  package version (`0.6.2` here) so manual publishes stay in step.

### Fixed
- **Homebrew bump PR**. The release workflow tried to `cp` the rendered
  formula into `tap/Formula/agent-relay.rb`, but a freshly-created tap
  repo doesn't have a `Formula/` directory yet. Added `mkdir -p
  tap/Formula` before the copy.

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
- `relay install` / `uninstall` / `doctor` — detects installed agents
  (Claude Code, Cursor, Antigravity, Windsurf, VS Code, Codex CLI, aider,
  Gemini CLI, Warp), wires hooks/extensions/configs, and registers the
  daemon for auto-start via launchd / systemd user units / Windows
  Startup folder. `doctor` runs six health checks.
- `relay daemon start|stop|status|tail` — manages the background
  process; `tail` streams live events from every adapter.
- `relay wrap <cmd>` — PTY-wraps any CLI agent (codex, aider,
  gemini-cli, sgpt, llm) so its rate-limits and lifecycle are captured
  without disturbing colours, prompts, or `^C`.
- `relay resume <snapshot-id>` + `relay snapshots` — list and
  reopen handoff snapshots produced by the daemon.
- `relay dashboard` — local web UI showing live sessions, snapshots,
  and a handoff trigger. Built into the binary; no external service.
- `relay proxy start|status|cert` — opt-in HTTPS proxy (requires
  `pip install agent-relay-tool[proxy]`) for lossless rate-limit capture
  from Anthropic / OpenAI / Google response headers.
- `relay mcp serve` — MCP server that lets Warp's native agent (or
  any MCP-aware client) feed events into the relay log.
- `relay self-update` — pulls the latest binary release and replaces
  the running executable atomically.
- `relay` short command — declared alongside `agent-relay` in the
  PyPI package, so the canonical short name works regardless of install
  method.
- `--version` / `-V` flag on the root parser.
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

[Unreleased]: https://github.com/bethvourc/agent--relay/compare/v0.7.6...HEAD
[0.7.6]: https://github.com/bethvourc/agent--relay/releases/tag/v0.7.6
[0.7.5]: https://github.com/bethvourc/agent--relay/releases/tag/v0.7.5
[0.7.0]: https://github.com/bethvourc/agent--relay/releases/tag/v0.7.0
[0.6.3]: https://github.com/bethvourc/agent--relay/releases/tag/v0.6.3
[0.6.2]: https://github.com/bethvourc/agent--relay/releases/tag/v0.6.2
[0.6.1]: https://github.com/bethvourc/agent--relay/releases/tag/v0.6.1
[0.6.0]: https://github.com/bethvourc/agent--relay/releases/tag/v0.6.0
[0.5.6]: https://github.com/bethvourc/agent--relay/releases/tag/v0.5.6
[0.5.5]: https://github.com/bethvourc/agent--relay/releases/tag/v0.5.5
[0.5.0]: https://github.com/bethvourc/agent--relay/releases/tag/v0.5.0
[0.4.0]: https://github.com/bethvourc/agent--relay/releases/tag/v0.4.0
[0.3.0]: https://github.com/bethvourc/agent--relay/releases/tag/v0.3.0
[0.2.0]: https://github.com/bethvourc/agent--relay/releases/tag/v0.2.0
[0.1.0]: https://github.com/bethvourc/agent--relay/releases/tag/v0.1.0
