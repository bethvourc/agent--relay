from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from agent_relay.concurrent import run_concurrent


FAKE_AGENT_SCRIPT = """#!/usr/bin/env python3
from pathlib import Path
import json
import os
import re
import shutil
import sys
import time

if "--version" in sys.argv:
    print(f"{Path(sys.argv[0]).name} fake 1.0")
    raise SystemExit(0)

prompt = sys.argv[-1] if len(sys.argv) > 1 else ""
if "## Planning Phase" in prompt:
    phase = "planning"
elif "## Resolution Phase" in prompt:
    phase = "resolution"
elif "## Review Phase" in prompt:
    phase = "review"
else:
    phase = "implementation"

match = re.search(r"running in slot (\\d+)", prompt)
slot = match.group(1) if match else "0"
plan_path_value = os.environ.get("AGENT_RELAY_FAKE_PLAN")
plan_path = Path(plan_path_value) if plan_path_value else Path(__file__).with_name("fake-plan.json")
plan = json.loads(plan_path.read_text(encoding="utf-8"))
entry = plan.get(phase, {}).get(slot)
if entry is None:
    sys.stdout.write(
        f'RELAY_STATUS: {{"status":"error","reason":"Missing fake plan for {phase} slot {slot}","remaining_work":["fix integration test"],"verification":[]}}\\n'
    )
    raise SystemExit(2)

for relative_path, content in entry.get("writes", {}).items():
    target = Path.cwd() / relative_path
    if content is None:
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        elif target.exists() or target.is_symlink():
            target.unlink()
        continue
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")

stdout = entry.get("stdout", "")
if stdout:
    sys.stdout.write(stdout)
    if not stdout.endswith("\\n"):
        sys.stdout.write("\\n")
    sys.stdout.flush()

time.sleep(float(entry.get("sleep_after", 0.15)))

raise SystemExit(int(entry.get("exit_code", 0)))
"""


