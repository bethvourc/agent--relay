from __future__ import annotations

import argparse
import json
import secrets
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from agent_relay.agents import (
    AGENT_NAMES,
    get_agent_profile,
    render_launch_command,
    render_launch_instructions,
)
from agent_relay.checkpoints import create_checkpoint, utc_now
from agent_relay.models import (
    SCHEMA_VERSION,
    SESSION_STATUSES,
    HandoffRecord,
    SessionState,
    ValidationState,
)
from agent_relay.storage import (
    default_repo_root,
    load_checkpoint,
    load_session,
    resume_dir,
    save_session,
    write_text_atomic,
)
from agent_relay.summary import write_summary


def render_resume_packet(
    session: SessionState,
    target_agent: str,
    *,
    handoff_reason: str,
    prepared_at: str,
) -> str:
    if target_agent == "claude":
        return render_claude_resume_packet(session, handoff_reason=handoff_reason, prepared_at=prepared_at)
    if target_agent == "codex":
        return render_codex_resume_packet(session, handoff_reason=handoff_reason, prepared_at=prepared_at)
    raise SystemExit(f"Unsupported target agent: {target_agent}")


def render_claude_resume_packet(
    session: SessionState,
    *,
    handoff_reason: str,
    prepared_at: str,
) -> str:
    source_profile = get_agent_profile(session.current_agent)
    lines = [
        "# Claude Code Resume Packet",
        "",
        "Resume this Agent Relay session from the structured state below.",
        "",
        "Priority for this turn:",
        "- Reconstruct the current repo state from the listed files and notes.",
        "- Continue from the recorded next action instead of re-planning from scratch.",
        "- Write a new checkpoint before another handoff.",
        "",
        "Session snapshot:",
        f"- Objective: {session.objective}",
        f"- Repository root: {session.repo_root}",
        f"- Current status: {session.current_status}",
        f"- Latest checkpoint: {session.latest_checkpoint_id or 'Not recorded'}",
        f"- Source agent: {source_profile.display_name}",
        f"- Prepared at: {prepared_at}",
        f"- Handoff reason: {handoff_reason}",
        f"- Next action: {session.next_action or 'Not recorded'}",
        "",
        "Validation:",
        f"- Status: {session.validation.status}",
        f"- Summary: {session.validation.summary or 'None recorded'}",
        "",
    ]
    append_bullet_section(lines, "Decisions:", session.decisions)
    append_bullet_section(lines, "Blockers:", session.blockers)
    append_bullet_section(lines, "Research notes:", session.research_notes)
    append_bullet_section(lines, "Implementation notes:", session.implementation_notes)
    append_bullet_section(lines, "Touched files:", session.touched_files)
    append_bullet_section(lines, "Recent handoffs:", render_recent_handoffs(session))
    return "\n".join(lines) + "\n"


def render_codex_resume_packet(
    session: SessionState,
    *,
    handoff_reason: str,
    prepared_at: str,
) -> str:
    source_profile = get_agent_profile(session.current_agent)
    lines = [
        "# Codex Resume Packet",
        "",
        "You are taking over an in-progress Agent Relay session in this repository.",
        "",
        "Execution brief:",
        f"- Objective: {session.objective}",
        f"- Repository root: {session.repo_root}",
        f"- Current status: {session.current_status}",
        f"- Latest checkpoint: {session.latest_checkpoint_id or 'Not recorded'}",
        f"- Source agent: {source_profile.display_name}",
        f"- Prepared at: {prepared_at}",
        f"- Handoff reason: {handoff_reason}",
        f"- Immediate next step: {session.next_action or 'Not recorded'}",
        "",
        "Operational constraints:",
        "- Work from the repository state on disk.",
        "- Preserve repo-local session state under .agent-relay/.",
        "- Update the session checkpoint before another failover.",
        "",
        "Validation:",
        f"- Status: {session.validation.status}",
        f"- Summary: {session.validation.summary or 'None recorded'}",
        "",
    ]
    append_bullet_section(lines, "Decisions to preserve:", session.decisions)
    append_bullet_section(lines, "Blockers to resolve:", session.blockers)
    append_bullet_section(lines, "Research context:", session.research_notes)
    append_bullet_section(lines, "Implementation context:", session.implementation_notes)
    append_bullet_section(lines, "Files to inspect first:", session.touched_files)
    append_bullet_section(lines, "Recent handoffs:", render_recent_handoffs(session))
    return "\n".join(lines) + "\n"


