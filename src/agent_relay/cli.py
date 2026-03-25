from __future__ import annotations

import argparse
import json
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path

from agent_relay.agents import AGENT_NAMES, get_agent_adapter
from agent_relay.checkpoints import create_checkpoint, utc_now
from agent_relay.launcher import build_handoff_record, launch_handoff, latest_handoff
from agent_relay.models import (
    SCHEMA_VERSION,
    SESSION_STATUSES,
    SessionState,
    ValidationState,
)
from agent_relay.resume import EVIDENCE_DEPTHS, ResumeRenderOptions, render_resume_packet
from agent_relay.storage import (
    default_repo_root,
    list_sessions,
    load_checkpoint,
    load_session,
    resume_dir,
    save_session,
    write_text_atomic,
)
from agent_relay.summary import write_summary
from agent_relay.ui import (
    create_console,
    emit_json,
    emit_quiet,
    render_checkpoint_success,
    render_dashboard,
    render_error,
    render_failover_success,
    render_inspect,
    render_launch_executing,
    render_launch_preview,
    render_launch_result,
    render_start_success,
)


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
    state_path = str(Path(session.repo_root) / ".agent-relay" / "sessions" / session_id / "state.json")

    if args.json:
        emit_json({
            "command": "start",
            "session_id": session_id,
            "state_path": state_path,
            "agent": args.agent,
            "objective": args.task,
            "status": "active",
        })
    elif args.quiet:
        emit_quiet(session_id)
    else:
        render_start_success(args.console, session_id, state_path, args.agent, args.task)
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

    if args.json:
        emit_json({
            "command": "checkpoint",
            "session_id": args.session,
            "checkpoint_id": checkpoint.checkpoint_id,
            "status": session.current_status,
        })
    elif args.quiet:
        emit_quiet(checkpoint.checkpoint_id)
    else:
        render_checkpoint_success(args.console, args.session, checkpoint.checkpoint_id)
    return 0


def cmd_failover(args: argparse.Namespace) -> int:
    repo_root = default_repo_root(args.repo)
    session = load_session(repo_root, args.session)
    if not session.latest_checkpoint_id:
        raise SystemExit("Session has no checkpoint to hand off")
    prepared_at = utc_now()
    target_adapter = get_agent_adapter(args.to_agent)
    resume_path = resume_dir(repo_root, session.session_id) / f"{target_adapter.resume_packet_target}.md"
    checkpoint = load_checkpoint(repo_root, session.session_id, session.latest_checkpoint_id)
    resume_packet = render_resume_packet(
        session,
        checkpoint,
        target_adapter.key,
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

    if args.json:
        emit_json({
            "command": "failover",
            "from_agent": handoff.from_agent,
            "to_agent": handoff.to_agent,
            "resume_path": str(resume_path),
            "launch_command": handoff.launch_command,
            "launch_instructions": handoff.launch_instructions,
        })
    elif args.quiet:
        emit_quiet(str(resume_path))
        emit_quiet(handoff.launch_command)
    else:
        render_failover_success(
            args.console,
            handoff.from_agent,
            handoff.to_agent,
            args.reason,
            str(resume_path),
            handoff.launch_command,
        )
    return 0


def cmd_launch(args: argparse.Namespace) -> int:
    repo_root = default_repo_root(args.repo)
    session = load_session(repo_root, args.session)
    handoff = latest_handoff(session)

    if not args.execute:
        if args.json:
            emit_json({
                "command": "launch",
                "mode": "dry_run",
                "target": handoff.to_agent,
                "resume_path": handoff.resume_packet_path,
                "launch_command": handoff.launch_command,
                "launch_instructions": handoff.launch_instructions,
            })
        elif args.quiet:
            emit_quiet(handoff.launch_command)
        else:
            render_launch_preview(
                args.console,
                handoff.to_agent,
                handoff.resume_packet_path,
                handoff.launch_command,
                handoff.launch_instructions,
            )
        return 0

    # Confirm before executing (only in interactive rich mode)
    if not args.json and not args.quiet and not getattr(args, "yes", False) and sys.stdin.isatty():
        from rich.prompt import Confirm

        render_launch_preview(
            args.console,
            handoff.to_agent,
            handoff.resume_packet_path,
            handoff.launch_command,
            handoff.launch_instructions,
        )
        if not Confirm.ask("\n  [brand]Execute launch?[/]", console=args.console, default=True):
            args.console.print("  [muted]Launch cancelled.[/]")
            return 0

    # Execute with spinner
    if not args.json and not args.quiet:
        with render_launch_executing(args.console):
            exit_code = launch_handoff(repo_root, session, handoff)
        render_launch_result(args.console, exit_code == 0, exit_code)
    else:
        exit_code = launch_handoff(repo_root, session, handoff)

    if args.json:
        emit_json({
            "command": "launch",
            "mode": "execute",
            "target": handoff.to_agent,
            "exit_code": exit_code,
            "launch_status": handoff.launch_status,
        })

    return exit_code


def cmd_inspect(args: argparse.Namespace) -> int:
    repo_root = default_repo_root(args.repo)
    session = load_session(repo_root, args.session)
    if args.json:
        emit_json(session.to_dict())
    elif args.quiet:
        emit_quiet(session.session_id)
    else:
        render_inspect(args.console, session.to_dict())
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    repo_root = default_repo_root(args.repo)
    sessions = list_sessions(repo_root)

    if args.json:
        emit_json({
            "command": "dashboard",
            "sessions": [
                {
                    "session_id": s.session_id,
                    "agent": s.current_agent,
                    "status": s.current_status,
                    "objective": s.objective,
                    "updated_at": s.updated_at,
                }
                for s in sessions
            ],
        })
    elif args.quiet:
        for s in sessions:
            emit_quiet(s.session_id)
    else:
        render_dashboard(args.console, [s.to_dict() for s in sessions])
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-relay")
    parser.add_argument("--json", action="store_true", help="Machine-readable JSON output")
    parser.add_argument("--quiet", "-q", action="store_true", help="Minimal output")
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
    launch.add_argument(
        "--yes", "-y",
        action="store_true",
        help="Skip confirmation prompt",
    )
    launch.set_defaults(func=cmd_launch)

    inspect = subparsers.add_parser("inspect", help="Print session state")
    inspect.add_argument("session")
    inspect.add_argument("--repo")
    inspect.set_defaults(func=cmd_inspect)

    dashboard = subparsers.add_parser("dashboard", help="Show all sessions in this repo")
    dashboard.add_argument("--repo")
    dashboard.set_defaults(func=cmd_dashboard)

    list_cmd = subparsers.add_parser("list", help="Show all sessions (alias for dashboard)")
    list_cmd.add_argument("--repo")
    list_cmd.set_defaults(func=cmd_dashboard)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    args.console = create_console(json_mode=args.json, quiet=args.quiet)
    try:
        return args.func(args)
    except SystemExit as exc:
        message = str(exc) if str(exc) else "Unknown error"
        if args.json:
            emit_json({"error": message})
        elif not args.quiet:
            render_error(args.console, message)
        return exc.code if isinstance(exc.code, int) else 1


if __name__ == "__main__":
    raise SystemExit(main())
