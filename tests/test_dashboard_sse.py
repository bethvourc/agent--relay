"""Phase E — Server-Sent Events live dashboard updates."""

from __future__ import annotations

import http.client
import json
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from agent_relay.metrics import CrossSessionMetrics, SessionMetrics, TokenUsage
from agent_relay.sse import (
    format_event,
    format_heartbeat,
    format_retry,
    stream_updates,
)
from tests._dashboard_test_helpers import (
    get_dashboard,
    start_dashboard_server,
    stop_dashboard_server,
)
from tests.session_fixtures import build_sample_session
from tests.test_dashboard_session import _write_turn_artifacts


def _metrics(*sessions: SessionMetrics) -> CrossSessionMetrics:
    return CrossSessionMetrics(
        sessions=sessions,
        by_agent={"claude": TokenUsage(input=1, output=1)},
        cost_by_agent={"claude": 0.0},
        by_day={},
        total_tokens=TokenUsage(input=1, output=1),
        total_cost_usd=0.0,
        total_duration_ms=1,
        session_count=len(sessions),
    )


def _session(session_id: str = "s1") -> SessionMetrics:
    return SessionMetrics(
        session_id=session_id,
        current_agent="claude",
        current_status="active",
        objective="SSE test",
        started_at="2026-05-09T10:00:00Z",
        updated_at="2026-05-09T10:00:00Z",
        turn_count=0,
        successful_turns=0,
        total_tokens=TokenUsage(),
        total_cost_usd=None,
        total_duration_ms=0,
        turns=(),
    )


def _open_stream(
    port: int, path: str
) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    conn.request("GET", path, headers={"Accept": "text/event-stream"})
    return conn, conn.getresponse()


def _read_frame(resp: http.client.HTTPResponse) -> str:
    lines: list[str] = []
    while True:
        line = resp.readline().decode("utf-8")
        if line == "":
            break
        lines.append(line)
        if line == "\n":
            break
    return "".join(lines)


def _read_until(resp: http.client.HTTPResponse, needle: str, *, frames: int = 6) -> str:
    seen: list[str] = []
    for _ in range(frames):
        frame = _read_frame(resp)
        seen.append(frame)
        if needle in frame:
            return frame
    raise AssertionError(f"did not find {needle!r} in frames: {seen!r}")


def _json_from_update(frame: str) -> dict[str, object]:
    data = "\n".join(
        line.removeprefix("data: ") for line in frame.splitlines() if line.startswith("data: ")
    )
    return json.loads(data)


class SSEFormatTests(TestCase):
    def test_format_event_single_line(self) -> None:
        self.assertEqual(format_event("update", '{"a":1}'), b'event: update\ndata: {"a":1}\n\n')

    def test_format_event_multi_line_data(self) -> None:
        frame = format_event("update", "a\nb").decode("utf-8")
        self.assertIn("data: a\n", frame)
        self.assertIn("data: b\n", frame)

    def test_format_event_no_event_name_emits_only_data(self) -> None:
        frame = format_event(None, "payload").decode("utf-8")
        self.assertNotIn("event:", frame)
        self.assertEqual(frame, "data: payload\n\n")

    def test_format_heartbeat_is_a_comment_line(self) -> None:
        self.assertTrue(format_heartbeat().startswith(b": heartbeat "))

    def test_format_retry_emits_retry_field(self) -> None:
        self.assertEqual(format_retry(), b"retry: 3000\n\n")