class RaceIntegrationTests(TestCase):
    def setUp(self) -> None:
        if shutil.which("tmux") is None:
            self.skipTest("tmux is required for race integration tests")
        # These tests spawn real tmux panes that invoke `claude` / `codex`
        # via PATH. tmux's respawn-pane shell does not always inherit the
        # PYTHONPATH/PATH manipulation cleanly, so when a real agent CLI
        # is installed it shadows the fake stubs and the test fails for
        # environment reasons rather than logic ones. Gate behind an
        # explicit opt-in so default test runs stay green.
        if not os.environ.get("AGENT_RELAY_RUN_INTEGRATION"):
            self.skipTest(
                "race integration tests are gated behind "
                "AGENT_RELAY_RUN_INTEGRATION=1"
            )
        self._initial_tmux_sessions = self._tmux_sessions()

    def tearDown(self) -> None:
        current_sessions = self._tmux_sessions()
        for session_name in sorted(current_sessions - self._initial_tmux_sessions):
            if not session_name.startswith("relay-"):
                continue
            subprocess.run(
                ["tmux", "kill-session", "-t", session_name],
                text=True,
                capture_output=True,
                check=False,
            )

    def _tmux_sessions(self) -> set[str]:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#S"],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            return set()
        return {
            line.strip()
            for line in result.stdout.splitlines()
            if line.strip()
        }

    def _init_repo(self, repo_root: Path) -> None:
        subprocess.run(["git", "init"], cwd=repo_root, text=True, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "tests@example.com"], cwd=repo_root, text=True, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Agent Relay Tests"], cwd=repo_root, text=True, capture_output=True, check=True)
        (repo_root / "README.md").write_text("shared line\n", encoding="utf-8")
        (repo_root / "tests").mkdir()
        (repo_root / "tests" / "placeholder.txt").write_text("baseline\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo_root, text=True, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=repo_root, text=True, capture_output=True, check=True)

    def _write_fake_agents(self, bin_dir: Path) -> None:
        for agent_name in ("claude", "codex"):
            target = bin_dir / agent_name
            target.write_text(FAKE_AGENT_SCRIPT, encoding="utf-8")
            target.chmod(0o755)

    def _write_plan(self, path: Path, payload: dict[str, object]) -> None:
        path.write_text(json.dumps(payload), encoding="utf-8")

    def _cleanup_worktrees(self, repo_root: Path) -> None:
        shutil.rmtree(
            Path(tempfile.gettempdir()) / "agent-relay-worktrees" / repo_root.name,
            ignore_errors=True,
        )

    def test_real_tmux_race_resolves_shared_conflict_end_to_end(self) -> None:
        with TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            self._init_repo(repo_root)
            self.addCleanup(self._cleanup_worktrees, repo_root)
            bin_dir = repo_root / "fake-bin"
            bin_dir.mkdir()
            self._write_fake_agents(bin_dir)
            plan_path = bin_dir / "fake-plan.json"
            self._write_plan(plan_path, {
                "planning": {
                    "0": {
                        "stdout": 'RELAY_STATUS: {"status":"planning","reason":"Shared docs","claims":[{"path":"README.md","role":"shared"}],"remaining_work":["docs"],"verification":[]}',
                    },
                    "1": {
                        "stdout": 'RELAY_STATUS: {"status":"planning","reason":"Shared docs too","claims":[{"path":"README.md","role":"shared"}],"remaining_work":["docs"],"verification":[]}',
                    },
                },
                "implementation": {
                    "0": {
                        "writes": {"README.md": "slot zero line\n"},
                        "stdout": 'RELAY_STATUS: {"status":"done","reason":"First shared edit","remaining_work":[],"verification":["review"]}',
                    },
                    "1": {
                        "writes": {"README.md": "slot one line\n"},
                        "stdout": 'RELAY_STATUS: {"status":"done","reason":"Second shared edit","remaining_work":[],"verification":["review"]}',
                    },
                },
                "resolution": {
                    "0": {
                        "writes": {"README.md": "resolved line\n"},
                        "stdout": 'RELAY_STATUS: {"status":"done","reason":"Resolved conflicted README","remaining_work":[],"verification":["manual review"]}',
                    },
                },
                "review": {
                    "0": {
                        "stdout": 'RELAY_STATUS: {"status":"done","reason":"Resolution looks correct","remaining_work":[],"verification":["manual review"]}',
                    },
                },
            })

            path_value = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
            with patch.dict(os.environ, {
                "PATH": path_value,
            }, clear=False), patch("agent_relay.concurrent._POLL_INTERVAL", 0.05):
                result = run_concurrent(
                    repo_root,
                    agents=["claude", "codex"],
                    task="Resolve the shared README conflict",
                )

            self.assertEqual(result.stop_reason, "all_done")
            self.assertEqual((repo_root / "README.md").read_text(encoding="utf-8"), "resolved line\n")
            self.assertIsNotNone(result.conflict_artifact_path)
            self.assertTrue(Path(result.conflict_artifact_path).exists())

    def test_real_tmux_race_can_continue_from_unresolved_conflict(self) -> None:
        with TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            self._init_repo(repo_root)
            self.addCleanup(self._cleanup_worktrees, repo_root)
            bin_dir = repo_root / "fake-bin"
            bin_dir.mkdir()
            self._write_fake_agents(bin_dir)
            plan_path = bin_dir / "fake-plan.json"
            path_value = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"

            first_plan = {
                "planning": {
                    "0": {
                        "stdout": 'RELAY_STATUS: {"status":"planning","reason":"Shared docs","claims":[{"path":"README.md","role":"shared"}],"remaining_work":["docs"],"verification":[]}',
                    },
                    "1": {
                        "stdout": 'RELAY_STATUS: {"status":"planning","reason":"Shared docs too","claims":[{"path":"README.md","role":"shared"}],"remaining_work":["docs"],"verification":[]}',
                    },
                },
                "implementation": {
                    "0": {
                        "writes": {"README.md": "slot zero line\n"},
                        "stdout": 'RELAY_STATUS: {"status":"done","reason":"First shared edit","remaining_work":[],"verification":["review"]}',
                    },
                    "1": {
                        "writes": {"README.md": "slot one line\n"},
                        "stdout": 'RELAY_STATUS: {"status":"done","reason":"Second shared edit","remaining_work":[],"verification":["review"]}',
                    },
                },
                "resolution": {
                    "0": {
                        "stdout": 'RELAY_STATUS: {"status":"blocked","reason":"Need a human call","remaining_work":["choose final README"],"verification":[]}',
                    },
                },
            }
            self._write_plan(plan_path, first_plan)

            with patch.dict(os.environ, {
                "PATH": path_value,
            }, clear=False), patch("agent_relay.concurrent._POLL_INTERVAL", 0.05):
                first_result = run_concurrent(
                    repo_root,
                    agents=["claude", "codex"],
                    task="Start a shared README review",
                )

            self.assertEqual(first_result.stop_reason, "manual_resolution_required")
            self.assertIsNotNone(first_result.conflict_artifact_path)

            second_plan = {
                "resolution": {
                    "0": {
                        "writes": {"README.md": "resolved from continuation\n"},
                        "stdout": 'RELAY_STATUS: {"status":"done","reason":"Resolved after continuation","remaining_work":[],"verification":["manual review"]}',
                    },
                },
                "review": {
                    "0": {
                        "stdout": 'RELAY_STATUS: {"status":"done","reason":"Reviewed continuation result","remaining_work":[],"verification":["manual review"]}',
                    },
                },
            }
            self._write_plan(plan_path, second_plan)

            with patch.dict(os.environ, {
                "PATH": path_value,
            }, clear=False), patch("agent_relay.concurrent._POLL_INTERVAL", 0.05):
                second_result = run_concurrent(
                    repo_root,
                    agents=["claude", "codex"],
                    task="Resolve the remaining conflict",
                    continue_from_session_id=first_result.session_id,
                )

            self.assertEqual(second_result.stop_reason, "all_done")
            self.assertEqual(second_result.continued_from_session_id, first_result.session_id)
            self.assertEqual((repo_root / "README.md").read_text(encoding="utf-8"), "resolved from continuation\n")
            self.assertEqual([outcome.phase for outcome in second_result.outcomes], ["resolution", "review"])