def append_bullet_section(lines: list[str], heading: str, items: list[str]) -> None:
    lines.append(heading)
    if items:
        lines.extend([f"- {item}" for item in items])
    else:
        lines.append("- None recorded")
    lines.append("")


def render_recent_handoffs(session: SessionState) -> list[str]:
    if not session.handoffs:
        return []

    rendered = []
    for handoff in session.handoffs[-3:]:
        source = describe_agent(handoff.from_agent)
        target = describe_agent(handoff.to_agent)
        rendered.append(f"{handoff.prepared_at}: {source} -> {target} ({handoff.reason})")
    return rendered


def describe_agent(agent: str) -> str:
    return get_agent_profile(agent).display_name


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

    profile = get_agent_profile(to_agent)
    launch_command, launch_template, launch_template_source = render_launch_command(profile, repo_root, resume_path)
    return HandoffRecord(
        from_agent=session.current_agent,
        to_agent=to_agent,
        reason=reason,
        prepared_at=prepared_at,
        checkpoint_id=session.latest_checkpoint_id,
        resume_packet_path=str(resume_path),
        launch_status="ready",
        launch_profile=profile.display_name,
        launch_cwd=str(repo_root),
        launch_command=launch_command,
        launch_template=launch_template,
        launch_template_source=launch_template_source,
        launch_instructions=render_launch_instructions(profile, repo_root, resume_path),
    )


def latest_handoff(session: SessionState) -> HandoffRecord:
    if not session.handoffs:
        raise SystemExit("No handoff has been prepared for this session")
    return session.handoffs[-1]


def write_latest_summary(repo_root: Path, session: SessionState) -> None:
    if not session.latest_checkpoint_id:
        return
    checkpoint = load_checkpoint(repo_root, session.session_id, session.latest_checkpoint_id)
    write_summary(repo_root, session, checkpoint)


def launch_handoff(repo_root: Path, session: SessionState, handoff: HandoffRecord) -> int:
    launched_at = utc_now()
    handoff.launch_status = "launching"
    handoff.launched_at = launched_at
    session.current_status = "launching"
    session.updated_at = launched_at
    save_session(repo_root, session)
    write_latest_summary(repo_root, session)

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
    write_latest_summary(repo_root, session)
    return completed.returncode


def cmd_start(args: argparse.Namespace) -> int:
    repo_root = default_repo_root(args.repo)
    session_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
    now = utc_now()
    session = SessionState(
        schema_version=SCHEMA_VERSION,
        session_id=session_id,
        repo_root=str(repo_root),
        objective=args.task,
        workstream_kind=args.workstream_kind,
        current_agent=args.agent,
        current_status="active",
        created_at=now,
        updated_at=now,
        next_action=args.next_action or "",
        decisions=[],
        blockers=[],
        research_notes=[],
        implementation_notes=[],
        touched_files=[],
        validation=ValidationState(status="not_run", summary=""),
        handoffs=[],
        latest_checkpoint_id=None,
    )
    checkpoint = create_checkpoint(repo_root, session, created_at=now)
    save_session(repo_root, session)
    write_summary(repo_root, session, checkpoint)
    print(f"Created session {session_id}")
    print(Path(session.repo_root) / ".agent-relay" / "sessions" / session_id / "state.json")
    return 0


