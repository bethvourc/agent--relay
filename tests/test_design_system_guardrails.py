"""Phase 6 guardrails for the Agent Relay design system.

These tests fail if the codebase drifts from the conventions established
in ``Agent Relay Design System/`` — literal Rich color names appearing
outside the theme module, missing tokens, or banner/help losing their
DS-shaped structure.
"""

from __future__ import annotations

import re
from io import StringIO
from pathlib import Path
from unittest import TestCase

from rich.console import Console

from agent_relay import tokens as T
from agent_relay.ui import RELAY_THEME, create_console, render_help

SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "agent_relay"

# Files allowed to use literal Rich color names — the theme module
# (which translates them) and the tokens module (raw constants).
_THEME_FILES = {"ui.py", "tokens.py"}

# Literal Rich color names we're trying to keep out of consumer code.
# Matches a quoted color name that isn't part of a longer identifier.
_LITERAL_COLOR_RE = re.compile(
    r'"\s*(bold\s+|dim\s+)?'
    r"(cyan|magenta|yellow|red|green|blue|white|black)"
    r'(\s+\w+)?\s*"'
)


class LiteralColorGuardrail(TestCase):
    def test_no_literal_rich_colors_outside_theme_files(self) -> None:
        offenders: list[str] = []
        for path in SRC_ROOT.rglob("*.py"):
            if path.name in _THEME_FILES:
                continue
            text = path.read_text(encoding="utf-8")
            for line_no, line in enumerate(text.splitlines(), start=1):
                if _LITERAL_COLOR_RE.search(line):
                    offenders.append(f"{path.relative_to(SRC_ROOT)}:{line_no}: {line.strip()}")

        self.assertEqual(
            offenders,
            [],
            "literal Rich color names found outside ui.py/tokens.py — "
            "use a theme token instead:\n  " + "\n  ".join(offenders),
        )


class RequiredThemeTokens(TestCase):
    """Every CSS variable in the design system must have a Rich theme key."""

    REQUIRED = (
        # accents
        "brand",
        "brand.dim",
        "signal",
        "signal.dim",
        # surfaces and rules
        "surface.0",
        "surface.1",
        "surface.2",
        "surface.3",
        "surface.rule",
        # foreground ramp
        "fg.1",
        "fg.2",
        "fg.3",
        "fg.4",
        # semantic
        "success",
        "error",
        "warning",
        "info",
        # event kinds (watch_ui)
        "kind.journal",
        "kind.workspace",
        "kind.turn",
        "kind.turn_started",
        "kind.turn_completed",
        "kind.output",
        "kind.status",
        "kind.heartbeat",
        # agents
        "agent.claude",
        "agent.codex",
        "agent.gemini",
    )

    def test_all_required_tokens_present(self) -> None:
        missing = [name for name in self.REQUIRED if name not in RELAY_THEME.styles]
        self.assertEqual(missing, [], f"missing theme tokens: {missing}")


class TokensModuleHexValues(TestCase):
    """Critical hex values match the design-system CSS source of truth."""

    def test_brand_and_signal_match_css(self) -> None:
        self.assertEqual(T.BRAND, "#FFB000")
        self.assertEqual(T.BRAND_DIM, "#B87A00")
        self.assertEqual(T.SIGNAL, "#7EE34B")
        self.assertEqual(T.SURFACE_1, "#121212")
        self.assertEqual(T.FG_1, "#ededed")


