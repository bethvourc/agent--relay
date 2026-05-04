"""Tests for the JSONL metrics exporter and `agent-relay metrics-tail` CLI."""

from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import MagicMock, patch

from agent_relay.cli import build_parser, cmd_metrics_tail
from agent_relay.exporters.jsonl import (
    parse_header_pairs,
    post_webhook,
    tail_jsonl,
)
from agent_relay.layout import (
    derived_view_path,
    turn_dir,
    turns_dir,
)
from agent_relay.ui import create_console
from agent_relay.watch import WatchEvent


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
        "objective": "test",
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
        "total_cost_usd": 0.4318,
        "usage": {"input_tokens": 6, "output_tokens": 56},
    }
    (tdir / "output.jsonl").write_text(json.dumps(result) + "\n", encoding="utf-8")
    (tdir / "state.json").write_text(json.dumps(state), encoding="utf-8")


def _make_args(
    *,
    repo: str,
    session_id: str | None = "s1",
    no_follow: bool = True,
    webhook: str | None = None,
    webhook_header: list[str] | None = None,
    webhook_timeout: float = 5.0,
) -> argparse.Namespace:
    return argparse.Namespace(
        repo=repo,
        session_id=session_id,
        no_follow=no_follow,
        poll_interval=0.01,
        webhook=webhook,
        webhook_header=webhook_header or [],
        webhook_timeout=webhook_timeout,
        json=False,
        quiet=False,
        console=create_console(),
    )


class _FakeSource:
    """Stand-in for WatchSource that yields a scripted list of events."""

    def __init__(self, repo_root: Path, session_id: str, events: list[WatchEvent]):
        self.repo_root = repo_root
        self.session_id = session_id
        self._events = events

    def iter_events(self):
        yield from self._events


def _turn_completed_event(turn_number: int, sequence: int = 1) -> WatchEvent:
    return WatchEvent(
        timestamp="2026-05-01T10:01:00.000Z",
        kind="turn_completed",
        payload={"turn_number": turn_number, "state": {}},
        sequence=sequence,
    )


def _status_change_event(to_status: str, sequence: int = 99) -> WatchEvent:
    return WatchEvent(
        timestamp="2026-05-01T10:05:00.000Z",
        kind="status_change",
        payload={"from_status": "active", "to_status": to_status},
        sequence=sequence,
    )


# ---------------------------------------------------------------------------
# parse_header_pairs
# ---------------------------------------------------------------------------


class ParseHeaderPairsTests(TestCase):
    def test_colon_separator(self) -> None:
        self.assertEqual(
            parse_header_pairs(["X-Token: abc"]), {"X-Token": "abc"}
        )

    def test_equals_separator(self) -> None:
        self.assertEqual(
            parse_header_pairs(["X-Token=abc"]), {"X-Token": "abc"}
        )

    def test_invalid_header_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_header_pairs(["malformed"])

    def test_empty_input_returns_empty_dict(self) -> None:
        self.assertEqual(parse_header_pairs(None), {})


# ---------------------------------------------------------------------------
# tail_jsonl — emission semantics
# ---------------------------------------------------------------------------


