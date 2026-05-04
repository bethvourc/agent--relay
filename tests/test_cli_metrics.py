"""Tests for the `agent-relay metrics` CLI handler and renderers."""

from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from agent_relay.cli import build_parser, cmd_metrics
from agent_relay.layout import (
    derived_view_path,
    turn_dir,
    turns_dir,
)
from agent_relay.metrics import (
    SessionMetrics,
    TokenUsage,
    TurnMetrics,
)
from agent_relay.metrics_ui import (
    render_cross_session_metrics,
    render_session_metrics,
)
from agent_relay.ui import create_console


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scaffold(repo: Path, sid: str) -> None:
    turns_dir(repo, sid).mkdir(parents=True, exist_ok=True)
    derived_view_path(repo, sid).parent.mkdir(parents=True, exist_ok=True)


def _write_view(repo: Path, sid: str, **fields) -> None:
    base = {
        "session_id": sid,
        "current_agent": "claude",
        "current_status": "active",
        "objective": "test objective",
        "created_at": "2026-05-01T10:00:00.000Z",
        "updated_at": "2026-05-01T10:30:00.000Z",
    }
    base.update(fields)
    derived_view_path(repo, sid).write_text(json.dumps(base), encoding="utf-8")


def _write_turn(repo: Path, sid: str, n: int) -> None:
    tdir = turn_dir(repo, sid, n)
    tdir.mkdir(parents=True, exist_ok=True)
    state = {
        "agent_key": "claude",
        "turn_number": n,
        "status": "continue",
        "metadata": {
            "started_at": "2026-05-01T10:00:00.000Z",
            "finished_at": "2026-05-01T10:00:42.000Z",
        },
    }
    result = {
        "type": "result",
        "duration_ms": 42000,
        "duration_api_ms": 31000,
        "total_cost_usd": 0.4318,
        "usage": {
            "input_tokens": 6,
            "output_tokens": 56,
            "cache_creation_input_tokens": 200,
            "cache_read_input_tokens": 100,
        },
        "modelUsage": {"claude-opus-4-7": {"costUSD": 0.4318}},
    }
    (tdir / "output.jsonl").write_text(json.dumps(result) + "\n", encoding="utf-8")
    (tdir / "state.json").write_text(json.dumps(state), encoding="utf-8")


