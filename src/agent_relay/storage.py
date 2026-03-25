from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from agent_relay.models import CheckpointRecord, ModelValidationError, SessionState

STATE_DIRNAME = ".agent-relay"


def default_repo_root(repo: str | None) -> Path:
    return Path(repo or os.getcwd()).resolve()


def session_root(repo_root: Path, session_id: str) -> Path:
    return repo_root / STATE_DIRNAME / "sessions" / session_id


def state_path(repo_root: Path, session_id: str) -> Path:
    return session_root(repo_root, session_id) / "state.json"


def summary_path(repo_root: Path, session_id: str) -> Path:
    return session_root(repo_root, session_id) / "summary.md"


def checkpoints_dir(repo_root: Path, session_id: str) -> Path:
    return session_root(repo_root, session_id) / "checkpoints"


def checkpoint_path(repo_root: Path, session_id: str, checkpoint_id: str) -> Path:
    return checkpoints_dir(repo_root, session_id) / f"{checkpoint_id}.json"


def resume_dir(repo_root: Path, session_id: str) -> Path:
    return session_root(repo_root, session_id) / "resume"


def artifacts_dir(repo_root: Path, session_id: str) -> Path:
    return session_root(repo_root, session_id) / "artifacts"


def ensure_session_layout(repo_root: Path, session_id: str) -> Path:
    root = session_root(repo_root, session_id)
    checkpoints_dir(repo_root, session_id).mkdir(parents=True, exist_ok=True)
    resume_dir(repo_root, session_id).mkdir(parents=True, exist_ok=True)
    artifacts_dir(repo_root, session_id).mkdir(parents=True, exist_ok=True)
    return root


def write_text_atomic(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        delete=False,
    ) as handle:
        handle.write(content)
        temp_path = Path(handle.name)
    temp_path.replace(path)
    return path


def write_json_atomic(path: Path, data: dict[str, object]) -> Path:
    return write_text_atomic(path, json.dumps(data, indent=2) + "\n")


def load_session(repo_root: Path, session_id: str) -> SessionState:
    path = state_path(repo_root, session_id)
    if not path.exists():
        raise SystemExit(f"Session not found: {session_id}")
    return SessionState.from_dict(json.loads(path.read_text()))


def save_session(repo_root: Path, session: SessionState) -> Path:
    ensure_session_layout(repo_root, session.session_id)
    return write_json_atomic(state_path(repo_root, session.session_id), session.to_dict())


def save_checkpoint(repo_root: Path, checkpoint: CheckpointRecord) -> Path:
    ensure_session_layout(repo_root, checkpoint.session_id)
    return write_json_atomic(
        checkpoint_path(repo_root, checkpoint.session_id, checkpoint.checkpoint_id),
        checkpoint.to_dict(),
    )


def sessions_root(repo_root: Path) -> Path:
    return repo_root / STATE_DIRNAME / "sessions"


def list_sessions(repo_root: Path) -> list[SessionState]:
    root = sessions_root(repo_root)
    if not root.exists():
        return []
    sessions: list[SessionState] = []
    for state_file in sorted(root.glob("*/state.json")):
        try:
            session = SessionState.from_dict(json.loads(state_file.read_text()))
            sessions.append(session)
        except (json.JSONDecodeError, ModelValidationError):
            continue
    sessions.sort(key=lambda s: s.updated_at, reverse=True)
    return sessions


def load_checkpoint(repo_root: Path, session_id: str, checkpoint_id: str) -> CheckpointRecord:
    path = checkpoint_path(repo_root, session_id, checkpoint_id)
    if not path.exists():
        raise SystemExit(f"Checkpoint not found: {checkpoint_id}")
    return CheckpointRecord.from_dict(json.loads(path.read_text()))