class TailJsonlEmissionTests(TestCase):
    def test_emits_turn_line_on_turn_completed(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _scaffold(repo, "s1")
            _write_view(repo, "s1")
            _write_turn(repo, "s1", 1)
            source = _FakeSource(repo, "s1", [_turn_completed_event(1)])
            buf = io.StringIO()
            rc = tail_jsonl(source, output=buf)
        self.assertEqual(rc, 0)
        lines = [line for line in buf.getvalue().splitlines() if line.strip()]
        self.assertGreaterEqual(len(lines), 1)
        first = json.loads(lines[0])
        self.assertEqual(first["kind"], "turn")
        self.assertEqual(first["turn_number"], 1)
        self.assertEqual(first["session_id"], "s1")
        self.assertAlmostEqual(first["cost_usd"], 0.4318)

    def test_emits_session_rollup_when_iterator_ends(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _scaffold(repo, "s1")
            _write_view(repo, "s1")
            _write_turn(repo, "s1", 1)
            source = _FakeSource(repo, "s1", [_turn_completed_event(1)])
            buf = io.StringIO()
            tail_jsonl(source, output=buf)
        lines = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
        kinds = [obj["kind"] for obj in lines]
        self.assertIn("turn", kinds)
        self.assertIn("session", kinds)
        # Session line should be last (rollup at the end).
        self.assertEqual(kinds[-1], "session")

    def test_emits_session_rollup_on_terminal_status(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _scaffold(repo, "s1")
            _write_view(repo, "s1")
            _write_turn(repo, "s1", 1)
            events = [
                _turn_completed_event(1, sequence=1),
                _status_change_event("completed", sequence=2),
                # Anything after terminal-status should not produce a
                # second session rollup.
                _turn_completed_event(2, sequence=3),
            ]
            _write_turn(repo, "s1", 2)
            source = _FakeSource(repo, "s1", events)
            buf = io.StringIO()
            tail_jsonl(source, output=buf)
        lines = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
        kinds = [obj["kind"] for obj in lines]
        # turn-1, then session rollup (terminal), then turn-2; no second session rollup.
        self.assertEqual(kinds.count("session"), 1)
        # Order: turn, session, then turn for the late turn-completed.
        self.assertEqual(kinds[1], "session")

    def test_ignores_non_turn_events(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _scaffold(repo, "s1")
            _write_view(repo, "s1")
            heartbeat = WatchEvent(
                timestamp="2026-05-01T10:01:00.000Z",
                kind="heartbeat",
                payload={"current_status": "active"},
                sequence=1,
            )
            workspace = WatchEvent(
                timestamp="2026-05-01T10:01:00.000Z",
                kind="workspace",
                payload={},
                sequence=2,
            )
            source = _FakeSource(repo, "s1", [heartbeat, workspace])
            buf = io.StringIO()
            tail_jsonl(source, output=buf)
        # Only a session rollup at the end (no turn lines).
        lines = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["kind"], "session")

    def test_keyboard_interrupt_returns_130(self) -> None:
        class _Boom:
            repo_root = Path("/tmp")
            session_id = "x"

            def iter_events(self):
                raise KeyboardInterrupt()

        rc = tail_jsonl(_Boom(), output=io.StringIO())
        self.assertEqual(rc, 130)


# ---------------------------------------------------------------------------
# Webhook delivery
# ---------------------------------------------------------------------------


class WebhookDeliveryTests(TestCase):
    def test_webhook_called_per_emitted_line(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _scaffold(repo, "s1")
            _write_view(repo, "s1")
            _write_turn(repo, "s1", 1)
            source = _FakeSource(repo, "s1", [_turn_completed_event(1)])
            posted: list[tuple[str, bytes, dict]] = []

            class _Resp:
                status = 200
                def __enter__(self): return self
                def __exit__(self, *a): return False

            def fake_urlopen(req, timeout=5.0):
                posted.append((req.full_url, req.data, dict(req.header_items())))
                return _Resp()

            with patch(
                "agent_relay.exporters.jsonl.urllib_request.urlopen",
                side_effect=fake_urlopen,
            ):
                rc = tail_jsonl(
                    source,
                    output=io.StringIO(),
                    webhook_url="http://example.test/hook",
                    webhook_headers={"X-Token": "abc"},
                )
        self.assertEqual(rc, 0)
        # 1 turn line + 1 session rollup
        self.assertEqual(len(posted), 2)
        urls = {p[0] for p in posted}
        self.assertEqual(urls, {"http://example.test/hook"})
        # Each body parses as JSON
        for _, body, headers in posted:
            obj = json.loads(body.decode("utf-8"))
            self.assertIn("kind", obj)
            # Header keys are case-insensitive in HTTP; ensure custom header is included.
            keys_lower = {k.lower() for k in headers}
            self.assertIn("x-token", keys_lower)
            self.assertIn("content-type", keys_lower)

    def test_webhook_5xx_logged_and_continues(self) -> None:
        from urllib.error import HTTPError

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _scaffold(repo, "s1")
            _write_view(repo, "s1")
            _write_turn(repo, "s1", 1)
            _write_turn(repo, "s1", 2)
            source = _FakeSource(
                repo,
                "s1",
                [_turn_completed_event(1, 1), _turn_completed_event(2, 2)],
            )

            err = HTTPError("http://example.test/hook", 500, "boom", {}, None)
            stderr_buf = io.StringIO()

            with (
                patch(
                    "agent_relay.exporters.jsonl.urllib_request.urlopen",
                    side_effect=err,
                ),
                patch("agent_relay.exporters.jsonl.sys.stderr", stderr_buf),
            ):
                rc = tail_jsonl(
                    source,
                    output=io.StringIO(),
                    webhook_url="http://example.test/hook",
                )
        self.assertEqual(rc, 0)  # Failure does NOT abort the stream
        log = stderr_buf.getvalue()
        self.assertIn("HTTP 500", log)

    def test_post_webhook_thin_wrapper(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _scaffold(repo, "s1")
            _write_view(repo, "s1")
            _write_turn(repo, "s1", 1)
            source = _FakeSource(repo, "s1", [_turn_completed_event(1)])

            class _Resp:
                status = 200
                def __enter__(self): return self
                def __exit__(self, *a): return False

            with patch(
                "agent_relay.exporters.jsonl.urllib_request.urlopen",
                return_value=_Resp(),
            ) as fake:
                rc = post_webhook(source, webhook_url="http://example.test/hook")
        self.assertEqual(rc, 0)
        self.assertGreaterEqual(fake.call_count, 1)


# ---------------------------------------------------------------------------
# CLI handler
# ---------------------------------------------------------------------------


class CmdMetricsTailTests(TestCase):
    def test_subparser_registered(self) -> None:
        parser = build_parser()
        ns = parser.parse_args(
            [
                "metrics-tail",
                "abc",
                "--no-follow",
                "--webhook",
                "http://x/y",
                "--webhook-header",
                "X-Token: t",
            ]
        )
        self.assertEqual(ns.command, "metrics-tail")
        self.assertEqual(ns.session_id, "abc")
        self.assertEqual(ns.webhook, "http://x/y")
        self.assertEqual(ns.webhook_header, ["X-Token: t"])
        self.assertTrue(ns.no_follow)

    def test_errors_when_no_sessions(self) -> None:
        with TemporaryDirectory() as tmp:
            args = _make_args(repo=tmp, session_id=None)
            rc = cmd_metrics_tail(args)
        self.assertEqual(rc, 2)

    def test_errors_with_unknown_session_id(self) -> None:
        with TemporaryDirectory() as tmp:
            args = _make_args(repo=tmp, session_id="nope")
            rc = cmd_metrics_tail(args)
        self.assertEqual(rc, 2)

    def test_invalid_webhook_header_returns_error(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _scaffold(repo, "s1")
            _write_view(repo, "s1")
            args = _make_args(
                repo=tmp, session_id="s1", webhook_header=["nocolon"]
            )
            with patch("agent_relay.cli.is_session", return_value=True):
                rc = cmd_metrics_tail(args)
        self.assertEqual(rc, 2)

    def test_dispatches_to_tail_jsonl(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _scaffold(repo, "s1")
            _write_view(repo, "s1")
            args = _make_args(repo=tmp, session_id="s1")
            stub = MagicMock()
            tail_mock = MagicMock(return_value=0)
            with (
                patch("agent_relay.cli.is_session", return_value=True),
                patch("agent_relay.cli.WatchSource", return_value=stub),
                patch("agent_relay.cli.tail_jsonl", tail_mock),
            ):
                rc = cmd_metrics_tail(args)
        self.assertEqual(rc, 0)
        tail_mock.assert_called_once()
        call = tail_mock.call_args
        self.assertIs(call.args[0], stub)
