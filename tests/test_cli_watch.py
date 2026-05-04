"""Tests for the `agent-relay watch` CLI handler."""

from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import MagicMock, patch

from agent_relay.cli import build_parser, cmd_watch
from agent_relay.layout import (
    derived_view_path,
    journal_dir,
    session_root,
    turn_dir,
    turns_dir,
)
from agent_relay.ui import create_console


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scaffold_session(tmpdir: Path, sid: str) -> None:
    journal_dir(tmpdir, sid).mkdir(parents=True, exist_ok=True)
    turns_dir(tmpdir, sid).mkdir(parents=True, exist_ok=True)
    derived_view_path(tmpdir, sid).parent.mkdir(parents=True, exist_ok=True)


def _write_view(
    tmpdir: Path,
    sid: str,
    *,
    phase: str = "active",
    current_agent: str = "claude",
) -> None:
    derived_view_path(tmpdir, sid).write_text(
        json.dumps(
            {
                "phase": phase,
                "current_agent": current_agent,
                "objective": "test",
            }
        ),
        encoding="utf-8",
    )


def _write_journal_event(
    tmpdir: Path, sid: str, sequence: int, event_type: str
) -> None:
    path = journal_dir(tmpdir, sid) / f"{sequence:06d}-{event_type}.json"
    path.write_text(
        json.dumps(
            {
                "type": event_type,
                "sequence": sequence,
                "phase_after": "active",
                "event_id": f"ev-{sequence:06d}",
                "timestamp": "2026-05-03T12:00:00.000Z",
            }
        ),
        encoding="utf-8",
    )


