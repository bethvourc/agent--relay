"""Tests for the Prometheus metrics exporter."""

from __future__ import annotations

import argparse
import http.client
import json
import socket
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import MagicMock, patch

from agent_relay.cli import build_parser, cmd_metrics_serve
from agent_relay.exporters.prometheus import (
    render_prometheus_text,
    serve_prometheus,
)
from agent_relay.layout import (
    derived_view_path,
    turn_dir,
    turns_dir,
)
from agent_relay.metrics import (
    CrossSessionMetrics,
    SessionMetrics,
    TokenUsage,
    TurnMetrics,
)
from agent_relay.ui import create_console


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _scaffold(repo: Path, sid: str) -> None:
    turns_dir(repo, sid).mkdir(parents=True, exist_ok=True)
    derived_view_path(repo, sid).parent.mkdir(parents=True, exist_ok=True)


def _write_view(repo: Path, sid: str, **fields) -> None:
    base = {
        "session_id": sid,
        "current_agent": "claude",
        "current_status": "active",
        "objective": "test",
        "created_at": "2026-05-01T10:00:00.000Z",
        "updated_at": "2026-05-01T10:30:00.000Z",
    }
    base.update(fields)
    derived_view_path(repo, sid).write_text(json.dumps(base), encoding="utf-8")


def _write_turn(repo: Path, sid: str, n: int, agent: str = "claude") -> None:
    tdir = turn_dir(repo, sid, n)
    tdir.mkdir(parents=True, exist_ok=True)
    state = {
        "agent_key": agent,
        "turn_number": n,
        "status": "continue",
        "metadata": {
            "started_at": "2026-05-01T10:00:00.000Z",
            "finished_at": "2026-05-01T10:00:42.000Z",
        },
    }
    if agent == "claude":
        result = {
            "type": "result",
            "duration_ms": 42000,
            "total_cost_usd": 0.4318,
            "usage": {"input_tokens": 6, "output_tokens": 56},
        }
    else:
        result = {
            "type": "result",
            "token_usage": {"input_tokens": 5, "output_tokens": 7},
        }
    (tdir / "output.jsonl").write_text(json.dumps(result) + "\n", encoding="utf-8")
    (tdir / "state.json").write_text(json.dumps(state), encoding="utf-8")


def _build_metrics() -> CrossSessionMetrics:
    """Build CrossSessionMetrics directly without filesystem I/O."""
    turn1 = TurnMetrics(
        session_id="s1",
        turn_number=1,
        agent="claude",
        model="claude-opus",
        started_at="2026-05-01T10:00:00.000Z",
        finished_at="2026-05-01T10:00:42.000Z",
        duration_ms=42000,
        api_duration_ms=31000,
        tokens=TokenUsage(input=10, output=20, cache_read=5, cache_creation=3),
        cost_usd=0.4318,
        tool_calls=2,
        status="continue",
        succeeded=True,
    )
    turn2 = TurnMetrics(
        session_id="s1",
        turn_number=2,
        agent="claude",
        model="claude-opus",
        started_at=None,
        finished_at=None,
        duration_ms=10000,
        api_duration_ms=None,
        tokens=TokenUsage(input=4, output=6),
        cost_usd=0.05,
        tool_calls=0,
        status="error",
        succeeded=False,
    )
    sm = SessionMetrics(
        session_id="s1",
        current_agent="claude",
        current_status="active",
        objective="o",
        started_at="2026-05-01T10:00:00.000Z",
        updated_at="2026-05-01T10:30:00.000Z",
        turn_count=2,
        successful_turns=1,
        total_tokens=TokenUsage(input=14, output=26, cache_read=5, cache_creation=3),
        total_cost_usd=0.4818,
        total_duration_ms=52000,
        by_agent={
            "claude": TokenUsage(
                input=14, output=26, cache_read=5, cache_creation=3
            )
        },
        cost_by_agent={"claude": 0.4818},
        turns=(turn1, turn2),
    )
    return CrossSessionMetrics(
        sessions=(sm,),
        by_agent={
            "claude": TokenUsage(input=14, output=26, cache_read=5, cache_creation=3)
        },
        cost_by_agent={"claude": 0.4818},
        by_day={},
        total_tokens=TokenUsage(input=14, output=26, cache_read=5, cache_creation=3),
        total_cost_usd=0.4818,
        total_duration_ms=52000,
        session_count=1,
    )


# ---------------------------------------------------------------------------
# Text format rendering
# ---------------------------------------------------------------------------


