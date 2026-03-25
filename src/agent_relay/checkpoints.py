from __future__ import annotations

import secrets
from datetime import UTC, datetime
from pathlib import Path

from agent_relay.models import CheckpointRecord, SessionState
from agent_relay.storage import save_checkpoint


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def checkpoint_id_now() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


def build_checkpoint(
    session: SessionState,
    *,
    checkpoint_id: str | None = None,
    created_at: str | None = None,
    artifacts: dict[str, str | list[str]] | None = None,
) -> CheckpointRecord:
    return CheckpointRecord(
        checkpoint_id=checkpoint_id or checkpoint_id_now(),
        session_id=session.session_id,
        created_at=created_at or utc_now(),
        status=session.current_status,
        next_action=session.next_action,
        decisions=list(session.decisions),
        blockers=list(session.blockers),
        research_notes=list(session.research_notes),
        implementation_notes=list(session.implementation_notes),
        touched_files=list(session.touched_files),
        validation=session.validation,
        artifacts=artifacts or {},
    )


def create_checkpoint(
    repo_root: Path,
    session: SessionState,
    *,
    checkpoint_id: str | None = None,
    created_at: str | None = None,
    artifacts: dict[str, str | list[str]] | None = None,
) -> CheckpointRecord:
    checkpoint = build_checkpoint(
        session,
        checkpoint_id=checkpoint_id,
        created_at=created_at,
        artifacts=artifacts,
    )
    save_checkpoint(repo_root, checkpoint)
    session.latest_checkpoint_id = checkpoint.checkpoint_id
    return checkpoint