class StreamUpdatesTests(TestCase):
    def test_stream_updates_emits_initial_update_then_idles(self) -> None:
        stop = threading.Event()
        frames = stream_updates(
            build_payload=lambda: {"generatedAt": str(time.time()), "regions": {"x": "1"}},
            stop_event=stop,
            tick_seconds=0,
            heartbeat_seconds=0,
        )

        self.assertEqual(next(frames), b"retry: 3000\n\n")
        self.assertIn(b"event: update", next(frames))
        self.assertIn(b": heartbeat ", next(frames))
        stop.set()
        self.assertIn(b"event: shutdown", next(frames))

    def test_stream_updates_emits_update_on_change(self) -> None:
        stop = threading.Event()
        count = 0

        def build() -> dict[str, object]:
            nonlocal count
            count += 1
            return {"regions": {"x": str(count)}}

        frames = stream_updates(
            build_payload=build,
            stop_event=stop,
            tick_seconds=0,
            heartbeat_seconds=100,
        )

        self.assertEqual(next(frames), b"retry: 3000\n\n")
        self.assertIn(b'"x":"1"', next(frames))
        self.assertIn(b'"x":"2"', next(frames))
        stop.set()

    def test_stream_updates_stops_on_event(self) -> None:
        stop = threading.Event()
        frames = stream_updates(
            build_payload=lambda: {"regions": {"x": "1"}},
            stop_event=stop,
            tick_seconds=0,
        )
        self.assertEqual(next(frames), b"retry: 3000\n\n")
        stop.set()
        self.assertIn(b"event: shutdown", next(frames))
        with self.assertRaises(StopIteration):
            next(frames)

    def test_stream_updates_swallows_build_payload_errors(self) -> None:
        stop = threading.Event()
        calls = 0

        def build() -> dict[str, object]:
            nonlocal calls
            calls += 1
            if calls % 2:
                raise RuntimeError("transient")
            return {"regions": {"x": "ok"}}

        frames = stream_updates(
            build_payload=build,
            stop_event=stop,
            tick_seconds=0,
            heartbeat_seconds=0,
        )

        self.assertEqual(next(frames), b"retry: 3000\n\n")
        self.assertIn(b": heartbeat ", next(frames))
        self.assertIn(b"event: update", next(frames))
        stop.set()