class RenderPrometheusTextTests(TestCase):
    def test_includes_help_and_type_lines(self) -> None:
        text = render_prometheus_text(_build_metrics())
        self.assertIn("# HELP agent_relay_tokens_total", text)
        self.assertIn("# TYPE agent_relay_tokens_total counter", text)
        self.assertIn("# HELP agent_relay_cost_usd_total", text)
        self.assertIn("# TYPE agent_relay_session_active gauge", text)

    def test_token_samples_per_direction(self) -> None:
        text = render_prometheus_text(_build_metrics())
        self.assertIn(
            'agent_relay_tokens_total{agent="claude",direction="input"} 14',
            text,
        )
        self.assertIn(
            'agent_relay_tokens_total{agent="claude",direction="output"} 26',
            text,
        )
        self.assertIn(
            'agent_relay_tokens_total{agent="claude",direction="cache_read"} 5',
            text,
        )

    def test_cost_sample(self) -> None:
        text = render_prometheus_text(_build_metrics())
        self.assertRegex(
            text, r'agent_relay_cost_usd_total\{agent="claude"\} 0\.4818'
        )

    def test_duration_summary_sum_and_count(self) -> None:
        text = render_prometheus_text(_build_metrics())
        self.assertIn(
            'agent_relay_turn_duration_ms_sum{agent="claude"} 52000', text
        )
        self.assertIn(
            'agent_relay_turn_duration_ms_count{agent="claude"} 2', text
        )

    def test_outcome_breakdown_per_agent(self) -> None:
        text = render_prometheus_text(_build_metrics())
        self.assertIn(
            'agent_relay_turns_total{agent="claude",result="success"} 1', text
        )
        self.assertIn(
            'agent_relay_turns_total{agent="claude",result="error"} 1', text
        )

    def test_session_active_gauge(self) -> None:
        text = render_prometheus_text(_build_metrics())
        self.assertIn("agent_relay_session_active 1", text)
        self.assertIn(
            'agent_relay_sessions_total{status="active"} 1', text
        )

    def test_empty_metrics_renders_only_metadata(self) -> None:
        text = render_prometheus_text(CrossSessionMetrics(sessions=()))
        self.assertIn("agent_relay_session_active 0", text)
        # Should still have HELP/TYPE lines.
        self.assertIn("# HELP agent_relay_tokens_total", text)

    def test_label_value_escaping(self) -> None:
        sm = SessionMetrics(
            session_id="weird",
            current_agent='odd"name',
            current_status="active",
            objective=None,
            started_at=None,
            updated_at=None,
            turn_count=0,
            successful_turns=0,
            total_tokens=TokenUsage(),
            total_cost_usd=None,
            total_duration_ms=0,
            by_agent={'odd"name': TokenUsage(input=1)},
            cost_by_agent={},
            turns=(),
        )
        cm = CrossSessionMetrics(
            sessions=(sm,),
            by_agent={'odd"name': TokenUsage(input=1)},
        )
        text = render_prometheus_text(cm)
        self.assertIn(r'agent="odd\"name"', text)


# ---------------------------------------------------------------------------
# Live server (ephemeral port)
# ---------------------------------------------------------------------------


class ServePrometheusEndpointTests(TestCase):
    def _free_port(self) -> int:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def test_metrics_endpoint_serves_text_format(self) -> None:
        port = self._free_port()
        ready = threading.Event()
        captured: dict[str, object] = {}
        cm = _build_metrics()

        class _CapturingServer(ThreadingHTTPServer):
            def __init__(self, address, handler) -> None:
                super().__init__(address, handler)
                captured["server"] = self
                ready.set()

        rc_holder: dict[str, int] = {}

        def run() -> None:
            rc_holder["rc"] = serve_prometheus(
                Path("."),
                "127.0.0.1",
                port,
                refresh_interval=0.0,
                extractor=lambda _repo: cm,
                server_factory=_CapturingServer,
            )

        t = threading.Thread(target=run, daemon=True)
        t.start()
        ready.wait(timeout=5)
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/metrics")
            resp = conn.getresponse()
            body = resp.read().decode("utf-8")
            self.assertEqual(resp.status, 200)
            self.assertIn("text/plain", resp.getheader("Content-Type", ""))
            self.assertIn("agent_relay_tokens_total", body)

            # Unknown path should 404.
            conn.request("GET", "/other")
            resp2 = conn.getresponse()
            resp2.read()
            self.assertEqual(resp2.status, 404)
            conn.close()
        finally:
            server = captured["server"]
            server.shutdown()  # type: ignore[attr-defined]
            t.join(timeout=5)


# ---------------------------------------------------------------------------
# CLI handler
# ---------------------------------------------------------------------------


def _make_serve_args(
    *,
    prometheus: str | None = None,
    otlp: str | None = None,
    repo: str = ".",
    refresh: float = 5.0,
) -> argparse.Namespace:
    return argparse.Namespace(
        prometheus=prometheus,
        prometheus_refresh=refresh,
        otlp=otlp,
        otlp_header=[],
        otlp_interval=30.0,
        repo=repo,
        json=False,
        quiet=False,
        console=create_console(),
    )


class CmdMetricsServeTests(TestCase):
    def test_subparser_registered(self) -> None:
        parser = build_parser()
        ns = parser.parse_args(
            ["metrics-serve", "--prometheus", ":9464", "--prometheus-refresh", "1.5"]
        )
        self.assertEqual(ns.command, "metrics-serve")
        self.assertEqual(ns.prometheus, ":9464")
        self.assertAlmostEqual(ns.prometheus_refresh, 1.5)

    def test_errors_when_no_target_given(self) -> None:
        rc = cmd_metrics_serve(_make_serve_args())
        self.assertEqual(rc, 2)

    def test_errors_on_malformed_prometheus_address(self) -> None:
        rc = cmd_metrics_serve(_make_serve_args(prometheus="not-an-address"))
        self.assertEqual(rc, 2)

    def test_dispatches_to_serve_prometheus(self) -> None:
        with TemporaryDirectory() as tmp:
            args = _make_serve_args(prometheus="127.0.0.1:9464", repo=tmp)
            serve_mock = MagicMock(return_value=0)
            with patch("agent_relay.cli.serve_prometheus", serve_mock):
                rc = cmd_metrics_serve(args)
        self.assertEqual(rc, 0)
        serve_mock.assert_called_once()
        call = serve_mock.call_args
        self.assertEqual(call.args[1], "127.0.0.1")
        self.assertEqual(call.args[2], 9464)
