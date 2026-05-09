"""Tests for the `agent-relay alerts` CLI handler."""

from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from agent_relay.alerts import alerts_config_path
from agent_relay.cli import build_parser, cmd_alerts
from agent_relay.layout import (
    derived_view_path,
    session_manifest_path,
    turn_dir,
    turns_dir,
)
from agent_relay.ui import create_console


def _scaffold(repo: Path, sid: str = "s1") -> None:
    session_manifest_path(repo, sid).parent.mkdir(parents=True, exist_ok=True)
    session_manifest_path(repo, sid).write_text("{}", encoding="utf-8")
    turns_dir(repo, sid).mkdir(parents=True, exist_ok=True)
    derived_view_path(repo, sid).parent.mkdir(parents=True, exist_ok=True)
    derived_view_path(repo, sid).write_text(
        json.dumps(
            {
                "session_id": sid,
                "current_agent": "claude",
                "current_status": "active",
                "objective": "test objective",
                "created_at": "2026-05-01T10:00:00.000Z",
                "updated_at": "2026-05-01T10:30:00.000Z",
            }
        ),
        encoding="utf-8",
    )


def _write_turn(repo: Path, sid: str = "s1", n: int = 1) -> None:
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
        "total_cost_usd": 0.4318,
        "usage": {"input_tokens": 6, "output_tokens": 56},
        "modelUsage": {"claude-opus-4-7": {"costUSD": 0.4318}},
    }
    (tdir / "output.jsonl").write_text(json.dumps(result) + "\n", encoding="utf-8")
    (tdir / "state.json").write_text(json.dumps(state), encoding="utf-8")


def _write_alert_config(repo: Path) -> None:
    path = alerts_config_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("cost_per_turn_usd = 0.10\n", encoding="utf-8")


def _make_args(
    *,
    repo: str,
    all_: bool = False,
    since: str | None = None,
    agent: list[str] | None = None,
    json_mode: bool = False,
    quiet: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(
        repo=repo,
        all=all_,
        since=since,
        agent=agent,
        json=json_mode,
        quiet=quiet,
        console=create_console(json_mode=json_mode, quiet=quiet),
    )


class AlertsParserTests(TestCase):
    def test_alerts_subparser_registered(self) -> None:
        ns = build_parser().parse_args(["alerts"])
        self.assertEqual(ns.command, "alerts")
        self.assertFalse(ns.all)

    def test_alerts_subparser_accepts_filters(self) -> None:
        ns = build_parser().parse_args(["alerts", "--since", "2026-05-01", "--agent", "claude"])
        self.assertEqual(ns.since, "2026-05-01")
        self.assertEqual(ns.agent, ["claude"])


class CmdAlertsTests(TestCase):
    def test_cmd_alerts_json_emits_command_field(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _scaffold(repo)
            _write_turn(repo)
            _write_alert_config(repo)
            args = _make_args(repo=tmp, json_mode=True)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cmd_alerts(args)
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue().strip())
        self.assertEqual(payload["command"], "alerts")
        self.assertEqual(len(payload["alerts"]), 1)

    def test_cmd_alerts_quiet_one_line_per_alert(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _scaffold(repo)
            _write_turn(repo)
            _write_alert_config(repo)
            args = _make_args(repo=tmp, quiet=True)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cmd_alerts(args)
        self.assertEqual(rc, 0)
        lines = [line for line in buf.getvalue().splitlines() if line]
        self.assertEqual(len(lines), 1)
        parts = lines[0].split("\t")
        self.assertEqual(parts[:2], ["critical", "cost_per_turn"])
        self.assertEqual(parts[4], "s1")

    def test_cmd_alerts_returns_zero_when_no_config(self) -> None:
        with TemporaryDirectory() as tmp:
            args = _make_args(repo=tmp, json_mode=True)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cmd_alerts(args)
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue().strip())
        self.assertEqual(payload["alerts"], [])

    def test_cmd_alerts_reports_config_path(self) -> None:
        with TemporaryDirectory() as tmp:
            args = _make_args(repo=tmp, json_mode=True)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cmd_alerts(args)
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue().strip())
        self.assertIn("config_path", payload)