def _make_args(
    *,
    repo: str,
    session_id: str | None = None,
    no_follow: bool = False,
    poll_interval: float = 0.01,
    json_mode: bool = False,
    quiet: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(
        repo=repo,
        session_id=session_id,
        no_follow=no_follow,
        poll_interval=poll_interval,
        json=json_mode,
        quiet=quiet,
        console=create_console(json_mode=json_mode, quiet=quiet),
    )


# ---------------------------------------------------------------------------
# Parser registration
# ---------------------------------------------------------------------------


class WatchParserTests(TestCase):
    def test_subparser_registered_with_optional_session_id(self) -> None:
        parser = build_parser()
        ns = parser.parse_args(["watch"])
        self.assertEqual(ns.command, "watch")
        self.assertIsNone(ns.session_id)
        self.assertFalse(ns.no_follow)
        self.assertAlmostEqual(ns.poll_interval, 0.25)

    def test_subparser_accepts_explicit_session_and_flags(self) -> None:
        parser = build_parser()
        ns = parser.parse_args(
            ["watch", "abc-123", "--no-follow", "--poll-interval", "0.5"]
        )
        self.assertEqual(ns.session_id, "abc-123")
        self.assertTrue(ns.no_follow)
        self.assertAlmostEqual(ns.poll_interval, 0.5)


# ---------------------------------------------------------------------------
# Handler — session resolution
# ---------------------------------------------------------------------------


class CmdWatchSessionResolutionTests(TestCase):
    def test_errors_when_no_active_session_and_no_arg(self) -> None:
        with TemporaryDirectory() as tmp:
            args = _make_args(repo=tmp, session_id=None, json_mode=True)
            # No sessions on disk.
            rc = cmd_watch(args)
            self.assertEqual(rc, 2)

    def test_errors_with_unknown_explicit_session(self) -> None:
        with TemporaryDirectory() as tmp:
            args = _make_args(
                repo=tmp, session_id="does-not-exist", json_mode=True
            )
            rc = cmd_watch(args)
            self.assertEqual(rc, 2)

    def test_auto_picks_latest_active_when_no_arg(self) -> None:
        with TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            sid = "my-active"
            _scaffold_session(tmpdir, sid)
            _write_view(tmpdir, sid, phase="active")

            captured: dict[str, str] = {}

            class _FakeSource:
                def __init__(self, repo, session_id, **kwargs) -> None:
                    captured["session_id"] = session_id

                def iter_events(self):
                    return iter(())

            args = _make_args(
                repo=tmp, session_id=None, no_follow=True, json_mode=True
            )
            with (
                patch(
                    "agent_relay.cli.pick_latest_active_session",
                    return_value=sid,
                ),
                patch("agent_relay.cli.is_session", return_value=True),
                patch("agent_relay.cli.WatchSource", _FakeSource),
            ):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = cmd_watch(args)
            self.assertEqual(rc, 0)
            self.assertEqual(captured["session_id"], sid)


# ---------------------------------------------------------------------------
# Handler — output mode dispatch
# ---------------------------------------------------------------------------


class CmdWatchOutputModeTests(TestCase):
    def test_json_mode_emits_one_object_per_line(self) -> None:
        with TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            sid = "s1"
            _scaffold_session(tmpdir, sid)
            _write_view(tmpdir, sid, phase="active")
            # Add a fresh journal event after WatchSource init so it gets emitted.
            from agent_relay.watch import WatchSource

            args = _make_args(
                repo=tmp,
                session_id=sid,
                no_follow=True,
                json_mode=True,
            )
            with (
                patch("agent_relay.cli.is_session", return_value=True),
                patch("agent_relay.watch.is_session", return_value=True),
            ):
                # Construct WatchSource manually to add an event between
                # construction and iteration so we exercise the streaming path.
                buf = io.StringIO()
                with redirect_stdout(buf):
                    # Patch WatchSource constructor to inject events on iter.
                    real_source = WatchSource(
                        tmpdir,
                        sid,
                        poll_interval=0.01,
                        follow=False,
                        heartbeat_interval=10.0,
                    )
                    _write_journal_event(tmpdir, sid, 1, "checkpoint.recorded")
                    with patch(
                        "agent_relay.cli.WatchSource", return_value=real_source
                    ):
                        rc = cmd_watch(args)

            self.assertEqual(rc, 0)
            lines = [
                line for line in buf.getvalue().splitlines() if line.strip()
            ]
            self.assertGreater(len(lines), 0)
            # Every line must parse as one compact JSON object.
            for line in lines:
                obj = json.loads(line)
                self.assertIn("kind", obj)
                self.assertIn("timestamp", obj)
                self.assertIn("sequence", obj)
            kinds = {json.loads(line)["kind"] for line in lines}
            self.assertIn("journal", kinds)

    def test_quiet_mode_emits_one_line_per_event(self) -> None:
        with TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            sid = "s1"
            _scaffold_session(tmpdir, sid)
            _write_view(tmpdir, sid, phase="active")

            from agent_relay.watch import WatchSource

            args = _make_args(
                repo=tmp,
                session_id=sid,
                no_follow=True,
                quiet=True,
            )
            with (
                patch("agent_relay.cli.is_session", return_value=True),
                patch("agent_relay.watch.is_session", return_value=True),
            ):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    real_source = WatchSource(
                        tmpdir,
                        sid,
                        poll_interval=0.01,
                        follow=False,
                        heartbeat_interval=10.0,
                    )
                    _write_journal_event(tmpdir, sid, 1, "session.started")
                    with patch(
                        "agent_relay.cli.WatchSource", return_value=real_source
                    ):
                        rc = cmd_watch(args)

            self.assertEqual(rc, 0)
            lines = [
                line for line in buf.getvalue().splitlines() if line.strip()
            ]
            self.assertGreater(len(lines), 0)
            # Every line should mention a kind keyword.
            joined = "\n".join(lines)
            self.assertIn("journal", joined)

    def test_default_mode_dispatches_to_live_renderer(self) -> None:
        with TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            sid = "s1"
            _scaffold_session(tmpdir, sid)
            _write_view(tmpdir, sid, phase="active")

            args = _make_args(
                repo=tmp, session_id=sid, no_follow=True, json_mode=False, quiet=False
            )
            live_renderer = MagicMock(return_value=0)
            stub_source = MagicMock()
            with (
                patch("agent_relay.cli.is_session", return_value=True),
                patch("agent_relay.cli.WatchSource", return_value=stub_source),
                patch("agent_relay.cli.render_watch_live", live_renderer),
            ):
                rc = cmd_watch(args)

            self.assertEqual(rc, 0)
            live_renderer.assert_called_once()
            self.assertIs(live_renderer.call_args.args[1], stub_source)


# ---------------------------------------------------------------------------
# Handler — no-follow snapshot exits cleanly
# ---------------------------------------------------------------------------


class CmdWatchNoFollowTests(TestCase):
    def test_no_follow_exits_after_one_pass(self) -> None:
        with TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            sid = "s1"
            _scaffold_session(tmpdir, sid)
            _write_view(tmpdir, sid, phase="active")

            args = _make_args(
                repo=tmp,
                session_id=sid,
                no_follow=True,
                json_mode=True,
            )
            with (
                patch("agent_relay.cli.is_session", return_value=True),
                patch("agent_relay.watch.is_session", return_value=True),
            ):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = cmd_watch(args)
            # Should return 0 even with no events to emit.
            self.assertEqual(rc, 0)
