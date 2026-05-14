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

## [0.5.5]

- Last release before the public-mirror split.

[Unreleased]: https://github.com/bethvourc/agent--relay/compare/v0.5.6...HEAD
[0.5.6]: https://github.com/bethvourc/agent--relay/releases/tag/v0.5.6
[0.5.5]: https://github.com/bethvourc/agent--relay/releases/tag/v0.5.5