class DashboardSSELiveTests(TestCase):
    def test_events_path_serves_text_event_stream(self) -> None:
        server, thread, port = start_dashboard_server(
            self,
            repo_root=Path("."),
            extractor=lambda _: _metrics(_session()),
            dashboard_refresh_interval=0.01,
        )
        conn: http.client.HTTPConnection | None = None
        try:
            conn, resp = _open_stream(port, "/events")
            self.assertEqual(resp.status, 200)
            self.assertIn("text/event-stream", resp.getheader("Content-Type", ""))
            self.assertEqual(_read_frame(resp), "retry: 3000\n\n")
            frame = _read_until(resp, "event: update")
            payload = _json_from_update(frame)
            self.assertIn("regions", payload)
        finally:
            if conn:
                conn.close()
            stop_dashboard_server(server, thread)

    def test_dashboard_events_alias_matches_refresh_endpoint_derivation(self) -> None:
        server, thread, port = start_dashboard_server(
            self,
            repo_root=Path("."),
            extractor=lambda _: _metrics(_session()),
            dashboard_refresh_interval=0.01,
        )
        conn: http.client.HTTPConnection | None = None
        try:
            conn, resp = _open_stream(port, "/dashboard/events")
            self.assertEqual(resp.status, 200)
            self.assertIn("text/event-stream", resp.getheader("Content-Type", ""))
        finally:
            if conn:
                conn.close()
            stop_dashboard_server(server, thread)

    def test_alerts_events_serves_alerts_payload(self) -> None:
        server, thread, port = start_dashboard_server(
            self,
            repo_root=Path("."),
            extractor=lambda _: _metrics(_session()),
            dashboard_refresh_interval=0.01,
        )
        conn: http.client.HTTPConnection | None = None
        try:
            conn, resp = _open_stream(port, "/alerts/events")
            self.assertEqual(resp.status, 200)
            _read_frame(resp)
            payload = _json_from_update(_read_until(resp, "event: update"))
            regions = payload["regions"]
            assert isinstance(regions, dict)
            self.assertIn("alerts-list", regions)
        finally:
            if conn:
                conn.close()
            stop_dashboard_server(server, thread)

    def test_session_events_404_for_unknown_session(self) -> None:
        server, thread, port = start_dashboard_server(
            self,
            repo_root=Path("."),
            extractor=lambda _: _metrics(),
        )
        try:
            status, content_type, body = get_dashboard(port, "/session/unknown/events")
            self.assertEqual(status, 404)
            self.assertIn("text/html", content_type)
            self.assertIn("session not found", body)
        finally:
            stop_dashboard_server(server, thread)

    def test_turn_events_404_for_unknown_turn(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            fixture = build_sample_session(repo)
            server, thread, port = start_dashboard_server(self, repo_root=repo)
            try:
                status, content_type, body = get_dashboard(
                    port,
                    f"/session/{fixture['session_id']}/turn/999/events",
                )
                self.assertEqual(status, 404)
                self.assertIn("text/html", content_type)
                self.assertIn("turn not found", body)
            finally:
                stop_dashboard_server(server, thread)

    def test_events_path_forwards_filter_query(self) -> None:
        captured = []

        def extractor(_repo: Path, *, filter=None):
            captured.append(filter)
            return _metrics(_session())

        server, thread, port = start_dashboard_server(
            self,
            repo_root=Path("."),
            extractor=extractor,
            dashboard_refresh_interval=0.01,
        )
        conn: http.client.HTTPConnection | None = None
        try:
            conn, resp = _open_stream(port, "/events?agent=claude")
            self.assertEqual(resp.status, 200)
            _read_until(resp, "event: update")
            self.assertTrue(captured)
            self.assertEqual(captured[-1].agents, ("claude",))
        finally:
            if conn:
                conn.close()
            stop_dashboard_server(server, thread)

    def test_events_idle_emits_heartbeat_within_window(self) -> None:
        server, thread, port = start_dashboard_server(
            self,
            repo_root=Path("."),
            extractor=lambda _: _metrics(_session()),
            dashboard_refresh_interval=0.01,
            sse_heartbeat_interval=0.02,
        )
        conn: http.client.HTTPConnection | None = None
        try:
            conn, resp = _open_stream(port, "/events")
            self.assertEqual(resp.status, 200)
            _read_frame(resp)
            _read_until(resp, "event: update")
            heartbeat = _read_until(resp, ": heartbeat", frames=10)
            self.assertTrue(heartbeat.startswith(": heartbeat "))
        finally:
            if conn:
                conn.close()
            stop_dashboard_server(server, thread)

    def test_concurrent_connection_cap(self) -> None:
        server, thread, port = start_dashboard_server(
            self,
            repo_root=Path("."),
            extractor=lambda _: _metrics(_session()),
            dashboard_refresh_interval=0.05,
            sse_connection_cap=1,
        )
        first: http.client.HTTPConnection | None = None
        second: http.client.HTTPConnection | None = None
        try:
            first, resp1 = _open_stream(port, "/events")
            self.assertEqual(resp1.status, 200)

            second = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            second.request("GET", "/alerts/events")
            resp2 = second.getresponse()
            body = resp2.read().decode("utf-8")
            self.assertEqual(resp2.status, 503)
            self.assertIn("too many connections", body)
        finally:
            if second:
                second.close()
            if first:
                first.close()
            stop_dashboard_server(server, thread)

    def test_shutdown_event_emitted_on_server_stop(self) -> None:
        server, thread, port = start_dashboard_server(
            self,
            repo_root=Path("."),
            extractor=lambda _: _metrics(_session()),
            dashboard_refresh_interval=0.01,
        )
        conn: http.client.HTTPConnection | None = None
        try:
            conn, resp = _open_stream(port, "/events")
            self.assertEqual(resp.status, 200)
            _read_frame(resp)
            _read_until(resp, "event: update")
            server.shutdown()
            frame = _read_until(resp, "event: shutdown", frames=10)
            self.assertIn("data: {}", frame)
            thread.join(timeout=2)
        finally:
            if conn:
                conn.close()
            if thread.is_alive():
                stop_dashboard_server(server, thread)

    def test_turn_events_serves_turn_payload(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            fixture = build_sample_session(repo)
            _write_turn_artifacts(repo, fixture["session_id"], 3)
            server, thread, port = start_dashboard_server(
                self,
                repo_root=repo,
                dashboard_refresh_interval=0.01,
            )
            conn: http.client.HTTPConnection | None = None
            try:
                conn, resp = _open_stream(port, f"/session/{fixture['session_id']}/turn/3/events")
                self.assertEqual(resp.status, 200)
                _read_frame(resp)
                payload = _json_from_update(_read_until(resp, "event: update"))
                regions = payload["regions"]
                assert isinstance(regions, dict)
                self.assertIn("prompt", regions)
            finally:
                if conn:
                    conn.close()
                stop_dashboard_server(server, thread)
