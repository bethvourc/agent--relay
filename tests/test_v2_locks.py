from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_relay.v2.locks import acquire_repo_lock, acquire_session_lock


class V2LockTests(TestCase):
    def run_child(self, code: str) -> subprocess.CompletedProcess[str]:
        env = {"PYTHONPATH": str(ROOT / "src")}
        return subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
        )

    def test_repo_lock_times_out_for_second_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            with acquire_repo_lock(repo_root, owner="parent", timeout_seconds=1.0):
                code = f"""
from pathlib import Path
from agent_relay.v2.errors import LockTimeoutError
from agent_relay.v2.locks import acquire_repo_lock

repo = Path({str(repo_root)!r})
try:
    lock = acquire_repo_lock(repo, owner="child", timeout_seconds=0.2, poll_interval_seconds=0.05)
except LockTimeoutError:
    raise SystemExit(7)
else:
    lock.release()
    raise SystemExit(0)
"""
                result = self.run_child(code)

            self.assertEqual(result.returncode, 7, result.stdout + result.stderr)

    def test_session_locks_are_independent_per_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            with acquire_session_lock(repo_root, "session-a", owner="one", timeout_seconds=1.0):
                with acquire_session_lock(repo_root, "session-b", owner="two", timeout_seconds=1.0):
                    pass

    def test_repo_lock_blocks_new_session_locks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            with acquire_repo_lock(repo_root, owner="maintenance", timeout_seconds=1.0):
                code = f"""
from pathlib import Path
from agent_relay.v2.errors import LockTimeoutError
from agent_relay.v2.locks import acquire_session_lock

repo = Path({str(repo_root)!r})
try:
    lock = acquire_session_lock(repo, "session-a", owner="child", timeout_seconds=0.2, poll_interval_seconds=0.05)
except LockTimeoutError:
    raise SystemExit(9)
else:
    lock.release()
    raise SystemExit(0)
"""
                result = self.run_child(code)

            self.assertEqual(result.returncode, 9, result.stdout + result.stderr)

    def test_active_session_lock_blocks_repo_maintenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            with acquire_session_lock(repo_root, "session-a", owner="worker", timeout_seconds=1.0):
                code = f"""
from pathlib import Path
from agent_relay.v2.errors import LockTimeoutError
from agent_relay.v2.locks import acquire_repo_lock

repo = Path({str(repo_root)!r})
try:
    lock = acquire_repo_lock(repo, owner="maintenance", timeout_seconds=0.2, poll_interval_seconds=0.05)
except LockTimeoutError:
    raise SystemExit(11)
else:
    lock.release()
    raise SystemExit(0)
"""
                result = self.run_child(code)

            self.assertEqual(result.returncode, 11, result.stdout + result.stderr)
