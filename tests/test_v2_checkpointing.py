from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_relay.v2.capture_support import CaptureOptions
from agent_relay.v2.checkpoints import create_checkpoint_for_command
from agent_relay.v2.errors import V2CorruptionError
from agent_relay.v2.layout import pending_tx_dir
from agent_relay.v2.models import CheckpointManifest
from agent_relay.v2.storage import load_session_view
from agent_relay.v2.tx import recover_session_transactions
from tests.v2_fixtures import build_sample_v2_session


class V2CheckpointingTests(TestCase):
    def init_git_repo(self, repo_root: Path) -> None:
        subprocess.run(["git", "init"], cwd=repo_root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.email", "relay@example.com"], cwd=repo_root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.name", "Relay Test"], cwd=repo_root, check=True, capture_output=True, text=True)

    def commit_file(self, repo_root: Path, relative_path: str, content: str, message: str) -> None:
        path = repo_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        subprocess.run(["git", "add", relative_path], cwd=repo_root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "commit", "-m", message], cwd=repo_root, check=True, capture_output=True, text=True)

    def test_git_checkpoint_creates_immutable_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            self.init_git_repo(repo_root)
            self.commit_file(repo_root, "src/app.py", "print('hello')\n", "initial commit")
            fixture = build_sample_v2_session(repo_root)

            result = create_checkpoint_for_command(
                repo_root,
                fixture["session_id"],
                command_name="checkpoint",
                options=CaptureOptions(next_action="Capture the immutable checkpoint"),
                owner="test:checkpoint",
            )

            checkpoint_dir = (
                repo_root / ".agent-relay" / "sessions" / fixture["session_id"] / "objects" / "checkpoints" / result.checkpoint_id
            )
            manifest = CheckpointManifest.from_dict(
                json.loads((checkpoint_dir / "manifest.json").read_text(encoding="utf-8"))
            )
            view = load_session_view(repo_root, fixture["session_id"])

            self.assertEqual(result.capture_mode, "git")
            self.assertEqual(view.latest_checkpoint_id, result.checkpoint_id)
            self.assertEqual(manifest.capture_mode, "git")
            self.assertTrue((checkpoint_dir / "repo-state.json").exists())
            self.assertTrue((checkpoint_dir / "validation.json").exists())
            self.assertTrue((checkpoint_dir / "summary.md").exists())
            self.assertTrue((checkpoint_dir / "git-head.txt").exists())
            self.assertTrue((checkpoint_dir / "workspace.patch").exists())
            self.assertTrue((checkpoint_dir / "untracked-manifest.json").exists())

    def test_git_checkpoint_captures_patch_and_untracked_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            self.init_git_repo(repo_root)
            self.commit_file(repo_root, "src/app.py", "print('hello')\n", "initial commit")
            fixture = build_sample_v2_session(repo_root)

            (repo_root / "src" / "app.py").write_text("print('updated')\n", encoding="utf-8")
            (repo_root / "notes.txt").write_text("capture me\n", encoding="utf-8")

            result = create_checkpoint_for_command(
                repo_root,
                fixture["session_id"],
                command_name="checkpoint",
                options=CaptureOptions(
                    next_action="Capture dirty repo state",
                    capture_git_changes=True,
                ),
                owner="test:dirty-checkpoint",
            )

            checkpoint_dir = (
                repo_root / ".agent-relay" / "sessions" / fixture["session_id"] / "objects" / "checkpoints" / result.checkpoint_id
            )
            patch_text = (checkpoint_dir / "workspace.patch").read_text(encoding="utf-8")
            untracked_manifest = json.loads((checkpoint_dir / "untracked-manifest.json").read_text(encoding="utf-8"))
            copied_untracked = checkpoint_dir / "untracked" / "notes.txt"

            self.assertIn("src/app.py", patch_text)
            self.assertEqual(untracked_manifest["files"][0]["path"], "notes.txt")
            self.assertEqual(untracked_manifest["files"][0]["stored_as"], "untracked/notes.txt")
            self.assertEqual(copied_untracked.read_text(encoding="utf-8"), "capture me\n")

    def test_non_git_checkpoint_requires_explicit_snapshot_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            fixture = build_sample_v2_session(repo_root)

            with self.assertRaises(SystemExit) as context:
                create_checkpoint_for_command(
                    repo_root,
                    fixture["session_id"],
                    command_name="checkpoint",
                    options=CaptureOptions(next_action="Should fail"),
                    owner="test:non-git",
                )

            self.assertIn("Git-backed repo or --snapshot-mode full", str(context.exception))

    def test_snapshot_mode_captures_full_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            (repo_root / "README.md").write_text("snapshot me\n", encoding="utf-8")
            (repo_root / "src").mkdir()
            (repo_root / "src" / "tool.py").write_text("print('snapshot')\n", encoding="utf-8")
            fixture = build_sample_v2_session(repo_root)

            result = create_checkpoint_for_command(
                repo_root,
                fixture["session_id"],
                command_name="checkpoint",
                options=CaptureOptions(next_action="Capture full snapshot", snapshot_mode="full"),
                owner="test:snapshot",
            )

            checkpoint_dir = (
                repo_root / ".agent-relay" / "sessions" / fixture["session_id"] / "objects" / "checkpoints" / result.checkpoint_id
            )
            manifest = CheckpointManifest.from_dict(
                json.loads((checkpoint_dir / "manifest.json").read_text(encoding="utf-8"))
            )
            snapshot_manifest = json.loads((checkpoint_dir / "snapshot-manifest.json").read_text(encoding="utf-8"))

            self.assertEqual(result.capture_mode, "snapshot")
            self.assertEqual(manifest.capture_mode, "snapshot")
            self.assertTrue((checkpoint_dir / "snapshot" / "README.md").exists())
            self.assertTrue((checkpoint_dir / "snapshot" / "src" / "tool.py").exists())
            self.assertEqual(
                sorted(entry["path"] for entry in snapshot_manifest["files"]),
                ["README.md", "src/tool.py"],
            )

    def test_corrupted_checkpoint_artifact_hash_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            self.init_git_repo(repo_root)
            self.commit_file(repo_root, "src/app.py", "print('hello')\n", "initial commit")
            fixture = build_sample_v2_session(repo_root)

            result = create_checkpoint_for_command(
                repo_root,
                fixture["session_id"],
                command_name="checkpoint",
                options=CaptureOptions(next_action="Create a checkpoint that will be corrupted"),
                owner="test:corrupt-checkpoint",
            )

            checkpoint_dir = (
                repo_root / ".agent-relay" / "sessions" / fixture["session_id"] / "objects" / "checkpoints" / result.checkpoint_id
            )
            (checkpoint_dir / "summary.md").write_text("# tampered\n", encoding="utf-8")

            with self.assertRaises(V2CorruptionError) as context:
                load_session_view(repo_root, fixture["session_id"])

            self.assertIn("object file hash mismatch", str(context.exception))

    def test_interrupted_checkpoint_is_not_visible_and_recovery_quarantines_residue(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            self.init_git_repo(repo_root)
            self.commit_file(repo_root, "src/app.py", "print('hello')\n", "initial commit")
            fixture = build_sample_v2_session(repo_root)
            checkpoint_id = "cp-20260325T181500Z-888888"
            interrupted_path: Path | None = None

            import agent_relay.v2.tx as tx_module

            original_write_json_atomic = tx_module.write_json_atomic

            def interrupt_on_journal_write(path: Path, payload) -> None:
                nonlocal interrupted_path
                if path.parent.name == "journal" and isinstance(payload, dict) and payload.get("kind") == "journal_event":
                    interrupted_path = path
                    raise KeyboardInterrupt("simulated interruption before checkpoint journal commit")
                original_write_json_atomic(path, payload)

            with patch("agent_relay.v2.checkpoints.checkpoint_id_now", return_value=checkpoint_id):
                with patch("agent_relay.v2.tx.write_json_atomic", side_effect=interrupt_on_journal_write):
                    with self.assertRaises(KeyboardInterrupt):
                        create_checkpoint_for_command(
                            repo_root,
                            fixture["session_id"],
                            command_name="checkpoint",
                            options=CaptureOptions(next_action="Interrupt after promoting the checkpoint"),
                            owner="test:interrupt-checkpoint",
                        )

            view = load_session_view(repo_root, fixture["session_id"])
            checkpoint_dir = (
                repo_root / ".agent-relay" / "sessions" / fixture["session_id"] / "objects" / "checkpoints" / checkpoint_id
            )
            quarantine_dir = (
                repo_root / ".agent-relay" / "sessions" / fixture["session_id"] / "recovery" / "quarantine"
            )

            self.assertIsNotNone(interrupted_path)
            self.assertEqual(view.latest_checkpoint_id, fixture["checkpoint_two_id"])
            self.assertFalse(interrupted_path.exists())
            self.assertTrue(checkpoint_dir.exists())
            self.assertTrue(any(path.is_dir() for path in pending_tx_dir(repo_root, fixture["session_id"]).iterdir()))

            report = recover_session_transactions(repo_root, fixture["session_id"])
            view = load_session_view(repo_root, fixture["session_id"])

            self.assertEqual(report.quarantined_transactions, 1)
            self.assertEqual(view.latest_checkpoint_id, fixture["checkpoint_two_id"])
            self.assertFalse(checkpoint_dir.exists())
            self.assertTrue(any(path.name.endswith("-abandoned") for path in quarantine_dir.iterdir()))