class HelpStructureGuardrail(TestCase):
    """`agent-relay --help` should read like ``man``: synopsis, commands, options, aliases."""

    def _render(self, width: int = 120) -> str:
        console = Console(theme=RELAY_THEME, width=width, file=StringIO(), record=True)
        render_help(console)
        return console.export_text()

    def test_wide_help_has_man_shaped_sections(self) -> None:
        out = self._render(width=120)
        for section in ("synopsis", "commands", "workflows", "options", "aliases"):
            self.assertIn(section, out, f"help missing section: {section}")
        # No marketing prose: the deprecated tagline should be gone.
        self.assertNotIn("Capture context, hand off cleanly", out)

    def test_help_lists_watch_and_metrics_commands(self) -> None:
        out = self._render(width=120)
        for cmd in ("watch", "metrics", "metrics-tail", "metrics-serve"):
            self.assertIn(cmd, out, f"help missing command: {cmd}")

    def test_help_is_derived_from_argparse_tree(self) -> None:
        """Every subparser must appear in the live --help output."""
        from agent_relay.cli import build_parser, iter_commands
        from agent_relay.ui import render_help

        parser = build_parser()
        rows = iter_commands(parser)
        usages = {usage for usage, _ in rows}
        self.assertIn("agent-relay <agent>", usages)
        for cmd in (
            "status",
            "deactivate",
            "watch",
            "metrics",
            "metrics-tail",
            "metrics-serve",
            "run",
            "chat",
            "race",
            "resolve",
            "inspect-conflicts",
            "discover",
            "clean",
        ):
            self.assertIn(f"agent-relay {cmd}", usages, f"iter_commands missing {cmd}")

        # And confirm the rendered help reflects them.
        console = Console(theme=RELAY_THEME, width=120, file=StringIO(), record=True)
        render_help(console, commands=rows)
        out = console.export_text()
        for usage, _ in rows:
            self.assertIn(usage, out)

    def test_help_explains_continue_and_end_session_workflows(self) -> None:
        out = self._render(width=120)
        self.assertIn("run --continue", out)
        self.assertIn("chat --continue", out)
        self.assertIn("deactivate <session-id>", out)
        self.assertIn("Ask another question", out)
        self.assertIn("completed/inactive", out)


class StatusBadgeRoutingGuardrail(TestCase):
    """Cross-session metrics renders status through `status_badge()` so the
    DS status vocabulary stays consistent with the dashboard / inspect view."""

    def test_cross_session_metrics_renders_status_badge(self) -> None:
        from agent_relay.metrics import (
            CrossSessionMetrics,
            SessionMetrics,
            TokenUsage,
        )
        from agent_relay.metrics_ui import render_cross_session_metrics
        from agent_relay.ui import STATUS_SYMBOLS

        sm = SessionMetrics(
            session_id="s-active",
            current_agent="claude",
            current_status="active",
            objective="o",
            started_at=None,
            updated_at=None,
            turn_count=1,
            successful_turns=1,
            total_tokens=TokenUsage(input=1, output=1),
            total_cost_usd=0.0,
            total_duration_ms=1,
            by_agent={},
            cost_by_agent={},
            turns=(),
        )
        cs = CrossSessionMetrics(
            sessions=(sm,),
            by_agent={},
            cost_by_agent={},
            by_day={},
            total_tokens=TokenUsage(input=1, output=1),
            total_cost_usd=0.0,
            total_duration_ms=1,
            session_count=1,
        )
        console = Console(theme=RELAY_THEME, width=120, file=StringIO(), record=True)
        render_cross_session_metrics(console, cs)
        out = console.export_text()
        # status_badge('active') prepends the canonical glyph from STATUS_SYMBOLS
        self.assertIn(STATUS_SYMBOLS["active"], out)


class WatchKindStyleUsesTokens(TestCase):
    """The watch event-kind map must reference theme tokens, not literals."""

    def test_kind_style_values_are_token_names(self) -> None:
        from agent_relay.watch_ui import _KIND_STYLE

        for kind, style in _KIND_STYLE.items():
            self.assertTrue(
                style.startswith("kind."),
                f"_KIND_STYLE[{kind!r}] = {style!r} is not a theme token",
            )


class FormattingHelpers(TestCase):
    """DS-spec number formatting."""

    def test_padded_cost_and_durations(self) -> None:
        from agent_relay.formatting import fmt_cost, fmt_duration_ms, fmt_int

        self.assertEqual(fmt_cost(0.0042), "$0.0042")
        self.assertEqual(fmt_cost(None), "-")
        self.assertEqual(fmt_int(1234), "1,234")
        self.assertEqual(fmt_duration_ms(45), "45ms")
        self.assertEqual(fmt_duration_ms(133_000), "2m13s")
        self.assertEqual(fmt_duration_ms(3_725_000), "1h02m05s")


class _SmokeRenderHelp(TestCase):
    """Help renders end-to-end without raising — guards against banner/icon
    desync after layout edits."""

    def test_help_renders(self) -> None:
        console = create_console()
        console.file = StringIO()
        console.width = 120
        render_help(console)
