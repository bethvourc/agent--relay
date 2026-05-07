"""Tests for the OTLP/HTTP-JSON metrics exporter."""

from __future__ import annotations

import argparse
import io
import json
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import MagicMock, patch

from agent_relay.cli import cmd_metrics_serve
from agent_relay.exporters.otlp import (
    export_otlp,
    render_otlp_payload,
    serve_otlp,
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


def _build_metrics() -> CrossSessionMetrics:
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
        cost_usd=None,
        tool_calls=0,
        status="error",
        succeeded=False,
    )
    sm = SessionMetrics(
        session_id="s1",
        current_agent="claude",
        current_status="active",
        objective="o",
        started_at=None,
        updated_at=None,
        turn_count=2,
        successful_turns=1,
        total_tokens=TokenUsage(input=14, output=26, cache_read=5, cache_creation=3),
        total_cost_usd=0.4318,
        total_duration_ms=52000,
        by_agent={"claude": TokenUsage(input=14, output=26, cache_read=5, cache_creation=3)},
        cost_by_agent={"claude": 0.4318},
        turns=(turn1, turn2),
    )
    return CrossSessionMetrics(
        sessions=(sm,),
        by_agent={"claude": TokenUsage(input=14, output=26, cache_read=5, cache_creation=3)},
        cost_by_agent={"claude": 0.4318},
        by_day={},
        total_tokens=TokenUsage(input=14, output=26, cache_read=5, cache_creation=3),
        total_cost_usd=0.4318,
        total_duration_ms=52000,
        session_count=1,
    )


def _flatten_metric_names(payload: dict) -> list[str]:
    out: list[str] = []
    for rm in payload.get("resourceMetrics", []):
        for sm in rm.get("scopeMetrics", []):
            for m in sm.get("metrics", []):
                out.append(m["name"])
    return out


def _find_metric(payload: dict, name: str) -> dict | None:
    for rm in payload.get("resourceMetrics", []):
        for sm in rm.get("scopeMetrics", []):
            for m in sm.get("metrics", []):
                if m["name"] == name:
                    return m
    return None


# ---------------------------------------------------------------------------
# Payload rendering
# ---------------------------------------------------------------------------


