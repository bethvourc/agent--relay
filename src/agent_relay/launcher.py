from __future__ import annotations

import subprocess
from pathlib import Path

from agent_relay.agents import get_agent_adapter
from agent_relay.checkpoints import utc_now
from agent_relay.models import HandoffRecord, SessionState
from agent_relay.storage import load_checkpoint, save_session
from agent_relay.summary import write_summary


def build_handoff_record(
    session: SessionState,
    *,
    repo_root: Path,
    to_agent: str,
    reason: str,
    prepared_at: str,
    resume_path: Path,
) -> HandoffRecord:
    if not session.latest_checkpoint_id:
        raise SystemExit("Session has no checkpoint to hand off")

    adapter = get_agent_adapter(to_agent)
    launch_spec = adapter.render_launch_spec(repo_root, resume_path)
    return HandoffRecord(
        from_agent=session.current_agent,
        to_agent=to_agent,
        reason=reason,
        prepared_at=prepared_at,
        checkpoint_id=session.latest_checkpoint_id,
        resume_packet_path=str(resume_path),
        launch_status="ready",
        launch_profile=adapter.display_name,
        launch_cwd=launch_spec.cwd,
        launch_command=launch_spec.command,
        launch_template=launch_spec.template,
        launch_template_source=launch_spec.template_source,
        launch_instructions=launch_spec.instructions,
    )


def latest_handoff(session: SessionState) -> HandoffRecord:
    if not session.handoffs:
        raise SystemExit("No handoff has been prepared for this session")
    return session.handoffs[-1]


def launch_preview_lines(handoff: HandoffRecord) -> list[str]:
    return [
        f"Launch target: {handoff.to_agent}",
        handoff.resume_packet_path,
        handoff.launch_command,
        handoff.launch_instructions,
    ]


def launch_handoff(repo_root: Path, session: SessionState, handoff: HandoffRecord) -> int:
    launched_at = utc_now()
    handoff.launch_status = "launching"
    handoff.launched_at = launched_at
    session.current_status = "launching"
    session.updated_at = launched_at
    save_session(repo_root, session)
    _write_latest_summary(repo_root, session)

    completed = subprocess.run(
        handoff.launch_command,
        cwd=handoff.launch_cwd or str(repo_root),
        shell=True,
        check=False,
    )

    finished_at = utc_now()
    handoff.finished_at = finished_at
    handoff.exit_code = completed.returncode
    if completed.returncode == 0:
        handoff.launch_status = "succeeded"
        session.current_agent = handoff.to_agent
        session.current_status = "active"
    else:
        handoff.launch_status = "failed"
        session.current_status = "launch_failed"
    session.updated_at = finished_at
    save_session(repo_root, session)
    _write_latest_summary(repo_root, session)
    return completed.returncode


def _write_latest_summary(repo_root: Path, session: SessionState) -> None:
    if not session.latest_checkpoint_id:
        return
    checkpoint = load_checkpoint(repo_root, session.session_id, session.latest_checkpoint_id)
    write_summary(repo_root, session, checkpoint)
