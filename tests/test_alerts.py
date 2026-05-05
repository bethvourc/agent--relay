"""Tests for the threshold alerts layer."""

from __future__ import annotations

import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from agent_relay.alerts import (
    Alert,
    AlertConfig,
    alerts_config_path,
    emit_alert,
    evaluate_session,
    evaluate_turn,
    load_alert_config,
)
from agent_relay.layout import relay_root
from agent_relay.metrics import (
    SessionMetrics,
    TokenUsage,
    TurnMetrics,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _turn(
    *,
    turn_number: int = 1,
    cost_usd: float | None = 0.05,
    duration_ms: int | None = 1000,
    tokens_input: int | None = 100,
    tokens_output: int | None = 50,
    succeeded: bool = True,
) -> TurnMetrics:
    return TurnMetrics(
        session_id="s1",
        turn_number=turn_number,
        agent="claude",
        model="m",
        started_at=None,
        finished_at=None,
        duration_ms=duration_ms,
        api_duration_ms=None,
        tokens=TokenUsage(input=tokens_input, output=tokens_output),
        cost_usd=cost_usd,
        tool_calls=0,
        status="continue" if succeeded else "error",
        succeeded=succeeded,
    )


def _session(
    *,
    turn_count: int = 1,
    successful_turns: int = 1,
    total_cost_usd: float | None = 0.05,
    turns: tuple[TurnMetrics, ...] = (),
) -> SessionMetrics:
    return SessionMetrics(
        session_id="s1",
        current_agent="claude",
        current_status="active",
        objective="o",
        started_at=None,
        updated_at=None,
        turn_count=turn_count,
        successful_turns=successful_turns,
        total_tokens=TokenUsage(input=100, output=50),
        total_cost_usd=total_cost_usd,
        total_duration_ms=1000,
        by_agent={"claude": TokenUsage(input=100, output=50)},
        cost_by_agent={"claude": total_cost_usd or 0.0},
        turns=turns,
    )


# ---------------------------------------------------------------------------
# AlertConfig
# ---------------------------------------------------------------------------


class AlertConfigTests(TestCase):
    def test_defaults_are_all_none(self) -> None:
        cfg = AlertConfig()
        self.assertTrue(cfg.is_empty)

    def test_is_empty_false_when_one_field_set(self) -> None:
        cfg = AlertConfig(cost_per_turn_usd=0.5)
        self.assertFalse(cfg.is_empty)


class LoadAlertConfigTests(TestCase):
    def test_returns_defaults_when_file_missing(self) -> None:
        with TemporaryDirectory() as tmp:
            cfg = load_alert_config(Path(tmp))
        self.assertTrue(cfg.is_empty)

    def test_loads_full_config(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            relay_root(repo).mkdir(parents=True, exist_ok=True)
            path = alerts_config_path(repo)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "cost_per_turn_usd = 0.5\n"
                "cost_per_session_usd = 5.0\n"
                "duration_per_turn_ms = 300000\n"
                "tokens_per_turn = 200000\n"
                "error_rate_threshold = 0.4\n"
                "error_rate_min_turns = 3\n",
                encoding="utf-8",
            )
            cfg = load_alert_config(repo)
        self.assertAlmostEqual(cfg.cost_per_turn_usd or 0.0, 0.5)
        self.assertAlmostEqual(cfg.cost_per_session_usd or 0.0, 5.0)
        self.assertEqual(cfg.duration_per_turn_ms, 300000)
        self.assertEqual(cfg.tokens_per_turn, 200000)
        self.assertAlmostEqual(cfg.error_rate_threshold or 0.0, 0.4)
        self.assertEqual(cfg.error_rate_min_turns, 3)

    def test_invalid_toml_falls_back_to_defaults(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            relay_root(repo).mkdir(parents=True, exist_ok=True)
            path = alerts_config_path(repo)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("not [valid toml", encoding="utf-8")
            cfg = load_alert_config(repo)
        self.assertTrue(cfg.is_empty)


# ---------------------------------------------------------------------------
# evaluate_turn
# ---------------------------------------------------------------------------


class EvaluateTurnTests(TestCase):
    def test_no_config_means_no_alerts(self) -> None:
        alerts = evaluate_turn(_turn(), _session(), AlertConfig())
        self.assertEqual(alerts, [])

    def test_cost_per_turn_fires_above_threshold(self) -> None:
        alerts = evaluate_turn(
            _turn(cost_usd=0.6), _session(), AlertConfig(cost_per_turn_usd=0.5)
        )
        rules = [a.rule for a in alerts]
        self.assertIn("cost_per_turn", rules)

    def test_cost_per_turn_does_not_fire_at_or_below_threshold(self) -> None:
        alerts = evaluate_turn(
            _turn(cost_usd=0.5), _session(), AlertConfig(cost_per_turn_usd=0.5)
        )
        self.assertEqual([a.rule for a in alerts if a.rule == "cost_per_turn"], [])

    def test_duration_per_turn_fires(self) -> None:
        alerts = evaluate_turn(
            _turn(duration_ms=400000),
            _session(),
            AlertConfig(duration_per_turn_ms=300000),
        )
        self.assertIn("duration_per_turn", [a.rule for a in alerts])

    def test_tokens_per_turn_fires(self) -> None:
        alerts = evaluate_turn(
            _turn(tokens_input=150000, tokens_output=100000),
            _session(),
            AlertConfig(tokens_per_turn=200000),
        )
        rules = [a.rule for a in alerts]
        self.assertIn("tokens_per_turn", rules)
        a = next(a for a in alerts if a.rule == "tokens_per_turn")
        self.assertEqual(a.observed, 250000)

    def test_critical_severity_at_double_threshold(self) -> None:
        alerts = evaluate_turn(
            _turn(cost_usd=2.0),
            _session(),
            AlertConfig(cost_per_turn_usd=0.5),
        )
        a = next(a for a in alerts if a.rule == "cost_per_turn")
        self.assertEqual(a.severity, "critical")

    def test_warning_severity_below_double(self) -> None:
        alerts = evaluate_turn(
            _turn(cost_usd=0.7),
            _session(),
            AlertConfig(cost_per_turn_usd=0.5),
        )
        a = next(a for a in alerts if a.rule == "cost_per_turn")
        self.assertEqual(a.severity, "warning")

    def test_missing_optional_fields_skip_their_rule(self) -> None:
        alerts = evaluate_turn(
            _turn(cost_usd=None),
            _session(total_cost_usd=None),
            AlertConfig(cost_per_turn_usd=0.5, cost_per_session_usd=5.0),
        )
        # No alerts because both observed values are None.
        self.assertEqual(alerts, [])


# ---------------------------------------------------------------------------
# evaluate_session
# ---------------------------------------------------------------------------


class EvaluateSessionTests(TestCase):
    def test_cost_per_session_fires(self) -> None:
        alerts = evaluate_session(
            _session(total_cost_usd=10.0),
            AlertConfig(cost_per_session_usd=5.0),
        )
        self.assertIn("cost_per_session", [a.rule for a in alerts])

    def test_error_rate_fires_when_above_threshold(self) -> None:
        turns = tuple(
            _turn(turn_number=i, succeeded=(i % 2 == 0)) for i in range(1, 7)
        )
        # 3 successful out of 6 → 50% error rate.
        sm = _session(turn_count=6, successful_turns=3, turns=turns)
        alerts = evaluate_session(
            sm,
            AlertConfig(error_rate_threshold=0.4, error_rate_min_turns=5),
        )
        self.assertIn("error_rate", [a.rule for a in alerts])

    def test_error_rate_does_not_fire_below_min_turns(self) -> None:
        sm = _session(turn_count=3, successful_turns=0)
        alerts = evaluate_session(
            sm,
            AlertConfig(error_rate_threshold=0.4, error_rate_min_turns=5),
        )
        self.assertEqual(alerts, [])


# ---------------------------------------------------------------------------
# emit_alert
# ---------------------------------------------------------------------------


class EmitAlertTests(TestCase):
    def test_writes_jsonl_to_output_and_human_line_to_stderr(self) -> None:
        alert = Alert(
            rule="cost_per_turn",
            severity="warning",
            session_id="s1",
            turn_number=2,
            threshold=0.5,
            observed=0.7,
            message="turn 2 cost $0.7000 exceeds threshold $0.5000",
            timestamp="2026-05-04T10:00:00.000000Z",
        )
        out = io.StringIO()
        err = io.StringIO()
        emit_alert(alert, output=out, stderr=err)

        line = out.getvalue().strip()
        payload = json.loads(line)
        self.assertEqual(payload["kind"], "metrics.alert")
        self.assertEqual(payload["rule"], "cost_per_turn")
        self.assertEqual(payload["severity"], "warning")
        self.assertEqual(payload["session_id"], "s1")
        self.assertEqual(payload["turn_number"], 2)

        stderr = err.getvalue()
        self.assertIn("[warning]", stderr)
        self.assertIn("cost_per_turn", stderr)
        self.assertIn("session s1", stderr)
        self.assertIn("turn 2", stderr)