class RenderOtlpPayloadTests(TestCase):
    def test_resource_includes_service_name(self) -> None:
        payload = render_otlp_payload(_build_metrics())
        rm = payload["resourceMetrics"][0]
        attrs = {a["key"]: a["value"] for a in rm["resource"]["attributes"]}
        self.assertEqual(attrs["service.name"], {"stringValue": "agent-relay"})

    def test_scope_metadata(self) -> None:
        payload = render_otlp_payload(_build_metrics())
        scope = payload["resourceMetrics"][0]["scopeMetrics"][0]["scope"]
        self.assertEqual(scope["name"], "agent-relay")
        self.assertIn("version", scope)

    def test_emits_expected_metric_names(self) -> None:
        payload = render_otlp_payload(_build_metrics())
        names = _flatten_metric_names(payload)
        self.assertIn("agent_relay.tokens", names)
        self.assertIn("agent_relay.cost_usd", names)
        self.assertIn("agent_relay.turn_duration_ms", names)
        self.assertIn("agent_relay.turns", names)
        self.assertIn("agent_relay.session.active", names)
        self.assertIn("agent_relay.sessions", names)

    def test_counters_use_cumulative_temporality_and_monotonic(self) -> None:
        payload = render_otlp_payload(_build_metrics())
        for name in ("agent_relay.tokens", "agent_relay.cost_usd", "agent_relay.turns"):
            metric = _find_metric(payload, name)
            self.assertIsNotNone(metric, f"missing metric {name}")
            self.assertIn("sum", metric)
            self.assertEqual(metric["sum"]["aggregationTemporality"], 2)
            self.assertTrue(metric["sum"]["isMonotonic"])

    def test_session_active_is_gauge(self) -> None:
        payload = render_otlp_payload(_build_metrics())
        metric = _find_metric(payload, "agent_relay.session.active")
        self.assertIsNotNone(metric)
        self.assertIn("gauge", metric)
        self.assertEqual(len(metric["gauge"]["dataPoints"]), 1)
        # Gauge value uses asInt string form.
        self.assertEqual(metric["gauge"]["dataPoints"][0]["asInt"], "1")

    def test_token_data_points_carry_attributes(self) -> None:
        payload = render_otlp_payload(_build_metrics())
        tokens = _find_metric(payload, "agent_relay.tokens")
        self.assertIsNotNone(tokens)
        directions = set()
        for point in tokens["sum"]["dataPoints"]:
            attrs = {a["key"]: a["value"] for a in point["attributes"]}
            self.assertEqual(attrs["agent"], {"stringValue": "claude"})
            directions.add(attrs["direction"]["stringValue"])
        self.assertIn("input", directions)
        self.assertIn("output", directions)

    def test_cost_uses_double_value(self) -> None:
        payload = render_otlp_payload(_build_metrics())
        cost = _find_metric(payload, "agent_relay.cost_usd")
        self.assertIsNotNone(cost)
        point = cost["sum"]["dataPoints"][0]
        self.assertIn("asDouble", point)
        self.assertAlmostEqual(point["asDouble"], 0.4318)

    def test_resource_attrs_extension(self) -> None:
        payload = render_otlp_payload(
            _build_metrics(),
            resource_attrs={"deployment.environment": "dev"},
        )
        attrs = {
            a["key"]: a["value"] for a in payload["resourceMetrics"][0]["resource"]["attributes"]
        }
        self.assertEqual(attrs["deployment.environment"], {"stringValue": "dev"})

    def test_empty_metrics_still_emit_session_active_gauge(self) -> None:
        payload = render_otlp_payload(CrossSessionMetrics(sessions=()))
        names = _flatten_metric_names(payload)
        self.assertIn("agent_relay.session.active", names)
        # No counters when nothing was observed.
        self.assertNotIn("agent_relay.tokens", names)


# ---------------------------------------------------------------------------
# export_otlp delivery
# ---------------------------------------------------------------------------


class ExportOtlpTests(TestCase):
    def test_post_to_endpoint_with_headers(self) -> None:
        captured: dict[str, object] = {}

        class _Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=10.0):
            captured["url"] = req.full_url
            captured["body"] = req.data
            captured["headers"] = dict(req.header_items())
            return _Resp()

        with patch(
            "agent_relay.exporters.otlp.urllib_request.urlopen",
            side_effect=fake_urlopen,
        ):
            export_otlp(
                _build_metrics(),
                endpoint="http://collector.example/v1/metrics",
                headers={"X-Token": "abc"},
            )

        self.assertEqual(captured["url"], "http://collector.example/v1/metrics")
        body = json.loads(captured["body"].decode("utf-8"))  # type: ignore[arg-type]
        self.assertIn("resourceMetrics", body)
        keys_lower = {k.lower() for k in captured["headers"]}  # type: ignore[union-attr]
        self.assertIn("x-token", keys_lower)
        self.assertIn("content-type", keys_lower)

    def test_5xx_logged_to_stderr_no_raise(self) -> None:
        from urllib.error import HTTPError

        err = HTTPError("http://x/y", 502, "boom", {}, None)
        stderr_buf = io.StringIO()
        with (
            patch(
                "agent_relay.exporters.otlp.urllib_request.urlopen",
                side_effect=err,
            ),
            patch("agent_relay.exporters.otlp.sys.stderr", stderr_buf),
        ):
            export_otlp(_build_metrics(), endpoint="http://x/y")
        self.assertIn("HTTP 502", stderr_buf.getvalue())

    def test_non_2xx_status_logged(self) -> None:
        class _Resp:
            status = 404

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        stderr_buf = io.StringIO()
        with (
            patch(
                "agent_relay.exporters.otlp.urllib_request.urlopen",
                return_value=_Resp(),
            ),
            patch("agent_relay.exporters.otlp.sys.stderr", stderr_buf),
        ):
            export_otlp(_build_metrics(), endpoint="http://x/y")
        self.assertIn("HTTP 404", stderr_buf.getvalue())