def _make_args(
    *,
    repo: str,
    session_id: str | None = None,
    all_: bool = False,
    since: str | None = None,
    agent: list[str] | None = None,
    json_mode: bool = False,
    quiet: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(
        repo=repo,
        session_id=session_id,
        all=all_,
        since=since,
        agent=agent,
        json=json_mode,
        quiet=quiet,
        console=create_console(json_mode=json_mode, quiet=quiet),
    )


# ---------------------------------------------------------------------------
# Parser registration
# ---------------------------------------------------------------------------


class MetricsParserTests(TestCase):
    def test_subparser_registered_with_optional_session_id(self) -> None:
        parser = build_parser()
        ns = parser.parse_args(["metrics"])
        self.assertEqual(ns.command, "metrics")
        self.assertIsNone(ns.session_id)
        self.assertFalse(ns.all)

    def test_subparser_accepts_explicit_session_and_flags(self) -> None:
        parser = build_parser()
        ns = parser.parse_args(
            [
                "metrics",
                "abc-123",
                "--since",
                "2026-05-01",
                "--agent",
                "claude",
                "--agent",
                "codex",
            ]
        )
        self.assertEqual(ns.session_id, "abc-123")
        self.assertEqual(ns.since, "2026-05-01")
        self.assertEqual(ns.agent, ["claude", "codex"])

    def test_all_flag_recognized(self) -> None:
        parser = build_parser()
        ns = parser.parse_args(["metrics", "--all"])
        self.assertTrue(ns.all)


# ---------------------------------------------------------------------------
# Handler — session resolution
# ---------------------------------------------------------------------------


class CmdMetricsSessionResolutionTests(TestCase):
    def test_errors_when_no_sessions_at_all(self) -> None:
        with TemporaryDirectory() as tmp:
            args = _make_args(repo=tmp, session_id=None, json_mode=True)
            rc = cmd_metrics(args)
            self.assertEqual(rc, 2)

    def test_errors_with_unknown_explicit_session(self) -> None:
        with TemporaryDirectory() as tmp:
            args = _make_args(repo=tmp, session_id="does-not-exist", json_mode=True)
            rc = cmd_metrics(args)
            self.assertEqual(rc, 2)

    def test_auto_picks_latest_session_when_no_arg(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _scaffold(repo, "s1")
            _write_view(repo, "s1")
            _write_turn(repo, "s1", 1)

            args = _make_args(repo=tmp, session_id=None, json_mode=True)
            buf = io.StringIO()
            with (
                patch(
                    "agent_relay.cli.pick_latest_session",
                    return_value={"session_id": "s1", "current_status": "active"},
                ),
                patch("agent_relay.cli.is_session", return_value=True),
                redirect_stdout(buf),
            ):
                rc = cmd_metrics(args)
            self.assertEqual(rc, 0)
            payload = json.loads(buf.getvalue().strip())
            self.assertEqual(payload["session"]["session_id"], "s1")


# ---------------------------------------------------------------------------
# Handler — output mode dispatch
# ---------------------------------------------------------------------------


class CmdMetricsOutputModeTests(TestCase):
    def test_json_mode_emits_one_object(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _scaffold(repo, "s1")
            _write_view(repo, "s1")
            _write_turn(repo, "s1", 1)
            _write_turn(repo, "s1", 2)
            args = _make_args(repo=tmp, session_id="s1", json_mode=True)
            buf = io.StringIO()
            with patch("agent_relay.cli.is_session", return_value=True), redirect_stdout(buf):
                rc = cmd_metrics(args)
        self.assertEqual(rc, 0)
        out = buf.getvalue().strip()
        self.assertTrue(out)
        payload = json.loads(out)
        self.assertEqual(payload["command"], "metrics")
        self.assertEqual(payload["session"]["turn_count"], 2)
        self.assertAlmostEqual(payload["session"]["total_cost_usd"], 0.4318 * 2, places=4)

    def test_quiet_mode_emits_one_line(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _scaffold(repo, "s1")
            _write_view(repo, "s1")
            _write_turn(repo, "s1", 1)
            args = _make_args(repo=tmp, session_id="s1", quiet=True)
            buf = io.StringIO()
            with patch("agent_relay.cli.is_session", return_value=True), redirect_stdout(buf):
                rc = cmd_metrics(args)
        self.assertEqual(rc, 0)
        line = buf.getvalue().strip().split("\n")[0]
        parts = line.split("\t")
        self.assertEqual(parts[0], "s1")
        self.assertEqual(parts[1], "claude")
        self.assertEqual(parts[2], "1")

    def test_default_mode_renders_table(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _scaffold(repo, "s1")
            _write_view(repo, "s1")
            _write_turn(repo, "s1", 1)
            args = _make_args(repo=tmp, session_id="s1")
            with patch("agent_relay.cli.is_session", return_value=True):
                rc = cmd_metrics(args)
        self.assertEqual(rc, 0)


class CmdMetricsAllTests(TestCase):
    def test_all_aggregates_across_sessions_json(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            for sid in ("a", "b"):
                _scaffold(repo, sid)
                _write_view(repo, sid)
                _write_turn(repo, sid, 1)
            args = _make_args(repo=tmp, all_=True, json_mode=True)
            buf = io.StringIO()
            with patch("agent_relay.metrics.is_session", return_value=True), redirect_stdout(buf):
                rc = cmd_metrics(args)
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue().strip())
        self.assertEqual(payload["session_count"], 2)
        self.assertAlmostEqual(payload["total_cost_usd"], 0.4318 * 2, places=4)

    def test_all_with_no_sessions_returns_empty(self) -> None:
        with TemporaryDirectory() as tmp:
            args = _make_args(repo=tmp, all_=True, json_mode=True)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cmd_metrics(args)
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue().strip())
        self.assertEqual(payload["session_count"], 0)

    def test_invalid_since_returns_error(self) -> None:
        with TemporaryDirectory() as tmp:
            args = _make_args(repo=tmp, all_=True, since="not-a-date")
            rc = cmd_metrics(args)
        self.assertEqual(rc, 2)

    def test_since_filter_excludes_old_sessions(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _scaffold(repo, "old")
            _write_view(
                repo,
                "old",
                created_at="2025-01-01T00:00:00.000Z",
                updated_at="2025-01-01T00:00:00.000Z",
            )
            _write_turn(repo, "old", 1)
            _scaffold(repo, "new")
            _write_view(repo, "new")
            _write_turn(repo, "new", 1)
            args = _make_args(
                repo=tmp, all_=True, since="2026-01-01", json_mode=True
            )
            buf = io.StringIO()
            with patch("agent_relay.metrics.is_session", return_value=True), redirect_stdout(buf):
                rc = cmd_metrics(args)
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue().strip())
        self.assertEqual(payload["session_count"], 1)
        self.assertEqual(payload["sessions"][0]["session_id"], "new")


# ---------------------------------------------------------------------------
# Renderers (smoke — table rendering doesn't crash on edge inputs)
# ---------------------------------------------------------------------------


class RendererSmokeTests(TestCase):
    def test_render_session_metrics_no_turns(self) -> None:
        sm = SessionMetrics(
            session_id="empty",
            current_agent="claude",
            current_status="active",
            objective=None,
            started_at=None,
            updated_at=None,
            turn_count=0,
            successful_turns=0,
            total_tokens=TokenUsage(),
            total_cost_usd=None,
            total_duration_ms=0,
        )
        console = create_console()
        render_session_metrics(console, sm)  # should not raise

    def test_render_cross_session_metrics_empty(self) -> None:
        from agent_relay.metrics import CrossSessionMetrics

        cm = CrossSessionMetrics(sessions=())
        console = create_console()
        render_cross_session_metrics(console, cm)
