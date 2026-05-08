"""Design system tokens — single source of truth for Agent Relay's palette.

Mirrors the CSS custom properties in
``Agent Relay Design System/colors_and_type.css``. Rich theme entries in
:mod:`agent_relay.ui` reference these constants so the TUI and the
dashboard stay in lockstep.

Two accents only: ``BRAND`` (amber) and ``SIGNAL`` (green). Everything
else is grayscale or a semantic role.
"""

from __future__ import annotations

# Surfaces (dark, default)
SURFACE_0 = "#0a0a0a"
SURFACE_1 = "#121212"
SURFACE_2 = "#1a1a1a"
SURFACE_3 = "#242424"
SURFACE_RULE = "#2e2e2e"

# Foreground ramp
FG_1 = "#ededed"
FG_2 = "#b8b8b8"
FG_3 = "#7a7a7a"
FG_4 = "#555555"

# Brand (amber)
BRAND = "#FFB000"
BRAND_DIM = "#B87A00"

# Signal (green) — live / heartbeat / on-air only
SIGNAL = "#7EE34B"
SIGNAL_DIM = "#4FA82A"

# Semantic roles
SUCCESS = "#44d46a"
ERROR = "#ff5c5c"
WARNING = "#ffd24a"
INFO = "#5fb3ff"

# Agent palette
AGENT_CLAUDE = BRAND
AGENT_CODEX = "#5cd9d9"
AGENT_GEMINI = "#4285F4"

# Event kinds (watch_ui)
KIND_JOURNAL = AGENT_CODEX
KIND_WORKSPACE = "#c97cd6"
KIND_TURN = BRAND
KIND_OUTPUT = FG_1
KIND_STATUS = WARNING
KIND_HEARTBEAT = FG_3