# ---------------------------------------------------------------------------
# serve_otlp loop
# ---------------------------------------------------------------------------


class ServeOtlpLoopTests(TestCase):
    def test_pushes_metrics_until_stop_event_set(self) -> None:
        cm = _build_metrics()
        push_count = {"n": 0}

        class _Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        stop = threading.Event()

        def fake_urlopen(req, timeout=10.0):
            push_count["n"] += 1
            if push_count["n"] >= 3:
                stop.set()
            return _Resp()

        with patch(
            "agent_relay.exporters.otlp.urllib_request.urlopen",
            side_effect=fake_urlopen,
        ):
            rc = serve_otlp(
                Path("."),
                endpoint="http://x/y",
                interval_seconds=0.05,
                extractor=lambda _r: cm,
                stop_event=stop,
                sleep=lambda s: None,
            )
        self.assertEqual(rc, 0)
        self.assertGreaterEqual(push_count["n"], 3)

    def test_extractor_failure_logged_and_continues(self) -> None:
        stop = threading.Event()
        attempts = {"n": 0}

        def boom(_repo):
            attempts["n"] += 1
            if attempts["n"] >= 2:
                stop.set()
            raise RuntimeError("synthetic")

        stderr_buf = io.StringIO()
        with patch("agent_relay.exporters.otlp.sys.stderr", stderr_buf):
            rc = serve_otlp(
                Path("."),
                endpoint="http://x/y",
                interval_seconds=0.05,
                extractor=boom,
                stop_event=stop,
                sleep=lambda s: None,
            )
        self.assertEqual(rc, 0)
        self.assertIn("otlp extract failed", stderr_buf.getvalue())


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


def _make_serve_args(**kwargs) -> argparse.Namespace:
    base = dict(
        prometheus=None,
        prometheus_refresh=5.0,
        otlp=None,
        otlp_header=[],
        otlp_interval=30.0,
        repo=".",
        json=False,
        quiet=False,
    )
    base.update(kwargs)
    base["console"] = create_console()
    return argparse.Namespace(**base)


class CmdMetricsServeOtlpTests(TestCase):
    def test_invalid_otlp_header_returns_error(self) -> None:
        rc = cmd_metrics_serve(_make_serve_args(otlp="http://x/y", otlp_header=["nocolon"]))
        self.assertEqual(rc, 2)

    def test_otlp_only_dispatches_to_serve_otlp(self) -> None:
        with TemporaryDirectory() as tmp:
            args = _make_serve_args(otlp="http://x/y", repo=tmp, otlp_interval=0.05)
            # Make serve_otlp return immediately so the OTLP-only join loop exits.
            otlp_call = MagicMock(return_value=0)
            with patch("agent_relay.cli.serve_otlp", otlp_call):
                rc = cmd_metrics_serve(args)
        self.assertEqual(rc, 0)
        otlp_call.assert_called_once()
        self.assertEqual(otlp_call.call_args.kwargs["endpoint"], "http://x/y")

    def test_both_exporters_run_concurrently(self) -> None:
        with TemporaryDirectory() as tmp:
            args = _make_serve_args(prometheus="127.0.0.1:9464", otlp="http://x/y", repo=tmp)
            otlp_call = MagicMock(return_value=0)
            prom_call = MagicMock(return_value=0)
            with (
                patch("agent_relay.cli.serve_otlp", otlp_call),
                patch("agent_relay.cli.serve_prometheus", prom_call),
            ):
                rc = cmd_metrics_serve(args)
        self.assertEqual(rc, 0)
        # Both should have been invoked.
        prom_call.assert_called_once()
        otlp_call.assert_called_once()
