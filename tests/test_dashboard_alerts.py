"""Phase C — alert evaluation, banner rendering, and /alerts routing."""

from __future__ import annotations

import http.client
import json
import threading
import time
from contextlib import closing
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from agent_relay.alerts import Alert, AlertConfig, alerts_config_path
from agent_relay.dashboard_alerts import (
    AlertConfigCache,
    evaluate_alerts_for_view,
    highest_severity,
    render_alert_banner_html,
    render_alerts_page_html,
    render_alerts_payload,
)
from agent_relay.exporters.prometheus import serve_prometheus
from agent_relay.metrics import (
    CrossSessionMetrics,
    SessionMetrics,
    TokenUsage,
    TurnMetrics,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _turn(
    *,
    sid: str = "s1",
    n: int = 1,
    cost: float | None = 0.05,
    duration_ms: int = 1000,
    tokens: int = 100,
    succeeded: bool = True,
    status: str = "done",
) -> TurnMetrics:
    return TurnMetrics(
        session_id=sid,
        turn_number=n,
        agent="claude",
        model="claude-opus",
        started_at="2026-05-07T10:00:00Z",
        finished_at="2026-05-07T10:00:01Z",
        duration_ms=duration_ms,
        api_duration_ms=None,
        tokens=TokenUsage(input=tokens // 2, output=tokens - tokens // 2),
        cost_usd=cost,
        tool_calls=0,
        status=status,
        succeeded=succeeded,
    )


def _session(*turns: TurnMetrics, sid: str = "s1") -> SessionMetrics:
    succ = sum(1 for t in turns if t.succeeded)
    cost = sum(t.cost_usd for t in turns if t.cost_usd is not None) or None
    duration = sum(t.duration_ms or 0 for t in turns)
    tokens = TokenUsage(
        input=sum(t.tokens.input or 0 for t in turns),
        output=sum(t.tokens.output or 0 for t in turns),
    )
    return SessionMetrics(
        session_id=sid,
        current_agent="claude",
        current_status="active",
        objective="o",
        started_at="2026-05-07T10:00:00Z",
        updated_at="2026-05-07T10:00:00Z",
        turn_count=len(turns),
        successful_turns=succ,
        total_tokens=tokens,
        total_cost_usd=cost,
        total_duration_ms=duration,
        by_agent={"claude": tokens},
        cost_by_agent={"claude": cost or 0.0},
        turns=tuple(turns),
    )


def _cross(*sessions: SessionMetrics) -> CrossSessionMetrics:
    return CrossSessionMetrics(
        sessions=tuple(sessions),
        by_agent={"claude": TokenUsage(input=1, output=1)},
        cost_by_agent={"claude": 0.0},
        by_day={},
        total_tokens=TokenUsage(input=1, output=1),
        total_cost_usd=0.0,
        total_duration_ms=1,
        session_count=len(sessions),
    )


def _alert(
    *,
    severity: str = "warning",
    rule: str = "cost_per_turn",
    sid: str = "s1",
    turn: int | None = 1,
    threshold: float | int = 0.10,
    observed: float | int = 0.20,
    message: str = "m",
) -> Alert:
    return Alert(
        rule=rule,
        severity=severity,
        session_id=sid,
        turn_number=turn,
        threshold=threshold,
        observed=observed,
        message=message,
        timestamp="2026-05-07T10:00:00Z",
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


class EvaluateAlertsForViewTests(TestCase):
    def test_empty_config_returns_no_alerts(self) -> None:
        metrics = _cross(_session(_turn(cost=10.0)))
        self.assertEqual(evaluate_alerts_for_view(metrics, AlertConfig()), ())

    def test_cost_per_turn_threshold_fires(self) -> None:
        metrics = _cross(_session(_turn(cost=0.50)))
        cfg = AlertConfig(cost_per_turn_usd=0.10)
        alerts = evaluate_alerts_for_view(metrics, cfg)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].rule, "cost_per_turn")

    def test_severity_orders_critical_above_warning(self) -> None:
        # 0.5 vs 0.10 = 5x → critical (>= 2x). 0.15 vs 0.10 = 1.5x → warning.
        metrics = _cross(
            _session(_turn(cost=0.50, n=1), _turn(cost=0.15, n=2)),
        )
        cfg = AlertConfig(cost_per_turn_usd=0.10)
        alerts = evaluate_alerts_for_view(metrics, cfg)
        # Sort puts critical first.
        self.assertEqual(alerts[0].severity, "critical")
        self.assertEqual(alerts[1].severity, "warning")

    def test_session_and_turn_alerts_combine(self) -> None:
        metrics = _cross(
            _session(*[_turn(cost=0.10, n=i + 1) for i in range(6)]),
        )
        # cost_per_session: 0.60, threshold 0.50 → fires once.
        # cost_per_turn: 0.10, threshold 0.05 → fires per turn (6 times).
        cfg = AlertConfig(cost_per_session_usd=0.50, cost_per_turn_usd=0.05)
        alerts = evaluate_alerts_for_view(metrics, cfg)
        rules = {a.rule for a in alerts}
        self.assertIn("cost_per_session", rules)
        self.assertIn("cost_per_turn", rules)


class HighestSeverityTests(TestCase):
    def _alert(self, severity: str) -> Alert:
        return Alert(
            rule="cost_per_turn",
            severity=severity,
            session_id="s1",
            turn_number=1,
            threshold=0.10,
            observed=0.20,
            message="m",
            timestamp="2026-05-07T10:00:00Z",
        )

    def test_none_when_empty(self) -> None:
        self.assertIsNone(highest_severity(()))

    def test_critical_wins_over_warning(self) -> None:
        sev = highest_severity((self._alert("warning"), self._alert("critical")))
        self.assertEqual(sev, "critical")

    def test_warning_when_no_critical(self) -> None:
        sev = highest_severity((self._alert("warning"), self._alert("warning")))
        self.assertEqual(sev, "warning")


# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------


class BannerRenderTests(TestCase):
    def test_empty_string_when_no_alerts(self) -> None:
        self.assertEqual(render_alert_banner_html(()), "")

    def test_count_and_link_to_alerts_page(self) -> None:
        a = _alert(severity="warning")
        html = render_alert_banner_html((a, a))
        self.assertIn("/alerts", html)
        self.assertIn("2 warning", html)
        self.assertIn("alert-warning", html)

    def test_critical_severity_paints_banner_red(self) -> None:
        warn = _alert(severity="warning", observed=0.15)
        crit = _alert(severity="critical", sid="s2", observed=0.50)
        html = render_alert_banner_html((warn, crit))
        self.assertIn("alert-critical", html)
        self.assertIn("var(--error)", html)

    def test_banner_shows_severity_breakdown_when_mixed(self) -> None:
        warn = _alert(severity="warning")
        crit = _alert(severity="critical", sid="s2")
        html = render_alert_banner_html((warn, warn, crit))
        self.assertIn("1 critical, 2 warning", html)

    def test_banner_omits_zero_severities(self) -> None:
        warn = _alert(severity="warning")
        html = render_alert_banner_html((warn, warn, warn))
        self.assertIn("3 warning", html)
        self.assertNotIn("critical", html)

    def test_banner_shows_filtered_scope_chip(self) -> None:
        html = render_alert_banner_html((_alert(),), filtered=True)
        self.assertIn("in current view", html)
        self.assertNotIn("across all sessions", html)

    def test_banner_shows_unfiltered_scope_chip(self) -> None:
        html = render_alert_banner_html((_alert(),), filtered=False)
        self.assertIn("across all sessions", html)
        self.assertNotIn("in current view", html)


# ---------------------------------------------------------------------------
# Full /alerts page
# ---------------------------------------------------------------------------


class AlertsPageRenderTests(TestCase):
    def test_renders_each_alert_row(self) -> None:
        a = Alert(
            rule="cost_per_turn",
            severity="critical",
            session_id="20260507-101010-aae0fb",
            turn_number=3,
            threshold=0.10,
            observed=0.42,
            message="cost $0.4200 exceeds threshold $0.1000",
            timestamp="2026-05-07T10:00:00Z",
        )
        html = render_alerts_page_html((a,), AlertConfig(cost_per_turn_usd=0.10))
        self.assertIn("20260507-101010-aae0fb", html)
        self.assertIn("$0.4200", html)
        self.assertIn("$0.1000", html)
        self.assertIn("cost / turn", html)
        self.assertIn("critical", html)
        # Breadcrumb back to dashboard is present.
        self.assertIn("← dashboard", html)

    def test_empty_state_friendly_copy(self) -> None:
        html = render_alerts_page_html((), AlertConfig(cost_per_turn_usd=0.10))
        self.assertIn("no alerts firing", html)
        self.assertIn("</html>", html)

    def test_thresholds_card_lists_configured_rules(self) -> None:
        cfg = AlertConfig(
            cost_per_turn_usd=0.10,
            cost_per_session_usd=1.00,
            duration_per_turn_ms=60_000,
            tokens_per_turn=10_000,
            error_rate_threshold=0.20,
        )
        html = render_alerts_page_html((), cfg)
        self.assertIn("$0.1000", html)
        self.assertIn("$1.0000", html)
        self.assertIn("1m00s", html)
        self.assertIn("10,000", html)
        self.assertIn("20%", html)

    def test_breadcrumb_preserves_filter_query(self) -> None:
        html = render_alerts_page_html(
            (), AlertConfig(), available_filter_query="agent=claude&since=2026-05-01"
        )
        self.assertIn('href="/?agent=claude', html)

    def test_alerts_page_links_to_external_history_sources(self) -> None:
        html = render_alerts_page_html((), AlertConfig())
        self.assertIn("prometheus", html)
        self.assertIn("metrics-tail", html)
        self.assertIn("metrics.alert", html)

    def test_tuning_hint_hidden_below_threshold(self) -> None:
        alerts = tuple(_alert(sid=f"s{i}") for i in range(9))
        html = render_alerts_page_html(alerts, AlertConfig(cost_per_turn_usd=0.10))
        self.assertNotIn("too noisy", html)

    def test_tuning_hint_shown_at_or_above_threshold(self) -> None:
        cost_alerts = tuple(
            _alert(rule="cost_per_turn", sid=f"cost-{i}", observed=0.20) for i in range(5)
        )
        token_alerts = tuple(
            _alert(
                rule="tokens_per_turn",
                sid=f"tokens-{i}",
                threshold=100,
                observed=200 + i,
            )
            for i in range(5)
        )
        html = render_alerts_page_html(
            cost_alerts + token_alerts,
            AlertConfig(cost_per_turn_usd=0.10, tokens_per_turn=100),
        )
        self.assertIn("too noisy", html)
        self.assertIn("cost / turn", html)
        self.assertIn("tokens / turn", html)

    def test_tuning_hint_uses_highest_observed_value_per_rule(self) -> None:
        alerts = tuple(
            _alert(rule="cost_per_turn", sid=f"s{i}", observed=value)
            for i, value in enumerate((0.10, 0.50, 0.30, 0.20, 0.21, 0.22, 0.23, 0.24, 0.25, 0.26))
        )
        html = render_alerts_page_html(alerts, AlertConfig(cost_per_turn_usd=0.01))
        self.assertIn("$0.5000", html)


# ---------------------------------------------------------------------------
# JSON payload (soft refresh)
# ---------------------------------------------------------------------------


class AlertsPayloadTests(TestCase):
    def test_payload_contains_alerts_list_region(self) -> None:
        a = Alert(
            rule="cost_per_turn",
            severity="warning",
            session_id="s1",
            turn_number=1,
            threshold=0.10,
            observed=0.15,
            message="m",
            timestamp="2026-05-07T10:00:00Z",
        )
        payload = render_alerts_payload(
            (a,), generated_at=datetime(2026, 5, 7, 10, 0, 0, tzinfo=UTC)
        )
        self.assertEqual(payload["generatedAt"], "2026-05-07T10:00:00Z")
        regions = payload["regions"]
        assert isinstance(regions, dict)
        self.assertIn("alerts-list", regions)
        self.assertIn("s1", regions["alerts-list"])


# ---------------------------------------------------------------------------
# AlertConfigCache (mtime-aware)
# ---------------------------------------------------------------------------


class AlertConfigCacheTests(TestCase):
    def test_returns_default_when_no_config_file(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = AlertConfigCache(Path(tmp))
            self.assertTrue(cache.get().is_empty)

    def test_picks_up_edits_via_mtime(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cfg_path = alerts_config_path(repo)
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            cfg_path.write_text("cost_per_turn_usd = 0.10\n")
            cache = AlertConfigCache(repo)
            self.assertEqual(cache.get().cost_per_turn_usd, 0.10)

            # Bump mtime + change value.
            time.sleep(0.01)
            cfg_path.write_text("cost_per_turn_usd = 0.50\n")
            import os

            future = cfg_path.stat().st_mtime + 1
            os.utime(cfg_path, (future, future))
            self.assertEqual(cache.get().cost_per_turn_usd, 0.50)


# ---------------------------------------------------------------------------
# Dashboard banner integration
# ---------------------------------------------------------------------------


class DashboardBannerIntegrationTests(TestCase):
    def test_banner_wired_into_alerts_region(self) -> None:
        from agent_relay.dashboard import render_dashboard_html

        a = Alert(
            rule="cost_per_turn",
            severity="critical",
            session_id="s1",
            turn_number=1,
            threshold=0.10,
            observed=0.42,
            message="m",
            timestamp="t",
        )
        banner = render_alert_banner_html((a,))
        html = render_dashboard_html(_cross(_session(_turn())), alerts_banner_html=banner)
        self.assertIn('data-dashboard-region="alerts"', html)
        self.assertIn("/alerts", html)
        self.assertIn("alert-critical", html)

    def test_no_banner_renders_empty_alerts_region(self) -> None:
        from agent_relay.dashboard import render_dashboard_html

        html = render_dashboard_html(_cross(_session(_turn())))
        self.assertIn('data-dashboard-region="alerts"', html)
        # The region exists but no actual banner element is rendered inside.
        # (We grep for the opening tag — `.alert-banner` as a CSS selector
        # appears in the stylesheet regardless.)
        self.assertNotIn('class="alert-banner', html)


# ---------------------------------------------------------------------------
# End-to-end routing
# ---------------------------------------------------------------------------


class AlertsRoutingTests(TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        repo = Path(self._tmp.name)
        cfg_path = alerts_config_path(repo)
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text("cost_per_turn_usd = 0.10\n")

        # Build a metrics snapshot that fires the cost_per_turn rule.
        metrics = _cross(_session(_turn(cost=0.42)))

        captured: dict[str, ThreadingHTTPServer] = {}

        def factory(addr, handler):
            server = ThreadingHTTPServer(addr, handler)
            captured["server"] = server
            return server

        self._thread = threading.Thread(
            target=serve_prometheus,
            kwargs={
                "repo_root": repo,
                "host": "127.0.0.1",
                "port": 0,
                "refresh_interval": 0.05,
                "extractor": lambda *args, **kwargs: metrics,
                "server_factory": factory,
            },
            daemon=True,
        )
        self._thread.start()
        for _ in range(50):
            if "server" in captured:
                break
            time.sleep(0.01)
        self.assertIn("server", captured)
        self._server = captured["server"]
        self._port = self._server.server_address[1]

    def tearDown(self) -> None:
        self._server.shutdown()
        self._thread.join(timeout=2.0)
        self._tmp.cleanup()

    def _get(self, path: str) -> tuple[int, str, str]:
        with closing(http.client.HTTPConnection("127.0.0.1", self._port, timeout=2)) as conn:
            conn.request("GET", path)
            resp = conn.getresponse()
            return resp.status, resp.getheader("Content-Type", ""), resp.read().decode("utf-8")

    def test_alerts_path_serves_html_page(self) -> None:
        status, content_type, body = self._get("/alerts")
        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)
        self.assertIn("active alerts", body)
        self.assertIn("cost / turn", body)

    def test_alerts_data_serves_json_payload(self) -> None:
        status, content_type, body = self._get("/alerts/data")
        self.assertEqual(status, 200)
        self.assertIn("application/json", content_type)
        payload = json.loads(body)
        self.assertIn("generatedAt", payload)
        regions = payload["regions"]
        self.assertIn("alerts-list", regions)
        self.assertIn("s1", regions["alerts-list"])

    def test_dashboard_banner_links_to_alerts_when_firing(self) -> None:
        status, _ct, body = self._get("/")
        self.assertEqual(status, 200)
        # Banner should be in the HTML pointing at /alerts.
        self.assertIn('href="/alerts"', body)
        # The banner uses critical color since 0.42 / 0.10 = 4.2x → critical.
        self.assertIn("alert-critical", body)

    def test_banner_chip_appears_in_dashboard_when_filter_active(self) -> None:
        status, _ct, body = self._get("/?agent=claude")
        self.assertEqual(status, 200)
        self.assertIn("in current view", body)
