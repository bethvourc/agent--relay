from __future__ import annotations

import argparse
import json
import secrets
from datetime import UTC, datetime
from pathlib import Path

from agent_relay.agents import AGENT_NAMES
from agent_relay.checkpoints import create_checkpoint, utc_now
from agent_relay.launcher import build_handoff_record, launch_handoff, launch_preview_lines, latest_handoff
from agent_relay.models import (
    SCHEMA_VERSION,
    SESSION_STATUSES,
    SessionState,
    ValidationState,
)
from agent_relay.resume import EVIDENCE_DEPTHS, ResumeRenderOptions, render_resume_packet
from agent_relay.storage import (
    default_repo_root,
    load_checkpoint,
    load_session,
    resume_dir,
    save_session,
    write_text_atomic,
)
from agent_relay.summary import write_summary


def write_latest_summary(repo_root: Path, session: SessionState) -> None:
    if not session.latest_checkpoint_id:
        return
    checkpoint = load_checkpoint(repo_root, session.session_id, session.latest_checkpoint_id)
    write_summary(repo_root, session, checkpoint)


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
    if not session.latest_checkpoint_id:
        raise SystemExit("Session has no checkpoint to hand off")
    prepared_at = utc_now()
    resume_path = resume_dir(repo_root, session.session_id) / f"{args.to_agent}.md"
    checkpoint = load_checkpoint(repo_root, session.session_id, session.latest_checkpoint_id)
    resume_packet = render_resume_packet(
        session,
        checkpoint,
        args.to_agent,
        handoff_reason=args.reason,
        prepared_at=prepared_at,
        options=ResumeRenderOptions(evidence_depth=args.resume_evidence_depth),
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
    preview_lines = launch_preview_lines(handoff)

    for line in preview_lines[:3]:
        print(line)
    if not args.execute:
        print(preview_lines[3])
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
    failover.add_argument(
        "--resume-evidence-depth",
        default="standard",
        choices=sorted(EVIDENCE_DEPTHS),
        help="How much latest-checkpoint evidence to include in the resume packet",
    )
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
