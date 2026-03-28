from __future__ import annotations

import shlex
import sys
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_relay.agents import AGENT_NAMES, get_agent_adapter, get_agent_display_name  # noqa: E402


class AgentAdapterTests(TestCase):
    def test_agent_registry_exposes_supported_adapters(self) -> None:
        self.assertEqual(set(AGENT_NAMES), {"claude", "codex"})
        self.assertEqual(get_agent_display_name("claude"), "Claude Code")
        self.assertEqual(get_agent_display_name("codex"), "Codex")

    def test_adapter_renders_default_launch_spec(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            resume_path = repo_root / ".agent-relay" / "sessions" / "s1" / "resume" / "codex.md"
            adapter = get_agent_adapter("codex")

            launch_spec = adapter.render_launch_spec(repo_root, resume_path)

            self.assertEqual(
                launch_spec.command,
                f'cd {shlex.quote(str(repo_root))} && codex "$(cat {shlex.quote(str(resume_path))})"',
            )
            self.assertEqual(launch_spec.template_source, "default")
            self.assertEqual(launch_spec.cwd, str(repo_root))
            self.assertTrue(launch_spec.packet_aware)
            self.assertEqual(launch_spec.execute_policy, "allow")
            self.assertIn("resume packet as its prompt", launch_spec.instructions)

    def test_adapter_renders_env_override_launch_spec(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            resume_path = repo_root / ".agent-relay" / "sessions" / "s1" / "resume" / "claude.md"
            adapter = get_agent_adapter("claude")

            with patch.dict(
                "os.environ",
                {"AGENT_RELAY_CLAUDE_LAUNCH_TEMPLATE": "cd {repo_root} && {agent_cli} --resume {resume_path}"},
                clear=False,
            ):
                launch_spec = adapter.render_launch_spec(repo_root, resume_path)

            self.assertEqual(
                launch_spec.command,
                f"cd {shlex.quote(str(repo_root))} && claude --resume {shlex.quote(str(resume_path))}",
            )
            self.assertEqual(launch_spec.template_source, "env")
            self.assertTrue(launch_spec.packet_aware)
            self.assertEqual(launch_spec.execute_policy, "allow")
            self.assertEqual(adapter.resume_packet_target, "claude")
            self.assertIsNone(adapter.event_capture_hook_name)

    def test_adapter_marks_unsafe_env_override_as_preview_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            resume_path = repo_root / ".agent-relay" / "sessions" / "s1" / "resume" / "codex.md"
            adapter = get_agent_adapter("codex")

            with patch.dict(
                "os.environ",
                {"AGENT_RELAY_CODEX_LAUNCH_TEMPLATE": "cd {repo_root} && {agent_cli}"},
                clear=False,
            ):
                launch_spec = adapter.render_launch_spec(repo_root, resume_path)

        self.assertFalse(launch_spec.packet_aware)
        self.assertEqual(launch_spec.execute_policy, "refuse")
        self.assertIn("does not pass the resume packet", launch_spec.warning or "")
