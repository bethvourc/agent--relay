from __future__ import annotations

from pathlib import Path

STATE_DIRNAME = ".agent-relay"
LAYOUT_VERSION = "2"
OBJECT_DIRNAMES = {
    "checkpoint": "checkpoints",
    "handoff": "handoffs",
    "launch": "launches",
}


def relay_root(repo_root: Path) -> Path:
    return repo_root / STATE_DIRNAME


def version_path(repo_root: Path) -> Path:
    return relay_root(repo_root) / "VERSION"


def locks_dir(repo_root: Path) -> Path:
    return relay_root(repo_root) / "locks"


def repo_lock_path(repo_root: Path) -> Path:
    return locks_dir(repo_root) / "repo.lock"


def sessions_root(repo_root: Path) -> Path:
    return relay_root(repo_root) / "sessions"


def session_root(repo_root: Path, session_id: str) -> Path:
    return sessions_root(repo_root) / session_id


def session_manifest_path(repo_root: Path, session_id: str) -> Path:
    return session_root(repo_root, session_id) / "session.json"


def session_lock_dir(repo_root: Path, session_id: str) -> Path:
    return session_root(repo_root, session_id) / "lock"


def session_lock_path(repo_root: Path, session_id: str) -> Path:
    return session_lock_dir(repo_root, session_id) / "session.lock"


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


def object_dirname(object_kind: str) -> str:
    try:
        return OBJECT_DIRNAMES[object_kind]
    except KeyError as exc:
        raise ValueError(f"Unknown object kind: {object_kind}") from exc


def object_dir(repo_root: Path, session_id: str, object_kind: str, object_id: str) -> Path:
    return objects_dir(repo_root, session_id) / object_dirname(object_kind) / object_id


def object_manifest_path(
    repo_root: Path, session_id: str, object_kind: str, object_id: str
) -> Path:
    return object_dir(repo_root, session_id, object_kind, object_id) / "manifest.json"


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


def pending_tx_root(repo_root: Path, session_id: str, tx_id: str) -> Path:
    return pending_tx_dir(repo_root, session_id) / tx_id


def pending_tx_manifest_path(repo_root: Path, session_id: str, tx_id: str) -> Path:
    return pending_tx_root(repo_root, session_id, tx_id) / "transaction.json"


def pending_tx_staging_dir(repo_root: Path, session_id: str, tx_id: str) -> Path:
    return pending_tx_root(repo_root, session_id, tx_id) / "staging"


def quarantine_dir(repo_root: Path, session_id: str) -> Path:
    return recovery_dir(repo_root, session_id) / "quarantine"


def repair_reports_dir(repo_root: Path, session_id: str) -> Path:
    return recovery_dir(repo_root, session_id) / "repair-reports"


def turns_dir(repo_root: Path, session_id: str) -> Path:
    return session_root(repo_root, session_id) / "turns"


def turn_dir(repo_root: Path, session_id: str, turn_number: int) -> Path:
    return turns_dir(repo_root, session_id) / f"turn-{turn_number:03d}"


def workspace_log_path(repo_root: Path, session_id: str) -> Path:
    return session_root(repo_root, session_id) / "workspace-log.md"


def concurrent_dir(repo_root: Path, session_id: str) -> Path:
    return session_root(repo_root, session_id) / "concurrent"


def concurrent_agent_dir(repo_root: Path, session_id: str, slot_index: int) -> Path:
    return concurrent_dir(repo_root, session_id) / f"agent-{slot_index:02d}"


def is_session_dir(path: Path) -> bool:
    return (path / "session.json").exists()
