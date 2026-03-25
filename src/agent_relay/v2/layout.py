from __future__ import annotations

from pathlib import Path


STATE_DIRNAME = ".agent-relay"
LAYOUT_VERSION = "2"


def relay_root(repo_root: Path) -> Path:
    return repo_root / STATE_DIRNAME


def version_path(repo_root: Path) -> Path:
    return relay_root(repo_root) / "VERSION"


def sessions_root(repo_root: Path) -> Path:
    return relay_root(repo_root) / "sessions"


def session_root(repo_root: Path, session_id: str) -> Path:
    return sessions_root(repo_root) / session_id


def session_manifest_path(repo_root: Path, session_id: str) -> Path:
    return session_root(repo_root, session_id) / "session.json"


def journal_dir(repo_root: Path, session_id: str) -> Path:
    return session_root(repo_root, session_id) / "journal"


def objects_dir(repo_root: Path, session_id: str) -> Path:
    return session_root(repo_root, session_id) / "objects"


def checkpoints_dir(repo_root: Path, session_id: str) -> Path:
    return objects_dir(repo_root, session_id) / "checkpoints"


def handoffs_dir(repo_root: Path, session_id: str) -> Path:
    return objects_dir(repo_root, session_id) / "handoffs"


def launches_dir(repo_root: Path, session_id: str) -> Path:
    return objects_dir(repo_root, session_id) / "launches"


def refs_dir(repo_root: Path, session_id: str) -> Path:
    return session_root(repo_root, session_id) / "refs"


def head_ref_path(repo_root: Path, session_id: str) -> Path:
    return refs_dir(repo_root, session_id) / "head.json"


def derived_dir(repo_root: Path, session_id: str) -> Path:
    return session_root(repo_root, session_id) / "derived"


def derived_view_path(repo_root: Path, session_id: str) -> Path:
    return derived_dir(repo_root, session_id) / "view.json"


def recovery_dir(repo_root: Path, session_id: str) -> Path:
    return session_root(repo_root, session_id) / "recovery"


def pending_tx_dir(repo_root: Path, session_id: str) -> Path:
    return recovery_dir(repo_root, session_id) / "pending-tx"


def quarantine_dir(repo_root: Path, session_id: str) -> Path:
    return recovery_dir(repo_root, session_id) / "quarantine"


def is_v2_session_dir(path: Path) -> bool:
    return (path / "session.json").exists()
