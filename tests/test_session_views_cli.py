from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.session_fixtures import build_sample_session


class AgentRelaySessionViewsCliTests(TestCase):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {"PYTHONPATH": str(ROOT / "src")}
        return subprocess.run(
            [sys.executable, "-m", "agent_relay.cli", *args],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
        )

    def test_inspect_reads_session_and_rebuilds_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            fixture = build_sample_session(repo_root)
            view_path = repo_root / ".agent-relay" / "sessions" / fixture["session_id"] / "derived" / "view.json"
            head_path = repo_root / ".agent-relay" / "sessions" / fixture["session_id"] / "refs" / "head.json"
            if view_path.exists():
                view_path.unlink()
            if head_path.exists():
                head_path.unlink()

            result = self.run_cli("--json", "inspect", fixture["session_id"], "--repo", tmpdir)
            self.assertEqual(result.returncode, 0, result.stderr)

            data = json.loads(result.stdout)
            self.assertEqual(data["storage_model"], "journal_v2")
            self.assertEqual(data["current_agent"], "codex")
            self.assertEqual(data["latest_checkpoint_id"], fixture["checkpoint_two_id"])
            self.assertTrue(view_path.exists())
            self.assertTrue(head_path.exists())

    def test_dashboard_surfaces_corrupt_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            fixture = build_sample_session(repo_root, session_id="20260325-180000-good111")
            build_sample_session(repo_root, session_id="20260325-180500-bad222")
            bad_manifest = repo_root / ".agent-relay" / "sessions" / "20260325-180500-bad222" / "session.json"
            bad_manifest.write_text("{bad json\n", encoding="utf-8")

            result = self.run_cli("--json", "dashboard", "--repo", tmpdir)
            self.assertEqual(result.returncode, 0, result.stderr)

            data = json.loads(result.stdout)
            by_id = {entry["session_id"]: entry for entry in data["sessions"]}
            self.assertEqual(by_id[fixture["session_id"]]["health"], "healthy")
            self.assertEqual(by_id["20260325-180500-bad222"]["health"], "corrupt")
            self.assertEqual(by_id["20260325-180500-bad222"]["status"], "corrupt")
            self.assertIn("session manifest", by_id["20260325-180500-bad222"]["objective"])

    def test_inspect_reports_corrupt_session_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            fixture = build_sample_session(repo_root)
            manifest_path = repo_root / ".agent-relay" / "sessions" / fixture["session_id"] / "session.json"
            manifest_path.write_text("{bad json\n", encoding="utf-8")

            result = self.run_cli("--json", "inspect", fixture["session_id"], "--repo", tmpdir)

            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            self.assertEqual(data["health"], "corrupt")
            self.assertIn("session manifest", data["error"])
            self.assertTrue(data["broken_paths"])

    def test_inspect_reports_corrupt_v1_session_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            session_root = repo_root / ".agent-relay" / "sessions" / "broken-v1"
            session_root.mkdir(parents=True, exist_ok=True)
            (session_root / "state.json").write_text("{bad json\n", encoding="utf-8")

            result = self.run_cli("--json", "inspect", "broken-v1", "--repo", tmpdir)

            self.assertNotEqual(result.returncode, 0)
            data = json.loads(result.stdout)
            self.assertIn("corrupt", data["error"])
