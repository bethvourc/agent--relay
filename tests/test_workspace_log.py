"""Tests for workspace log module."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import TestCase

from agent_relay.workspace_log import LogEntry, WorkspaceLog, utc_timestamp


class WorkspaceLogTests(TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.log_path = Path(self._tmpdir.name) / "workspace-log.md"

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_append_creates_file_with_header(self) -> None:
        wlog = WorkspaceLog(self.log_path)
        entry = LogEntry(
            timestamp="2025-01-01T00:00:00Z",
            agent_key="claude",
            agent_slot=0,
            entry_type="turn_complete",
            summary="Analyzed the codebase.",
        )
        wlog.append(entry)
        content = self.log_path.read_text()
        self.assertIn("# Workspace Activity Log", content)
        self.assertIn("Claude Code", content)
        self.assertIn("Analyzed the codebase.", content)

    def test_append_multiple_entries(self) -> None:
        wlog = WorkspaceLog(self.log_path)
        wlog.append(LogEntry("2025-01-01T00:00:00Z", "claude", 0, "turn_complete", "First turn."))
        wlog.append(LogEntry("2025-01-01T00:01:00Z", "codex", 1, "turn_complete", "Second turn."))
        content = self.log_path.read_text()
        self.assertIn("First turn.", content)
        self.assertIn("Second turn.", content)
        self.assertIn("Claude Code", content)
        self.assertIn("Codex", content)

    def test_read_all_returns_empty_for_missing_file(self) -> None:
        wlog = WorkspaceLog(self.log_path)
        entries = wlog.read_all()
        self.assertEqual(entries, [])

    def test_read_all_roundtrips_entries(self) -> None:
        wlog = WorkspaceLog(self.log_path)
        wlog.append(LogEntry("2025-01-01T00:00:00Z", "claude", 0, "turn_complete", "Did analysis."))
        wlog.append(LogEntry("2025-01-01T00:01:00Z", "codex", 1, "signal", "Task done."))

        entries = wlog.read_all()
        self.assertEqual(len(entries), 2)

        self.assertEqual(entries[0].timestamp, "2025-01-01T00:00:00Z")
        self.assertEqual(entries[0].agent_key, "claude")
        self.assertEqual(entries[0].agent_slot, 0)
        self.assertEqual(entries[0].entry_type, "turn_complete")
        self.assertEqual(entries[0].summary, "Did analysis.")

        self.assertEqual(entries[1].timestamp, "2025-01-01T00:01:00Z")
        self.assertEqual(entries[1].agent_key, "codex")
        self.assertEqual(entries[1].agent_slot, 1)
        self.assertEqual(entries[1].entry_type, "signal")
        self.assertEqual(entries[1].summary, "Task done.")

    def test_path_property(self) -> None:
        wlog = WorkspaceLog(self.log_path)
        self.assertEqual(wlog.path, self.log_path)

    def test_entry_type_formatting(self) -> None:
        wlog = WorkspaceLog(self.log_path)
        wlog.append(
            LogEntry("2025-01-01T00:00:00Z", "claude", 0, "file_changed", "Updated main.py.")
        )
        content = self.log_path.read_text()
        self.assertIn("File Changed", content)

    def test_utc_timestamp_format(self) -> None:
        ts = utc_timestamp()
        # Should match ISO format: YYYY-MM-DDTHH:MM:SSZ
        self.assertRegex(ts, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_header_written_only_once(self) -> None:
        wlog = WorkspaceLog(self.log_path)
        wlog.append(LogEntry("2025-01-01T00:00:00Z", "claude", 0, "turn_complete", "First."))
        wlog.append(LogEntry("2025-01-01T00:01:00Z", "codex", 1, "turn_complete", "Second."))
        content = self.log_path.read_text()
        self.assertEqual(content.count("# Workspace Activity Log"), 1)
