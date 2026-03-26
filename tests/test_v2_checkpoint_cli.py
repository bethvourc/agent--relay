from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.v2_fixtures import build_sample_v2_session


class AgentRelayV2CheckpointCliTests(TestCase):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {"PYTHONPATH": str(ROOT / "src")}
        return subprocess.run(
            [sys.executable, "-m", "agent_relay.cli", *args],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
        )

    def init_git_repo(self, repo_root: Path) -> None:
        subprocess.run(["git", "init"], cwd=repo_root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.email", "relay@example.com"], cwd=repo_root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.name", "Relay Test"], cwd=repo_root, check=True, capture_output=True, text=True)
        (repo_root / "src").mkdir()
        (repo_root / "src" / "demo.py").write_text("print('demo')\n", encoding="utf-8")
        subprocess.run(["git", "add", "src/demo.py"], cwd=repo_root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=repo_root, check=True, capture_output=True, text=True)

    def test_prepare_routes_v2_session_through_tx_checkpoint_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            self.init_git_repo(repo_root)
            fixture = build_sample_v2_session(repo_root)

            result = self.run_cli(
                "--json",
                "prepare",
                fixture["session_id"],
                "--next-action",
                "Hand off with immutable checkpoint evidence",
                "--repo",
                tmpdir,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)

            inspect_result = self.run_cli("--json", "inspect", fixture["session_id"], "--repo", tmpdir)
            self.assertEqual(inspect_result.returncode, 0, inspect_result.stderr)
            inspect_data = json.loads(inspect_result.stdout)

            self.assertEqual(data["command"], "prepare")
            self.assertEqual(data["status"], "ready_for_handoff")
            self.assertEqual(data["capture_mode"], "git")
            self.assertEqual(inspect_data["current_status"], "ready_for_handoff")
            self.assertEqual(inspect_data["latest_checkpoint_id"], data["checkpoint_id"])
