"""Tests for the metrics-serve HTML dashboard."""

from __future__ import annotations

import http.client
import threading
import time
from contextlib import closing
from http.server import ThreadingHTTPServer
from unittest import TestCase

from agent_relay.dashboard import render_dashboard_html
from agent_relay.exporters.prometheus import serve_prometheus
from agent_relay.metrics import (
    CrossSessionMetrics,
    SessionMetrics,
    TokenUsage,
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
        """Auto-refresh is off by default — no meta-refresh, no setInterval
        running until the user ticks the live toggle. Manual reload + live
        checkbox + stale-age indicator are present in the header."""
        html = render_dashboard_html(_build_sample_metrics())
        self.assertNotIn('http-equiv="refresh"', html)
        self.assertIn("data-refresh-now", html)
        self.assertIn('name="live"', html)
        self.assertIn("data-stale", html)
        self.assertIn("localStorage", html)


class DashboardRoutingTests(TestCase):
    """End-to-end: serve_prometheus mounts /, /dashboard, and /metrics."""

    def _start_server(self) -> tuple[ThreadingHTTPServer, threading.Thread, int]:
        captured: dict[str, ThreadingHTTPServer] = {}

        def factory(addr, handler):
            server = ThreadingHTTPServer(addr, handler)
            captured["server"] = server
            return server

        thread = threading.Thread(
            target=serve_prometheus,
            kwargs={
                "repo_root": __import__("pathlib").Path("."),
                "host": "127.0.0.1",
                "port": 0,
                "refresh_interval": 0.1,
                "extractor": lambda _: _build_sample_metrics(),
                "server_factory": factory,
            },
            daemon=True,
        )
        thread.start()

        # Wait for server to come up (factory called synchronously inside serve_prometheus)
        for _ in range(50):
            if "server" in captured:
                break
            time.sleep(0.01)
        self.assertIn("server", captured, "server never started")
        port = captured["server"].server_address[1]
        return captured["server"], thread, port

    def _stop(self, server, thread) -> None:
        server.shutdown()
        thread.join(timeout=2.0)

    def _get(self, port: int, path: str) -> tuple[int, str, str]:
        with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=2)) as conn:
            conn.request("GET", path)
            resp = conn.getresponse()
            body = resp.read().decode("utf-8")
            return resp.status, resp.getheader("Content-Type", ""), body

    def test_metrics_path_serves_prometheus_text(self) -> None:
        server, thread, port = self._start_server()
        try:
            status, content_type, body = self._get(port, "/metrics")
            self.assertEqual(status, 200)
            self.assertIn("text/plain", content_type)
            self.assertIn("agent_relay_tokens_total", body)
        finally:
            self._stop(server, thread)

    def test_root_serves_html_dashboard(self) -> None:
        server, thread, port = self._start_server()
        try:
            status, content_type, body = self._get(port, "/")
            self.assertEqual(status, 200)
            self.assertIn("text/html", content_type)
            self.assertIn("<!doctype html>", body)
            self.assertIn("Agent Relay", body)
        finally:
            self._stop(server, thread)

    def test_dashboard_alias_is_equivalent_to_root(self) -> None:
        server, thread, port = self._start_server()
        try:
            _, _, body_root = self._get(port, "/")
            _, _, body_dash = self._get(port, "/dashboard")
            self.assertEqual(body_root, body_dash)
        finally:
            self._stop(server, thread)

    def test_unknown_path_returns_404(self) -> None:
        server, thread, port = self._start_server()
        try:
            status, _, _ = self._get(port, "/nope")
            self.assertEqual(status, 404)
        finally:
            self._stop(server, thread)
