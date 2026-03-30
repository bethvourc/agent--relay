from __future__ import annotations

import json
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_relay.storage import load_session_view


class RelayCommandTests(TestCase):
    def run_cli(self, *args: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        env = {"PYTHONPATH": str(ROOT / "src")}
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
        subprocess.run(["git", "init"], cwd=repo_root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.email", "relay@example.com"], cwd=repo_root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.name", "Relay Test"], cwd=repo_root, check=True, capture_output=True, text=True)
        (repo_root / "src").mkdir()
        (repo_root / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo_root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=repo_root, check=True, capture_output=True, text=True)

    def test_one_command_relay_captures_inline_planning_and_proposed_edits(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            self.init_git_repo(repo_root)

            proposed_patch = "\n".join([
                "diff --git a/src/app.py b/src/app.py",
                "--- a/src/app.py",
                "+++ b/src/app.py",
                "@@ -1 +1 @@",
                "-print('hello')",
                "+print('hello from proposal')",
            ])

            result = self.run_cli(
                "--json",
                "codex",
                "--task",
                "Continue after Claude hit its usage limit",
                "--planning-note",
                "Claude planned a two-step migration but stopped before writing any files.",
                "--proposed-edits",
                proposed_patch,
                "--no-launch",
                "--repo",
                tmpdir,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            session_id = data["session_id"]
            session_root = repo_root / ".agent-relay" / "sessions" / session_id
            view = load_session_view(repo_root, session_id)
            checkpoint_root = session_root / "objects" / "checkpoints" / view.latest_checkpoint_id
            handoff_root = session_root / "objects" / "handoffs" / data["handoff_id"]
            packet_text = Path(data["resume_path"]).read_text(encoding="utf-8")

            self.assertTrue((checkpoint_root / "captures" / "planning-snapshot.md").exists())
            self.assertTrue((checkpoint_root / "captures" / "proposed-edits.diff").exists())
            self.assertTrue((checkpoint_root / "captures" / "manifest.json").exists())
            self.assertTrue((handoff_root / "relay" / "inputs" / "planning-snapshot.md").exists())
            self.assertTrue((handoff_root / "relay" / "inputs" / "proposed-edits.diff").exists())
            self.assertIn("## Explicit Handoff Inputs", packet_text)
            self.assertIn("Claude planned a two-step migration", packet_text)
            self.assertIn("These edits were captured outside the working tree", packet_text)
            self.assertIn("relay/inputs/planning-snapshot.md", packet_text)
            self.assertIn("relay/inputs/proposed-edits.diff", packet_text)
            self.assertTrue(any(note.startswith("Planning snapshot captured:") for note in view.research_notes))
            self.assertTrue(any(note.startswith("Captured UI-only proposed edits:") for note in view.implementation_notes))

    def test_one_command_relay_captures_file_backed_inputs_relative_to_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            self.init_git_repo(repo_root)
            notes_dir = repo_root / "handoff-notes"
            notes_dir.mkdir()
            (notes_dir / "planning.md").write_text(
                "Claude inspected the current auth flow and planned the validation sequence.\n",
                encoding="utf-8",
            )
            (notes_dir / "proposed.diff").write_text(
                "\n".join([
                    "diff --git a/src/app.py b/src/app.py",
                    "--- a/src/app.py",
                    "+++ b/src/app.py",
                    "@@ -1 +1 @@",
                    "-print('hello')",
                    "+print('hello from file proposal')",
                ])
                + "\n",
                encoding="utf-8",
            )

            result = self.run_cli(
                "--json",
                "codex",
                "--task",
                "Continue from the file-backed planning snapshot",
                "--planning-note-file",
                "handoff-notes/planning.md",
                "--proposed-edits-file",
                "handoff-notes/proposed.diff",
                "--no-launch",
                "--repo",
                tmpdir,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            packet_text = Path(data["resume_path"]).read_text(encoding="utf-8")

            self.assertIn("Claude inspected the current auth flow", packet_text)
            self.assertIn("hello from file proposal", packet_text)

    def test_one_command_relay_includes_provider_export_when_hook_is_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            self.init_git_repo(repo_root)
            payload = json.dumps(
                {
                    "resumable_state": {
                        "summary": "Claude exported a resumable migration plan.",
                        "current_plan": ["Port the auth guard", "run auth regression tests"],
                        "intended_edits": ["src/auth.py", "tests/test_auth.py"],
                        "next_step": "Port the auth guard",
                    },
                    "planning_snapshot": "Provider export says Claude planned the migration order.",
                    "proposed_edits": "\n".join([
                        "diff --git a/src/app.py b/src/app.py",
                        "--- a/src/app.py",
                        "+++ b/src/app.py",
                        "@@ -1 +1 @@",
                        "-print('hello')",
                        "+print('hello from provider export')",
                    ]),
                    "transcript": "Hidden provider transcript excerpt.",
                    "session_metadata": {"provider_session_id": "claude-123", "mode": "planning"},
                    "warnings": ["Provider export is partial and omits hidden reasoning."],
                }
            )
            command = (
                f"{shlex.quote(sys.executable)} -c "
                f"{shlex.quote('import sys; print(sys.argv[1])')} "
                f"{shlex.quote(payload).replace('{', '{{').replace('}', '}}')}"
            )

            result = self.run_cli(
                "--json",
                "codex",
                "--task",
                "Continue from provider-exported session state",
                "--no-launch",
                "--repo",
                tmpdir,
                extra_env={"AGENT_RELAY_CLAUDE_CAPTURE_TEMPLATE": command},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            session_id = data["session_id"]
            session_root = repo_root / ".agent-relay" / "sessions" / session_id
            view = load_session_view(repo_root, session_id)
            checkpoint_root = session_root / "objects" / "checkpoints" / view.latest_checkpoint_id
            handoff_root = session_root / "objects" / "handoffs" / data["handoff_id"]
            packet_text = Path(data["resume_path"]).read_text(encoding="utf-8")

            self.assertTrue((checkpoint_root / "captures" / "planning-snapshot.md").exists())
            self.assertTrue((checkpoint_root / "captures" / "proposed-edits.diff").exists())
            self.assertTrue((checkpoint_root / "captures" / "provider" / "claude-resumable-state.json").exists())
            self.assertTrue((checkpoint_root / "captures" / "provider" / "claude-transcript.md").exists())
            self.assertTrue((checkpoint_root / "captures" / "provider" / "claude-session-metadata.json").exists())
            self.assertTrue((checkpoint_root / "captures" / "provider" / "claude-warnings.md").exists())
            self.assertTrue((handoff_root / "relay" / "inputs" / "planning-snapshot.md").exists())
            self.assertTrue((handoff_root / "relay" / "inputs" / "proposed-edits.diff").exists())
            self.assertTrue((handoff_root / "relay" / "provider" / "claude-resumable-state.json").exists())
            self.assertTrue((handoff_root / "relay" / "provider" / "claude-transcript.md").exists())
            self.assertTrue((handoff_root / "relay" / "provider" / "claude-session-metadata.json").exists())
            self.assertIn("## Resumable State", packet_text)
            self.assertIn("Claude exported a resumable migration plan.", packet_text)
            self.assertIn("Port the auth guard", packet_text)
            self.assertIn("Provider export says Claude planned the migration order.", packet_text)
            self.assertIn("hello from provider export", packet_text)
            self.assertIn("## Provider Session Export", packet_text)
            self.assertIn("Hidden provider transcript excerpt.", packet_text)
            self.assertIn("claude-123", packet_text)
            self.assertIn("Provider export is partial and omits hidden reasoning.", packet_text)
            self.assertTrue(any("Provider session transcript captured from Claude Code." == note for note in view.research_notes))

    def test_one_command_relay_survives_provider_hook_failure_with_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            self.init_git_repo(repo_root)
            command = (
                f"{shlex.quote(sys.executable)} -c "
                f"{shlex.quote('import sys; print(\"hook failed\", file=sys.stderr); sys.exit(3)')}"
            )

            result = self.run_cli(
                "--json",
                "codex",
                "--task",
                "Continue after failed provider export",
                "--no-launch",
                "--repo",
                tmpdir,
                extra_env={"AGENT_RELAY_CLAUDE_CAPTURE_TEMPLATE": command},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            session_id = data["session_id"]
            view = load_session_view(repo_root, session_id)
            packet_text = Path(data["resume_path"]).read_text(encoding="utf-8")

            self.assertIn("## Provider Session Export", packet_text)
            self.assertIn("capture hook failed", packet_text)
            self.assertTrue(any("Provider export warning (Claude Code):" in note for note in view.research_notes))

    def test_explicit_inputs_override_provider_exported_planning_and_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            self.init_git_repo(repo_root)
            payload = json.dumps(
                {
                    "planning_snapshot": "Provider planning snapshot that should be ignored.",
                    "proposed_edits": "\n".join([
                        "diff --git a/src/app.py b/src/app.py",
                        "--- a/src/app.py",
                        "+++ b/src/app.py",
                        "@@ -1 +1 @@",
                        "-print('hello')",
                        "+print('provider version')",
                    ]),
                }
            )
            command = (
                f"{shlex.quote(sys.executable)} -c "
                f"{shlex.quote('import sys; print(sys.argv[1])')} "
                f"{shlex.quote(payload).replace('{', '{{').replace('}', '}}')}"
            )
            explicit_patch = "\n".join([
                "diff --git a/src/app.py b/src/app.py",
                "--- a/src/app.py",
                "+++ b/src/app.py",
                "@@ -1 +1 @@",
                "-print('hello')",
                "+print('explicit version')",
            ])

            result = self.run_cli(
                "--json",
                "codex",
                "--task",
                "Continue from explicit inputs even if a provider export is available",
                "--planning-note",
                "Explicit planning note wins.",
                "--proposed-edits",
                explicit_patch,
                "--no-launch",
                "--repo",
                tmpdir,
                extra_env={"AGENT_RELAY_CLAUDE_CAPTURE_TEMPLATE": command},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            packet_text = Path(data["resume_path"]).read_text(encoding="utf-8")

            self.assertIn("Explicit planning note wins.", packet_text)
            self.assertIn("explicit version", packet_text)
            self.assertNotIn("Provider planning snapshot that should be ignored.", packet_text)
            self.assertNotIn("provider version", packet_text)
