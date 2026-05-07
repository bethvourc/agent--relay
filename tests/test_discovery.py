"""Tests for agent discovery functionality."""

from __future__ import annotations

import subprocess
from unittest import TestCase
from unittest.mock import MagicMock, patch

from agent_relay.agents import discover, require_available


class DiscoverTests(TestCase):
    @patch("shutil.which", return_value="/usr/local/bin/claude")
    @patch("agent_relay.agents._detect_version", return_value="claude 1.0.30")
    def test_discovers_available_agent(self, mock_version, mock_which) -> None:
        results = discover(["claude"])
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].available)
        self.assertEqual(results[0].cli_path, "/usr/local/bin/claude")
        self.assertEqual(results[0].version, "claude 1.0.30")

    @patch("shutil.which", return_value=None)
    def test_discovers_missing_agent(self, mock_which) -> None:
        results = discover(["codex"])
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].available)
        self.assertIsNone(results[0].cli_path)
        self.assertIsNone(results[0].version)

    @patch("shutil.which", side_effect=lambda cmd: f"/usr/bin/{cmd}")
    @patch("agent_relay.agents._detect_version", return_value="1.0.0")
    def test_discovers_all_registered(self, mock_version, mock_which) -> None:
        results = discover()
        self.assertGreaterEqual(len(results), 2)
        keys = {r.key for r in results}
        self.assertIn("claude", keys)
        self.assertIn("codex", keys)

    def test_skips_unknown_keys(self) -> None:
        results = discover(["nonexistent_agent"])
        self.assertEqual(len(results), 0)


class RequireAvailableTests(TestCase):
    @patch("shutil.which", return_value="/usr/bin/claude")
    def test_passes_when_available(self, mock_which) -> None:
        require_available(["claude"])  # Should not raise

    @patch("shutil.which", return_value=None)
    def test_raises_when_missing(self, mock_which) -> None:
        with self.assertRaises(SystemExit) as ctx:
            require_available(["claude"])
        self.assertIn("not found on PATH", str(ctx.exception))

    def test_raises_for_unknown_agent(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            require_available(["fake_agent"])
        self.assertIn("Unknown agent", str(ctx.exception))


class DetectVersionTests(TestCase):
    @patch("subprocess.run")
    def test_returns_first_line(self, mock_run) -> None:
        from agent_relay.agents import _detect_version

        mock_run.return_value = MagicMock(returncode=0, stdout="claude v1.0.30\nmore info")
        result = _detect_version("claude")
        self.assertEqual(result, "claude v1.0.30")

    @patch("subprocess.run")
    def test_returns_none_on_failure(self, mock_run) -> None:
        from agent_relay.agents import _detect_version

        mock_run.side_effect = FileNotFoundError
        result = _detect_version("nonexistent")
        self.assertIsNone(result)

    @patch("subprocess.run")
    def test_returns_none_on_timeout(self, mock_run) -> None:
        from agent_relay.agents import _detect_version

        mock_run.side_effect = subprocess.TimeoutExpired("cmd", 5)
        result = _detect_version("slow_cli")
        self.assertIsNone(result)
