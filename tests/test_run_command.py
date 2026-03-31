from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_relay.storage import load_session_view


class RunCommandTests(TestCase):
    def run_cli(
        self, *args: str, extra_env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        env = {
            "PYTHONPATH": str(ROOT / "src"),
        }
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [sys.executable, "-m", "agent_relay.cli", *args],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def init_git_repo(self, repo_root: Path) -> None:
        subprocess.run(
            ["git", "init"], cwd=repo_root, check=True, capture_output=True, text=True
        )
        subprocess.run(
            ["git", "config", "user.email", "relay@example.com"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Relay Test"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
        (repo_root / "src").mkdir()
        (repo_root / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "."],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )

    def install_fake_claude(self, tmpdir: str) -> tuple[str, Path]:
        fake_bin = Path(tmpdir) / "fake-bin"
        fake_bin.mkdir()
        output_file = fake_bin / "claude-output.jsonl"
        script = fake_bin / "claude"
        script.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "import os",
                    "from pathlib import Path",
                    'output_path = os.environ.get("AGENT_RELAY_FAKE_OUTPUT_FILE", "")',
                    "if output_path and Path(output_path).exists():",
                    '    print(Path(output_path).read_text(encoding="utf-8"), end="")',
                    "else:",
                    "    print(",
                    '        \'{"message":{"role":"assistant","content":[{"type":"text","text":"Default reply\\\\nRELAY_STATUS: {\\\\\\"status\\\\\\":\\\\\\"propose_done\\\\\\",\\\\\\"reason\\\\\\":\\\\\\"Done\\\\\\",\\\\\\"remaining_work\\\\\\":[],\\\\\\"verification\\\\\\":[]}"}}]}\'',
                    "    )",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        path = f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"
        return path, output_file

    def write_fake_claude_output(self, output_file: Path, text: str) -> None:
        payload = json.dumps(
            {
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": text}],
                },
            }
        )
        output_file.write_text(payload + "\n", encoding="utf-8")

    def test_run_command_creates_managed_session_and_handoff_reuses_saved_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            self.init_git_repo(repo_root)
            path, output_file = self.install_fake_claude(tmpdir)
            self.write_fake_claude_output(
                output_file,
                "\n".join(
                    [
                        "Inspected auth flow",
                        'RELAY_STATE: {"summary":"Inspected auth flow","current_plan":["patch auth middleware"],"intended_edits":["src/app.py"],"next_step":"edit src/app.py"}',
                        'RELAY_STATUS: {"status":"propose_done","reason":"Ready for handoff","remaining_work":[],"verification":["pytest tests/test_run_command.py"]}',
                    ]
                ),
            )
            env = {
                "PATH": path,
                "AGENT_RELAY_FAKE_OUTPUT_FILE": str(output_file),
            }

            run_result = self.run_cli(
                "--json",
                "run",
                "claude",
                "Stabilize auth handoff",
                "--repo",
                tmpdir,
                extra_env=env,
            )

            self.assertEqual(run_result.returncode, 0, run_result.stderr)
            run_data = json.loads(run_result.stdout)
            self.assertEqual(run_data["command"], "run")
            self.assertEqual(run_data["agent"], "claude")
            self.assertEqual(run_data["stop_reason"], "done_signal")
            self.assertEqual(run_data["turns_completed"], 1)

            state_path = (
                repo_root
                / ".agent-relay"
                / "sessions"
                / run_data["session_id"]
                / "turns"
                / "turn-001"
                / "state.json"
            )
            self.assertTrue(state_path.exists())

            handoff_result = self.run_cli(
                "--json",
                "codex",
                "--task",
                "Continue from the managed Claude run",
                "--no-launch",
                "--repo",
                tmpdir,
                extra_env=env,
            )

            self.assertEqual(handoff_result.returncode, 0, handoff_result.stderr)
            handoff_data = json.loads(handoff_result.stdout)
            self.assertEqual(handoff_data["session_id"], run_data["session_id"])

            packet_text = Path(handoff_data["resume_path"]).read_text(encoding="utf-8")
            self.assertIn("## Resumable State", packet_text)
            self.assertIn("Inspected auth flow", packet_text)
            self.assertIn("patch auth middleware", packet_text)
            self.assertIn("relay/turns/turn-001/state.json", packet_text)

    def test_run_command_continue_uses_prior_objective_when_task_is_omitted(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            self.init_git_repo(repo_root)
            path, output_file = self.install_fake_claude(tmpdir)
            self.write_fake_claude_output(
                output_file,
                "\n".join(
                    [
                        "Audited auth flow",
                        'RELAY_STATE: {"summary":"Audited auth flow","current_plan":["review middleware"],"next_step":"review middleware"}',
                        'RELAY_STATUS: {"status":"propose_done","reason":"Done for now","remaining_work":[],"verification":["pytest tests/test_run_command.py"]}',
                    ]
                ),
            )
            env = {
                "PATH": path,
                "AGENT_RELAY_FAKE_OUTPUT_FILE": str(output_file),
            }

            first_result = self.run_cli(
                "--json",
                "run",
                "claude",
                "Audit auth flow",
                "--repo",
                tmpdir,
                extra_env=env,
            )
            self.assertEqual(first_result.returncode, 0, first_result.stderr)
            first_data = json.loads(first_result.stdout)

            second_result = self.run_cli(
                "--json",
                "run",
                "claude",
                "--continue",
                first_data["session_id"],
                "--repo",
                tmpdir,
                extra_env=env,
            )

            self.assertEqual(second_result.returncode, 0, second_result.stderr)
            second_data = json.loads(second_result.stdout)
            self.assertEqual(
                second_data["continued_from_session_id"], first_data["session_id"]
            )
            self.assertNotEqual(second_data["session_id"], first_data["session_id"])

            second_view = load_session_view(repo_root, second_data["session_id"])
            self.assertEqual(second_view.objective, "Audit auth flow")

            prompt_path = (
                repo_root
                / ".agent-relay"
                / "sessions"
                / second_data["session_id"]
                / "turns"
                / "turn-001"
                / "prompt.md"
            )
            prompt_text = prompt_path.read_text(encoding="utf-8")
            self.assertIn("Audit auth flow", prompt_text)
            self.assertIn(
                f"This run continues prior relay session: {first_data['session_id']}",
                prompt_text,
            )
