"""Phase F production hardening tests."""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest import TestCase

from agent_relay.exporters.prometheus import (
    DashboardTelemetry,
    is_loopback_bind,
    serve_prometheus,
)
from agent_relay.metrics import CrossSessionMetrics, SessionMetrics, TokenUsage
from tests._dashboard_test_helpers import (
    get_dashboard,
    get_dashboard_response,
    start_dashboard_server,
    stop_dashboard_server,
)


def _sample_metrics() -> CrossSessionMetrics:
    sm = SessionMetrics(
        session_id="s1",
        current_agent="claude",
        current_status="active",
        objective="o",
        started_at=None,
        updated_at="2026-05-09T10:00:00Z",
        turn_count=0,
        successful_turns=0,
        total_tokens=TokenUsage(),
        total_cost_usd=None,
        total_duration_ms=0,
        turns=(),
    )
    return CrossSessionMetrics(
        sessions=(sm,),
        by_agent={"claude": TokenUsage()},
        cost_by_agent={"claude": 0.0},
        by_day={},
        total_tokens=TokenUsage(),
        total_cost_usd=None,
        total_duration_ms=0,
        session_count=1,
    )


class IsLoopbackBindTests(TestCase):
    def test_loopback_hosts_recognised(self) -> None:
        for host in ("127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1", "LOCALHOST"):
            self.assertTrue(is_loopback_bind(host), host)

    def test_remote_hosts_rejected(self) -> None:
        for host in ("0.0.0.0", "192.168.1.5", "example.com"):
            self.assertFalse(is_loopback_bind(host), host)


class BindGateTests(TestCase):
    def test_remote_bind_without_allow_remote_raises(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            serve_prometheus(
                Path("."),
                "0.0.0.0",
                0,
                allow_remote=False,
            )
        self.assertIn("refusing to bind", str(ctx.exception))

    def test_loopback_bind_does_not_raise_on_validation(self) -> None:
        # Use start_dashboard_server which actually starts a server.
        server, thread, port = start_dashboard_server(
            self,
            repo_root=Path("."),
            extractor=lambda _: _sample_metrics(),
        )
        try:
            status, _ = get_dashboard(port, "/metrics")[:2]
            self.assertEqual(status, 200)
        finally:
            stop_dashboard_server(server, thread)


class SecurityHeaderTests(TestCase):
    def setUp(self) -> None:
        self.server, self.thread, self.port = start_dashboard_server(
            self,
            repo_root=Path("."),
            extractor=lambda _: _sample_metrics(),
        )

    def tearDown(self) -> None:
        stop_dashboard_server(self.server, self.thread)

    def test_html_route_carries_csp_and_no_store(self) -> None:
        status, headers, _ = get_dashboard_response(self.port, "/")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(headers.get("Referrer-Policy"), "no-referrer")
        self.assertEqual(headers.get("X-Frame-Options"), "DENY")
        csp = headers.get("Content-Security-Policy", "")
        self.assertIn("default-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("fonts.googleapis.com", csp)

    def test_json_data_route_no_store_no_csp(self) -> None:
        _status, headers, _ = get_dashboard_response(self.port, "/dashboard/data")
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        # JSON does not need a CSP header (no document context).
        self.assertNotIn("Content-Security-Policy", headers)

    def test_metrics_route_uses_no_cache_not_no_store(self) -> None:
        _status, headers, _ = get_dashboard_response(self.port, "/metrics")
        self.assertEqual(headers.get("Cache-Control"), "no-cache")


class NoDashboardModeTests(TestCase):
    def setUp(self) -> None:
        self.server, self.thread, self.port = start_dashboard_server(
            self,
            repo_root=Path("."),
            extractor=lambda _: _sample_metrics(),
            dashboard_enabled=False,
        )

    def tearDown(self) -> None:
        stop_dashboard_server(self.server, self.thread)

    def test_metrics_still_served(self) -> None:
        status, _, body = get_dashboard(self.port, "/metrics")
        self.assertEqual(status, 200)
        self.assertIn("agent_relay_session_active", body)

    def test_dashboard_html_returns_404(self) -> None:
        status, _, _ = get_dashboard(self.port, "/")
        self.assertEqual(status, 404)

    def test_dashboard_data_returns_404(self) -> None:
        status, _, _ = get_dashboard(self.port, "/dashboard/data")
        self.assertEqual(status, 404)

    def test_session_route_returns_404(self) -> None:
        status, _, _ = get_dashboard(self.port, "/session/anything")
        self.assertEqual(status, 404)


class AccessLogTests(TestCase):
    def test_jsonl_record_per_request(self) -> None:
        sink = io.StringIO()
        server, thread, port = start_dashboard_server(
            self,
            repo_root=Path("."),
            extractor=lambda _: _sample_metrics(),
            access_log=sink,
        )
        try:
            get_dashboard(port, "/metrics")
            get_dashboard(port, "/")
        finally:
            stop_dashboard_server(server, thread)
        lines = [line for line in sink.getvalue().splitlines() if line]
        self.assertEqual(len(lines), 2)
        first = json.loads(lines[0])
        for key in ("ts", "path", "status", "ms", "len"):
            self.assertIn(key, first)
        paths = sorted(json.loads(line)["path"] for line in lines)
        self.assertEqual(paths, ["/", "/metrics"])


class SelfMetricsTests(TestCase):
    def test_dashboard_requests_counter_appears_in_metrics(self) -> None:
        server, thread, port = start_dashboard_server(
            self,
            repo_root=Path("."),
            refresh_interval=0.0,
            extractor=lambda _: _sample_metrics(),
        )
        try:
            get_dashboard(port, "/")
            get_dashboard(port, "/")
            _status, _, body = get_dashboard(port, "/metrics")
        finally:
            stop_dashboard_server(server, thread)
        self.assertIn("agent_relay_dashboard_requests_total", body)
        self.assertIn('path="/"', body)
        self.assertIn("agent_relay_dashboard_render_duration_ms_count", body)
        self.assertIn("agent_relay_dashboard_sse_connections", body)

    def test_path_template_buckets_dynamic_session_ids(self) -> None:
        telemetry = DashboardTelemetry()
        telemetry.record_request(path="/session/{id}", status=200, duration_ms=1.2)
        telemetry.record_request(path="/session/{id}", status=200, duration_ms=2.4)
        lines = telemetry.render_prometheus_lines()
        joined = "\n".join(lines)
        self.assertIn('path="/session/{id}",status="200"} 2', joined)

    def test_sse_connection_gauge_tracks_increment_decrement(self) -> None:
        telemetry = DashboardTelemetry()
        telemetry.sse_connected()
        telemetry.sse_connected()
        telemetry.sse_disconnected()
        joined = "\n".join(telemetry.render_prometheus_lines())
        self.assertIn("agent_relay_dashboard_sse_connections 1", joined)


class CSPDoesNotBlockInlineStyleTests(TestCase):
    """Sanity check — the dashboard *uses* an inline ``<style>`` block, so
    the CSP must keep ``style-src 'unsafe-inline'``. Loose this and the
    dashboard goes blank in any modern browser."""

    def test_csp_keeps_unsafe_inline_for_styles(self) -> None:
        server, thread, port = start_dashboard_server(
            self,
            repo_root=Path("."),
            extractor=lambda _: _sample_metrics(),
        )
        try:
            _status, headers, _ = get_dashboard_response(port, "/")
        finally:
            stop_dashboard_server(server, thread)
        csp = headers.get("Content-Security-Policy", "")
        self.assertIn("style-src", csp)
        self.assertIn("'unsafe-inline'", csp)
