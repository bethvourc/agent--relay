"""Tests for the metrics-serve HTML dashboard."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest import TestCase

from agent_relay.dashboard import render_dashboard_html, render_dashboard_update_payload
from agent_relay.metrics import (
    CrossSessionMetrics,
    SessionMetrics,
    TokenUsage,
)
from tests._dashboard_test_helpers import (
    get_dashboard,
    start_dashboard_server,
    stop_dashboard_server,
)


def _build_sample_metrics() -> CrossSessionMetrics:
    sm = SessionMetrics(
        session_id="20260507-101010-aae0fb",
        current_agent="claude",
        current_status="active",
        objective="o",
        started_at=None,
        updated_at="2026-05-07T10:11:00.000Z",
        turn_count=2,
        successful_turns=2,
        total_tokens=TokenUsage(input=10, output=20),
        total_cost_usd=0.1841,
        total_duration_ms=2600,
        by_agent={"claude": TokenUsage(input=10, output=20)},
        cost_by_agent={"claude": 0.1841},
        turns=(),
    )
    return CrossSessionMetrics(
        sessions=(sm,),
        by_agent={"claude": TokenUsage(input=10, output=20)},
        cost_by_agent={"claude": 0.1841},
        by_day={"2026-05-07": TokenUsage(input=10, output=20)},
        total_tokens=TokenUsage(input=10, output=20),
        total_cost_usd=0.1841,
        total_duration_ms=2600,
        session_count=1,
    )


class DashboardRendererTests(TestCase):
    def test_emits_complete_html_document(self) -> None:
        html = render_dashboard_html(_build_sample_metrics())
        self.assertTrue(html.startswith("<!doctype html>"))
        self.assertIn("</html>", html)

    def test_includes_design_system_tokens_and_no_marketing(self) -> None:
        html = render_dashboard_html(_build_sample_metrics())
        # Brand and signal hex values from tokens.py are inlined.
        self.assertIn("#FFB000", html)
        self.assertIn("#7EE34B", html)
        self.assertIn("#121212", html)
        # Mono-first typography.
        self.assertIn("JetBrains Mono", html)
        # Lowercase headings per DS.
        self.assertIn(">totals<", html)
        self.assertIn(">sessions<", html)
        # No banned patterns.
        self.assertNotIn("Capture context, hand off cleanly", html)

    def test_renders_session_row_with_brand_id_and_status_glyph(self) -> None:
        html = render_dashboard_html(_build_sample_metrics())
        self.assertIn("20260507-101010-aae0fb", html)
        # active → ● glyph
        self.assertIn("●", html)
        # Numbers formatted via formatting.fmt_*
        self.assertIn("$0.1841", html)
        self.assertIn("2.6s", html)

    def test_empty_state_renders_clean(self) -> None:
        empty = CrossSessionMetrics(sessions=())
        html = render_dashboard_html(empty)
        self.assertIn("no sessions found", html)
        # Document is still a complete page.
        self.assertIn("</html>", html)

    def test_live_controls_are_opt_in(self) -> None:
        """Auto-refresh is off by default and uses soft refresh when enabled."""
        html = render_dashboard_html(_build_sample_metrics())
        self.assertNotIn('http-equiv="refresh"', html)
        self.assertNotIn("window.location.reload", html)
        self.assertIn("fetch(refreshEndpoint()", html)
        self.assertIn("data-refresh-now", html)
        self.assertIn('name="live"', html)
        self.assertIn("data-stale", html)
        self.assertIn("data-dashboard-region", html)
        self.assertIn("localStorage", html)

    def test_generated_at_is_emitted_for_real_stale_age(self) -> None:
        html = render_dashboard_html(
            _build_sample_metrics(),
            generated_at=datetime(2026, 5, 7, 10, 11, 0, tzinfo=UTC),
        )
        self.assertIn("2026-05-07 10:11:00 UTC", html)
        self.assertIn('data-generated-at="2026-05-07T10:11:00Z"', html)

    def test_update_payload_contains_patchable_regions(self) -> None:
        payload = render_dashboard_update_payload(
            _build_sample_metrics(),
            generated_at=datetime(2026, 5, 7, 10, 11, 0, tzinfo=UTC),
        )
        self.assertEqual(payload["generatedAt"], "2026-05-07T10:11:00Z")
        regions = payload["regions"]
        assert isinstance(regions, dict)
        self.assertIn("totals", regions)
        self.assertIn("sessions", regions)
        self.assertIn("20260507-101010-aae0fb", regions["sessions"])


class DashboardRoutingTests(TestCase):
    """End-to-end: serve_prometheus mounts /, /dashboard, and /metrics."""

    def _start_server(self):
        return start_dashboard_server(
            self,
            repo_root=Path("."),
            extractor=lambda _: _build_sample_metrics(),
        )

    def test_metrics_path_serves_prometheus_text(self) -> None:
        server, thread, port = self._start_server()
        try:
            status, content_type, body = get_dashboard(port, "/metrics")
            self.assertEqual(status, 200)
            self.assertIn("text/plain", content_type)
            self.assertIn("agent_relay_tokens_total", body)
        finally:
            stop_dashboard_server(server, thread)

    def test_root_serves_html_dashboard(self) -> None:
        server, thread, port = self._start_server()
        try:
            status, content_type, body = get_dashboard(port, "/")
            self.assertEqual(status, 200)
            self.assertIn("text/html", content_type)
            self.assertIn("<!doctype html>", body)
            self.assertIn("Agent Relay", body)
        finally:
            stop_dashboard_server(server, thread)

    def test_dashboard_data_serves_json_refresh_payload(self) -> None:
        server, thread, port = self._start_server()
        try:
            status, content_type, body = get_dashboard(port, "/dashboard/data")
            self.assertEqual(status, 200)
            self.assertIn("application/json", content_type)
            payload = json.loads(body)
            self.assertIn("generatedAt", payload)
            self.assertIn("regions", payload)
            self.assertIn("20260507-101010-aae0fb", payload["regions"]["sessions"])
        finally:
            stop_dashboard_server(server, thread)

    def test_dashboard_alias_is_equivalent_to_root(self) -> None:
        server, thread, port = self._start_server()
        try:
            _, _, body_root = get_dashboard(port, "/")
            _, _, body_dash = get_dashboard(port, "/dashboard")
            self.assertEqual(body_root, body_dash)
        finally:
            stop_dashboard_server(server, thread)

    def test_unknown_path_returns_404(self) -> None:
        server, thread, port = self._start_server()
        try:
            status, _, _ = get_dashboard(port, "/nope")
            self.assertEqual(status, 404)
        finally:
            stop_dashboard_server(server, thread)
