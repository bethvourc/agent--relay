from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent_relay.v2.handoffs import recover_interrupted_launches
from agent_relay.v2.integrity import inspect_session_integrity
from agent_relay.v2.layout import session_root, sessions_root
from agent_relay.v2.storage import is_v2_session, load_session_view


@dataclass(frozen=True, slots=True)
class DashboardEntry:
    session_id: str
    current_agent: str
    current_status: str
    objective: str
    updated_at: str
    storage_model: str
    health: str
    error: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "session_id": self.session_id,
            "current_agent": self.current_agent,
            "current_status": self.current_status,
            "objective": self.objective,
            "updated_at": self.updated_at,
            "storage_model": self.storage_model,
            "health": self.health,
            "error": self.error,
        }


def load_session_for_inspect(repo_root: Path, session_id: str) -> dict:
    if not is_v2_session(repo_root, session_id):
        candidate = session_root(repo_root, session_id)
        if candidate.exists():
            raise SystemExit(f"corrupt or unsupported session directory: {session_id}")
        raise SystemExit(f"Session not found: {session_id}")

    report = inspect_session_integrity(repo_root, session_id).report
    if report.health == "healthy":
        load_session_view(repo_root, session_id)
        if report.current_status == "launching":
            recover_interrupted_launches(repo_root, session_id, owner="cli:inspect")
        report = inspect_session_integrity(repo_root, session_id).report
    return report.to_dict()


def list_sessions_for_dashboard(repo_root: Path) -> list[dict]:
    root = sessions_root(repo_root)
    if not root.exists():
        return []

    entries: list[DashboardEntry] = []
    for session_dir in sorted(root.iterdir()):
        if not session_dir.is_dir():
            continue
        session_id = session_dir.name
        if is_v2_session(repo_root, session_id):
            report = inspect_session_integrity(repo_root, session_id).report
            if report.health == "healthy":
                load_session_view(repo_root, session_id)
                if report.current_status == "launching":
                    recover_interrupted_launches(repo_root, session_id, owner="cli:dashboard")
                report = inspect_session_integrity(repo_root, session_id).report
            entries.append(
                DashboardEntry(
                    session_id=report.session_id,
                    current_agent=report.current_agent,
                    current_status=report.current_status,
                    objective=report.objective,
                    updated_at=report.updated_at,
                    storage_model=report.storage_model,
                    health=report.health,
                    error=report.error,
                )
            )
            continue

        entries.append(
            DashboardEntry(
                session_id=session_id,
                current_agent="?",
                current_status="corrupt",
                objective="corrupt or unsupported session directory",
                updated_at="",
                storage_model="unknown",
                health="corrupt",
                error="corrupt or unsupported session directory",
            )
        )

    return [entry.to_dict() for entry in sorted(entries, key=_dashboard_sort_key, reverse=True)]


def _dashboard_sort_key(entry: DashboardEntry) -> tuple[str, str]:
    return (entry.updated_at, entry.session_id)
