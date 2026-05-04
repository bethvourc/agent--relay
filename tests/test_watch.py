"""Tests for the live watch source (agent_relay.watch)."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from agent_relay.layout import (
    derived_view_path,
    journal_dir,
    session_root,
    turn_dir,
    turns_dir,
    workspace_log_path,
)
from agent_relay.watch import (
    WatchSource,
    _JournalTail,
    _OutputTail,
    _StatusPoller,
    _TurnTail,
    _WorkspaceLogTail,
    is_terminal_status,
    pick_latest_active_session,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scaffold_session(tmpdir: Path, session_id: str = "test-session") -> Path:
    """Create the minimum directory layout a tail/poller needs."""
    journal_dir(tmpdir, session_id).mkdir(parents=True, exist_ok=True)
    turns_dir(tmpdir, session_id).mkdir(parents=True, exist_ok=True)
    derived_view_path(tmpdir, session_id).parent.mkdir(parents=True, exist_ok=True)
    return session_root(tmpdir, session_id)


def _write_journal_event(
    tmpdir: Path,
    session_id: str,
    sequence: int,
    event_type: str,
    *,
    phase_after: str = "active",
) -> Path:
    path = journal_dir(tmpdir, session_id) / f"{sequence:06d}-{event_type}.json"
    path.write_text(
        json.dumps(
            {
                "type": event_type,
                "sequence": sequence,
                "phase_after": phase_after,
                "event_id": f"ev-{sequence:06d}",
                "timestamp": "2026-05-03T12:00:00.000Z",
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_derived_view(
    tmpdir: Path,
    session_id: str,
    *,
    phase: str = "active",
    current_agent: str = "claude",
    objective: str = "test objective",
) -> None:
    path = derived_view_path(tmpdir, session_id)
    path.write_text(
        json.dumps(
            {
                "phase": phase,
                "current_agent": current_agent,
                "objective": objective,
            }
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# is_terminal_status
# ---------------------------------------------------------------------------


class IsTerminalStatusTests(TestCase):
    def test_completed_is_terminal(self) -> None:
        self.assertTrue(is_terminal_status("completed"))

    def test_ready_for_handoff_is_terminal(self) -> None:
        self.assertTrue(is_terminal_status("ready_for_handoff"))

    def test_active_is_not_terminal(self) -> None:
        self.assertFalse(is_terminal_status("active"))

    def test_none_is_not_terminal(self) -> None:
        self.assertFalse(is_terminal_status(None))


# ---------------------------------------------------------------------------
# _JournalTail
# ---------------------------------------------------------------------------


class JournalTailTests(TestCase):
    def test_yields_only_new_events(self) -> None:
        with TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            sid = "s1"
            _scaffold_session(tmpdir, sid)
            _write_journal_event(tmpdir, sid, 1, "session.started")

            tail = _JournalTail(tmpdir, sid)
            # Initial event was on disk before the watcher started -> skip.
            self.assertEqual(tail.poll(), [])

            _write_journal_event(tmpdir, sid, 2, "checkpoint.recorded")
            events = tail.poll()
            self.assertEqual(len(events), 1)
            kind, payload = events[0]
            self.assertEqual(kind, "journal")
            self.assertEqual(payload["sequence"], 2)
            self.assertEqual(payload["event_type"], "checkpoint.recorded")

            # Polling again with no new files yields nothing.
            self.assertEqual(tail.poll(), [])

    def test_handles_missing_directory(self) -> None:
        with TemporaryDirectory() as tmp:
            tail = _JournalTail(Path(tmp), "no-such-session")
            self.assertEqual(tail.poll(), [])

    def test_orders_events_by_sequence_even_if_filesystem_is_unsorted(self) -> None:
        with TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            sid = "s1"
            _scaffold_session(tmpdir, sid)
            tail = _JournalTail(tmpdir, sid)
            _write_journal_event(tmpdir, sid, 7, "launch.started")
            _write_journal_event(tmpdir, sid, 2, "session.started")
            _write_journal_event(tmpdir, sid, 5, "checkpoint.recorded")
            events = tail.poll()
            self.assertEqual([p["sequence"] for _, p in events], [2, 5, 7])


# ---------------------------------------------------------------------------
# _WorkspaceLogTail
# ---------------------------------------------------------------------------


class WorkspaceLogTailTests(TestCase):
    def test_picks_up_appended_entries_only(self) -> None:
        with TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            sid = "s1"
            _scaffold_session(tmpdir, sid)
            log_path = workspace_log_path(tmpdir, sid)

            # Pre-existing entry should not be re-emitted on first poll.
            log_path.write_text(
                "# Workspace Activity Log\n\n"
                "## [2026-05-03T12:00:00Z] Claude Code (slot 0) — Turn Complete\n\n"
                "first entry\n\n",
                encoding="utf-8",
            )

            tail = _WorkspaceLogTail(tmpdir, sid)
            self.assertEqual(tail.poll(), [])

            with log_path.open("a", encoding="utf-8") as f:
                f.write(
                    "## [2026-05-03T12:01:00Z] Codex (slot 1) — File Changed\n\n"
                    "edited src/foo.py\n\n"
                )

            events = tail.poll()
            self.assertEqual(len(events), 1)
            kind, payload = events[0]
            self.assertEqual(kind, "workspace")
            self.assertEqual(payload["agent"], "Codex")
            self.assertEqual(payload["slot"], 1)
            self.assertEqual(payload["entry_type"], "file_changed")
            self.assertEqual(payload["summary"], "edited src/foo.py")

    def test_no_events_when_log_missing(self) -> None:
        with TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            tail = _WorkspaceLogTail(tmpdir, "missing")
            self.assertEqual(tail.poll(), [])


# ---------------------------------------------------------------------------
# _TurnTail
# ---------------------------------------------------------------------------


class TurnTailTests(TestCase):
    def test_detects_new_turn_and_completion(self) -> None:
        with TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            sid = "s1"
            _scaffold_session(tmpdir, sid)

            tail = _TurnTail(tmpdir, sid)
            self.assertIsNone(tail.current_turn())

            # New turn 1 starts.
            t1 = turn_dir(tmpdir, sid, 1)
            t1.mkdir(parents=True)
            events = tail.poll()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0][0], "turn_started")
            self.assertEqual(events[0][1]["turn_number"], 1)
            self.assertEqual(tail.current_turn(), 1)

            # Turn completes.
            (t1 / "state.json").write_text(
                json.dumps({"summary": "did stuff", "status": "continue"}),
                encoding="utf-8",
            )
            events = tail.poll()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0][0], "turn_completed")
            self.assertEqual(events[0][1]["turn_number"], 1)
            self.assertEqual(events[0][1]["state"]["summary"], "did stuff")

    def test_pre_existing_turns_do_not_replay(self) -> None:
        with TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            sid = "s1"
            _scaffold_session(tmpdir, sid)
            t1 = turn_dir(tmpdir, sid, 1)
            t1.mkdir(parents=True)
            (t1 / "state.json").write_text("{}", encoding="utf-8")

            tail = _TurnTail(tmpdir, sid)
            # Already started AND completed when watcher began.
            self.assertEqual(tail.poll(), [])
            self.assertEqual(tail.current_turn(), 1)


# ---------------------------------------------------------------------------
# _OutputTail
# ---------------------------------------------------------------------------


class OutputTailTests(TestCase):
    def test_tolerates_partial_last_line(self) -> None:
        with TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            sid = "s1"
            _scaffold_session(tmpdir, sid)
            t1 = turn_dir(tmpdir, sid, 1)
            t1.mkdir(parents=True)
            out = t1 / "output.jsonl"

            tail = _OutputTail(tmpdir, sid)
            tail.reset_to_turn(1)

            # Write one complete line and a partial second line.
            out.write_text('{"a":1}\n{"b":2', encoding="utf-8")
            events = tail.poll()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0][0], "output_chunk")
            self.assertEqual(events[0][1]["parsed"], {"a": 1})

            # Polling again with no growth: nothing new.
            self.assertEqual(tail.poll(), [])

            # Finish the partial line — now it gets emitted.
            with out.open("a", encoding="utf-8") as f:
                f.write("}\n")
            events = tail.poll()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0][1]["parsed"], {"b": 2})

    def test_resets_when_turn_advances(self) -> None:
        with TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            sid = "s1"
            _scaffold_session(tmpdir, sid)
            t1 = turn_dir(tmpdir, sid, 1)
            t1.mkdir(parents=True)
            (t1 / "output.jsonl").write_text(
                '{"turn":1}\n', encoding="utf-8"
            )

            tail = _OutputTail(tmpdir, sid)
            tail.reset_to_turn(1)
            tail.poll()  # consume turn 1's output

            t2 = turn_dir(tmpdir, sid, 2)
            t2.mkdir(parents=True)
            (t2 / "output.jsonl").write_text(
                '{"turn":2}\n', encoding="utf-8"
            )
            tail.reset_to_turn(2)
            events = tail.poll()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0][1]["parsed"], {"turn": 2})

    def test_yields_nothing_before_turn_set(self) -> None:
        with TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            sid = "s1"
            _scaffold_session(tmpdir, sid)
            tail = _OutputTail(tmpdir, sid)
            self.assertEqual(tail.poll(), [])

    def test_tolerates_unparseable_json_lines(self) -> None:
        with TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            sid = "s1"
            _scaffold_session(tmpdir, sid)
            t1 = turn_dir(tmpdir, sid, 1)
            t1.mkdir(parents=True)
            (t1 / "output.jsonl").write_text("not json\n", encoding="utf-8")
            tail = _OutputTail(tmpdir, sid)
            tail.reset_to_turn(1)
            events = tail.poll()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0][1]["line"], "not json")
            self.assertEqual(events[0][1]["event_subtype"], "raw")


# ---------------------------------------------------------------------------
# _StatusPoller
# ---------------------------------------------------------------------------


class StatusPollerTests(TestCase):
    def test_emits_event_only_on_transition(self) -> None:
        with TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            sid = "s1"
            _scaffold_session(tmpdir, sid)
            _write_derived_view(tmpdir, sid, phase="active", current_agent="claude")

            poller = _StatusPoller(tmpdir, sid)
            self.assertEqual(poller.status(), "active")
            self.assertEqual(poller.poll(), [])

            _write_derived_view(tmpdir, sid, phase="completed", current_agent="claude")
            events = poller.poll()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0][0], "status_change")
            self.assertEqual(events[0][1]["from_status"], "active")
            self.assertEqual(events[0][1]["to_status"], "completed")

    def test_handles_missing_view_gracefully(self) -> None:
        with TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            sid = "s1"
            session_root(tmpdir, sid).mkdir(parents=True)
            poller = _StatusPoller(tmpdir, sid)
            self.assertIsNone(poller.status())
            self.assertEqual(poller.poll(), [])


# ---------------------------------------------------------------------------
# pick_latest_active_session
# ---------------------------------------------------------------------------


class PickLatestActiveSessionTests(TestCase):
    def test_returns_newest_active_when_multiple(self) -> None:
        rows = [
            {
                "session_id": "old-active",
                "current_status": "active",
                "updated_at": "2026-05-03T10:00:00Z",
                "health": "healthy",
            },
            {
                "session_id": "new-active",
                "current_status": "active",
                "updated_at": "2026-05-03T12:00:00Z",
                "health": "healthy",
            },
            {
                "session_id": "completed",
                "current_status": "completed",
                "updated_at": "2026-05-03T13:00:00Z",
                "health": "healthy",
            },
        ]
        # list_sessions_for_dashboard sorts by (updated_at, session_id) DESC.
        rows_sorted = sorted(
            rows, key=lambda r: (r["updated_at"], r["session_id"]), reverse=True
        )
        with patch(
            "agent_relay.watch.list_sessions_for_dashboard",
            return_value=rows_sorted,
        ):
            self.assertEqual(
                pick_latest_active_session(Path("/tmp/anything")),
                "new-active",
            )

    def test_returns_none_when_no_active(self) -> None:
        rows = [
            {
                "session_id": "completed",
                "current_status": "completed",
                "updated_at": "2026-05-03T13:00:00Z",
                "health": "healthy",
            },
        ]
        with patch(
            "agent_relay.watch.list_sessions_for_dashboard", return_value=rows
        ):
            self.assertIsNone(pick_latest_active_session(Path("/tmp/anything")))

    def test_skips_corrupt_sessions(self) -> None:
        rows = [
            {
                "session_id": "bad",
                "current_status": "active",
                "updated_at": "2026-05-03T13:00:00Z",
                "health": "corrupt",
            },
        ]
        with patch(
            "agent_relay.watch.list_sessions_for_dashboard", return_value=rows
        ):
            self.assertIsNone(pick_latest_active_session(Path("/tmp/anything")))


# ---------------------------------------------------------------------------
# WatchSource end-to-end
# ---------------------------------------------------------------------------


class WatchSourceTests(TestCase):
    def _make_source(
        self, tmpdir: Path, sid: str, **kwargs
    ) -> WatchSource:
        with patch("agent_relay.watch.is_session", return_value=True):
            return WatchSource(tmpdir, sid, **kwargs)

    def test_iter_events_terminates_on_completed_status(self) -> None:
        with TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            sid = "s1"
            _scaffold_session(tmpdir, sid)
            _write_derived_view(tmpdir, sid, phase="active")

            source = self._make_source(
                tmpdir,
                sid,
                poll_interval=0.01,
                heartbeat_interval=10.0,
                sleep=lambda _s: _write_derived_view(
                    tmpdir, sid, phase="completed"
                ),
            )
            events = list(source.iter_events())

            # Status-change event for active->completed should be present, and
            # iteration should terminate after that poll.
            kinds = [e.kind for e in events]
            self.assertIn("status_change", kinds)
            self.assertEqual(events[-1].kind, "status_change")
            self.assertEqual(events[-1].payload["to_status"], "completed")

    def test_iter_events_no_follow_emits_one_pass_then_exits(self) -> None:
        with TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            sid = "s1"
            _scaffold_session(tmpdir, sid)
            _write_derived_view(tmpdir, sid, phase="active")
            _write_journal_event(tmpdir, sid, 1, "session.started")

            # Note: pre-existing events are skipped by tails on init, so we
            # need to add a fresh one *between* construction and iteration.
            source = self._make_source(
                tmpdir, sid, follow=False, heartbeat_interval=10.0
            )
            _write_journal_event(tmpdir, sid, 2, "checkpoint.recorded")
            events = list(source.iter_events())

            kinds = [e.kind for e in events]
            self.assertIn("journal", kinds)
            # Heartbeat may or may not fire depending on timing; just confirm
            # the loop exited (no exception, finite list).
            self.assertGreater(len(events), 0)

    def test_snapshot_reflects_current_state(self) -> None:
        with TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            sid = "s1"
            _scaffold_session(tmpdir, sid)
            _write_derived_view(
                tmpdir, sid, phase="active", current_agent="codex",
                objective="ship the watch command",
            )
            t1 = turn_dir(tmpdir, sid, 2)
            t1.mkdir(parents=True)
            (t1 / "state.json").write_text(
                json.dumps({"summary": "halfway done"}),
                encoding="utf-8",
            )

            source = self._make_source(tmpdir, sid)
            snap = source.snapshot()

            self.assertEqual(snap.session_id, sid)
            self.assertEqual(snap.current_agent, "codex")
            self.assertEqual(snap.current_status, "active")
            self.assertEqual(snap.objective, "ship the watch command")
            self.assertEqual(snap.current_turn, 2)
            self.assertEqual(snap.last_turn_state, {"summary": "halfway done"})

    def test_constructor_raises_on_unknown_session(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch("agent_relay.watch.is_session", return_value=False):
                with self.assertRaises(ValueError):
                    WatchSource(Path(tmp), "missing")

    def test_iter_events_emits_progressive_output_chunks(self) -> None:
        with TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            sid = "s1"
            _scaffold_session(tmpdir, sid)
            _write_derived_view(tmpdir, sid, phase="active")

            # Iteration plan, driven by the patched sleep:
            # poll 0: nothing on disk
            # poll 1: turn-001 dir appears + first output line
            # poll 2: second output line appears
            # poll 3: derived view flips to completed -> exit
            polls = {"n": 0}
            t1 = turn_dir(tmpdir, sid, 1)
            output = t1 / "output.jsonl"

            def fake_sleep(_s: float) -> None:
                polls["n"] += 1
                if polls["n"] == 1:
                    t1.mkdir(parents=True)
                    output.write_text('{"event":"a"}\n', encoding="utf-8")
                elif polls["n"] == 2:
                    with output.open("a", encoding="utf-8") as f:
                        f.write('{"event":"b"}\n')
                elif polls["n"] == 3:
                    _write_derived_view(tmpdir, sid, phase="completed")

            source = self._make_source(
                tmpdir, sid,
                poll_interval=0.01,
                heartbeat_interval=10.0,
                sleep=fake_sleep,
            )
            events = list(source.iter_events())

            kinds = [e.kind for e in events]
            self.assertIn("turn_started", kinds)
            self.assertGreaterEqual(kinds.count("output_chunk"), 2)
            chunks = [e for e in events if e.kind == "output_chunk"]
            self.assertEqual(chunks[0].payload["parsed"], {"event": "a"})
            self.assertEqual(chunks[1].payload["parsed"], {"event": "b"})