def cmd_checkpoint(args: argparse.Namespace) -> int:
    repo_root = default_repo_root(args.repo)
    session = load_session(repo_root, args.session)
    session.updated_at = utc_now()
    if args.status:
        session.current_status = args.status
    if args.next_action is not None:
        session.next_action = args.next_action
    if args.decision:
        session.decisions.extend(args.decision)
    if args.blocker:
        session.blockers.extend(args.blocker)
    if args.touched_file:
        session.touched_files.extend(args.touched_file)
    checkpoint = create_checkpoint(repo_root, session, created_at=session.updated_at)
    save_session(repo_root, session)
    write_summary(repo_root, session, checkpoint)
    print(f"Updated session {args.session}")
    print(checkpoint.checkpoint_id)
    return 0


def cmd_failover(args: argparse.Namespace) -> int:
    repo_root = default_repo_root(args.repo)
    session = load_session(repo_root, args.session)
    prepared_at = utc_now()
    resume_path = resume_dir(repo_root, session.session_id) / f"{args.to_agent}.md"
    resume_packet = render_resume_packet(
        session,
        args.to_agent,
        handoff_reason=args.reason,
        prepared_at=prepared_at,
    )
    write_text_atomic(resume_path, resume_packet)

    handoff = build_handoff_record(
        session,
        repo_root=repo_root,
        to_agent=args.to_agent,
        reason=args.reason,
        prepared_at=prepared_at,
        resume_path=resume_path,
    )
    session.handoffs.append(handoff)
    session.current_status = "handoff_prepared"
    session.updated_at = prepared_at
    save_session(repo_root, session)
    write_latest_summary(repo_root, session)
    print(f"Prepared handoff from {handoff.from_agent} to {handoff.to_agent}")
    print(resume_path)
    print(handoff.launch_command)
    return 0


def cmd_launch(args: argparse.Namespace) -> int:
    repo_root = default_repo_root(args.repo)
    session = load_session(repo_root, args.session)
    handoff = latest_handoff(session)

    print(f"Launch target: {handoff.to_agent}")
    print(handoff.resume_packet_path)
    print(handoff.launch_command)
    if not args.execute:
        print(handoff.launch_instructions)
        return 0

    return launch_handoff(repo_root, session, handoff)


def cmd_inspect(args: argparse.Namespace) -> int:
    repo_root = default_repo_root(args.repo)
    session = load_session(repo_root, args.session)
    print(json.dumps(session.to_dict(), indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-relay")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="Create a new relay session")
    start.add_argument("--agent", required=True, choices=AGENT_NAMES)
    start.add_argument("--task", required=True)
    start.add_argument("--workstream-kind", default="mixed", choices=["research", "implementation", "mixed"])
    start.add_argument("--next-action")
    start.add_argument("--repo")
    start.set_defaults(func=cmd_start)

    checkpoint = subparsers.add_parser("checkpoint", help="Update session state and create a checkpoint")
    checkpoint.add_argument("session")
    checkpoint.add_argument("--status", choices=sorted(SESSION_STATUSES))
    checkpoint.add_argument("--next-action")
    checkpoint.add_argument("--decision", action="append")
    checkpoint.add_argument("--blocker", action="append")
    checkpoint.add_argument("--touched-file", action="append")
    checkpoint.add_argument("--repo")
    checkpoint.set_defaults(func=cmd_checkpoint)

    failover = subparsers.add_parser("failover", help="Prepare a handoff packet")
    failover.add_argument("session")
    failover.add_argument("--to-agent", required=True, choices=AGENT_NAMES)
    failover.add_argument("--reason", required=True)
    failover.add_argument("--repo")
    failover.set_defaults(func=cmd_failover)

    launch = subparsers.add_parser("launch", help="Launch the latest prepared handoff")
    launch.add_argument("session")
    launch.add_argument("--repo")
    launch.add_argument(
        "--execute",
        action="store_true",
        help="Run the prepared launch command instead of printing it",
    )
    launch.set_defaults(func=cmd_launch)

    inspect = subparsers.add_parser("inspect", help="Print session state")
    inspect.add_argument("session")
    inspect.add_argument("--repo")
    inspect.set_defaults(func=cmd_inspect)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
