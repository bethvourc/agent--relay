from __future__ import annotations

from pathlib import Path

from agent_relay.models import CheckpointRecord, SessionState
from agent_relay.storage import summary_path, write_text_atomic


def render_summary(session: SessionState, checkpoint: CheckpointRecord) -> str:
    lines = [
        "# Agent Relay Summary",
        "",
        f"Objective: {session.objective}",
        f"Current agent: {session.current_agent}",
        f"Current status: {session.current_status}",
        f"Next action: {session.next_action or 'Not recorded'}",
        f"Validation: {session.validation.status} - {session.validation.summary or 'None recorded'}",
        f"Latest checkpoint: {checkpoint.checkpoint_id}",
        "",
    ]
    _append_bullets(lines, "Recent decisions:", session.decisions)
    _append_bullets(lines, "Blockers:", session.blockers)
    _append_bullets(lines, "Research notes:", session.research_notes[-3:])
    _append_bullets(lines, "Implementation notes:", session.implementation_notes[-3:])
    _append_bullets(lines, "Touched files:", session.touched_files)
    return "\n".join(lines) + "\n"


def write_summary(repo_root: Path, session: SessionState, checkpoint: CheckpointRecord) -> Path:
    return write_text_atomic(
        summary_path(repo_root, session.session_id),
        render_summary(session, checkpoint),
    )


def _append_bullets(lines: list[str], heading: str, items: list[str]) -> None:
    lines.append(heading)
    if items:
        lines.extend([f"- {item}" for item in items])
    else:
        lines.append("- None recorded")
    lines.append("")
