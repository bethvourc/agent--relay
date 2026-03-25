from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from agent_relay.models import ModelValidationError
from agent_relay.storage import load_session, sessions_root
from agent_relay.v2.errors import V2Error
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
    if is_v2_session(repo_root, session_id):
        try:
            return load_session_view(repo_root, session_id).to_dict()
        except V2Error as exc:
            raise SystemExit(f"Session {session_id} is corrupt: {exc}") from exc
    try:
        return load_session(repo_root, session_id).to_dict()
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Session {session_id} is corrupt: {exc}") from exc
    except ModelValidationError as exc:
        raise SystemExit(f"Session {session_id} is corrupt: {exc}") from exc


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
            try:
                view = load_session_view(repo_root, session_id)
            except V2Error as exc:
                entries.append(
                    DashboardEntry(
                        session_id=session_id,
                        current_agent="?",
                        current_status="corrupt",
                        objective=str(exc),
                        updated_at="",
                        storage_model="journal_v2",
                        health="corrupt",
                        error=str(exc),
                    )
                )
                continue
            entries.append(
                DashboardEntry(
                    session_id=view.session_id,
                    current_agent=view.current_agent,
                    current_status=view.current_status,
                    objective=view.objective,
                    updated_at=view.updated_at,
                    storage_model=view.storage_model,
                    health=view.health,
                )
            )
            continue

        state_path = session_dir / "state.json"
        if not state_path.exists():
            entries.append(
                DashboardEntry(
                    session_id=session_id,
                    current_agent="?",
                    current_status="corrupt",
                    objective="Session directory is missing state.json or session.json",
                    updated_at="",
                    storage_model="unknown",
                    health="corrupt",
                    error="Session directory is missing state.json or session.json",
                )
            )
            continue

        try:
            session = load_session(repo_root, session_id)
        except json.JSONDecodeError as exc:
            entries.append(
                DashboardEntry(
                    session_id=session_id,
                    current_agent="?",
                    current_status="corrupt",
                    objective=f"Corrupt v1 session: {exc}",
                    updated_at="",
                    storage_model="state_v1",
                    health="corrupt",
                    error=str(exc),
                )
            )
            continue
        except ModelValidationError as exc:
            entries.append(
                DashboardEntry(
                    session_id=session_id,
                    current_agent="?",
                    current_status="corrupt",
                    objective=f"Corrupt v1 session: {exc}",
                    updated_at="",
                    storage_model="state_v1",
                    health="corrupt",
                    error=str(exc),
                )
            )
            continue

        entries.append(
            DashboardEntry(
                session_id=session.session_id,
                current_agent=session.current_agent,
                current_status=session.current_status,
                objective=session.objective,
                updated_at=session.updated_at,
                storage_model="state_v1",
                health="healthy",
            )
        )

    return [entry.to_dict() for entry in sorted(entries, key=_dashboard_sort_key, reverse=True)]


def _dashboard_sort_key(entry: DashboardEntry) -> tuple[str, str]:
    return (entry.updated_at, entry.session_id)
