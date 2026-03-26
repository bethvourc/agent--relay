from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path

from agent_relay.agents import AGENT_NAMES
from agent_relay.read_views import list_sessions_for_dashboard, load_session_for_inspect
from agent_relay.capture_support import CaptureOptions
from agent_relay.errors import V2Error
from agent_relay.lifecycle import CHECKPOINT_STATUS_DIRECTIVES
from agent_relay.models import VALIDATION_STATUSES
from agent_relay.resume_options import EVIDENCE_DEPTHS
from agent_relay.ui import (
    create_console,
    emit_json,
    emit_quiet,
    render_checkpoint_success,
    render_dashboard,
    render_error,
    render_failover_success,
    render_help,
    render_inspect,
    render_launch_executing,
    render_launch_preview,
    render_launch_result,
    render_pause_success,
    render_prepare_success,
    render_start_success,
)
from agent_relay.checkpoints import create_checkpoint_for_command
from agent_relay.bootstrap import start_session
from agent_relay.handoffs import (
    create_handoff_for_command,
    execute_launch_for_command,
    preview_launch_for_command,
    resume_handoff_for_command,
)
from agent_relay.repair import repair_session


def default_repo_root(repo: str | None) -> Path:
    return Path(repo or os.getcwd()).resolve()


def build_capture_options(
    args: argparse.Namespace,
    *,
    default_status: str | None = None,
) -> CaptureOptions:
    status = default_status
    if hasattr(args, "status") and args.status:
        status = args.status
    next_action = getattr(args, "next_action", None)
    if next_action is not None:
        next_action = next_action.strip()
    return CaptureOptions(
        status=status,
        snapshot_mode=getattr(args, "snapshot_mode", None),
        next_action=next_action,
        decisions=list(getattr(args, "decision", None) or []),
        blockers=list(getattr(args, "blocker", None) or []),
        touched_files=list(getattr(args, "touched_file", None) or []),
        research_notes=list(getattr(args, "research_note", None) or []),
        implementation_notes=list(getattr(args, "implementation_note", None) or []),
        validation_status=getattr(args, "validation_status", None),
        validation_summary=getattr(args, "validation_summary", None),
        research_note_file=getattr(args, "research_note_file", None),
        implementation_note_file=getattr(args, "implementation_note_file", None),
        validation_summary_file=getattr(args, "validation_summary_file", None),
        capture_git_changes=bool(getattr(args, "capture_git_changes", False)),
    )


def run_capture_command(
    args: argparse.Namespace,
    *,
    command_name: str,
) -> int:
    repo_root = default_repo_root(args.repo)
    options = build_capture_options(args, default_status=None)
    result = create_checkpoint_for_command(
        repo_root,
        args.session,
        command_name=command_name,
        options=options,
        owner=f"cli:{command_name}",
    )

    if args.json:
        emit_json({
            "command": command_name,
            "session_id": args.session,
            "checkpoint_id": result.checkpoint_id,
            "status": result.phase,
            "next_action": result.next_action,
            "validation_status": result.validation_status,
            "capture_mode": result.capture_mode,
        })
    elif args.quiet:
        emit_quiet(result.checkpoint_id)
    elif command_name == "pause":
        render_pause_success(args.console, args.session, result.checkpoint_id, result.next_action)
    elif command_name == "prepare":
        render_prepare_success(args.console, args.session, result.checkpoint_id, result.next_action)
    else:
        render_checkpoint_success(args.console, args.session, result.checkpoint_id)
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    repo_root = default_repo_root(args.repo)
    session_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
    result = start_session(
        repo_root,
        session_id=session_id,
        objective=args.task,
        workstream_kind=args.workstream_kind,
        initial_agent=args.agent,
        next_action=args.next_action or "",
        snapshot_mode=getattr(args, "snapshot_mode", None),
        owner="cli:start",
    )
    state_path = result.session_path

    if args.json:
        emit_json({
            "command": "start",
            "session_id": session_id,
            "state_path": state_path,
            "agent": args.agent,
            "objective": args.task,
            "status": result.phase,
            "checkpoint_id": result.checkpoint_id,
            "storage_model": "journal_v2",
        })
    elif args.quiet:
        emit_quiet(session_id)
    else:
        render_start_success(args.console, session_id, state_path, args.agent, args.task)
    return 0


def cmd_checkpoint(args: argparse.Namespace) -> int:
    return run_capture_command(args, command_name="checkpoint")


def cmd_pause(args: argparse.Namespace) -> int:
    return run_capture_command(args, command_name="pause")


def cmd_prepare(args: argparse.Namespace) -> int:
    return run_capture_command(args, command_name="prepare")


def cmd_failover(args: argparse.Namespace) -> int:
    repo_root = default_repo_root(args.repo)
    result = create_handoff_for_command(
        repo_root,
        args.session,
        to_agent=args.to_agent,
        reason=args.reason,
        evidence_depth=args.resume_evidence_depth,
        owner="cli:failover",
    )
    if args.json:
        emit_json({
            "command": "failover",
            "session_id": args.session,
            "handoff_id": result.handoff_id,
            "to_agent": result.to_agent,
            "resume_path": result.resume_path,
            "launch_command": result.launch_command,
            "launch_instructions": result.launch_instructions,
        })
    elif args.quiet:
        emit_quiet(result.resume_path)
        emit_quiet(result.launch_command)
    else:
        session_view = load_session_for_inspect(repo_root, args.session)
        render_failover_success(
            args.console,
            session_view["current_agent"],
            result.to_agent,
            args.reason,
            result.resume_path,
            result.launch_command,
        )
    return 0


def cmd_launch(args: argparse.Namespace) -> int:
    repo_root = default_repo_root(args.repo)
    preview = preview_launch_for_command(
        repo_root,
        args.session,
        handoff_id=getattr(args, "handoff_id", None),
        owner="cli:launch",
    )

    if not args.execute:
        if args.json:
            emit_json({
                "command": "launch",
                "mode": "dry_run",
                "session_id": args.session,
                "handoff_id": preview.handoff_id,
                "target": preview.to_agent,
                "resume_path": preview.resume_path,
                "launch_command": preview.launch_command,
                "launch_instructions": preview.launch_instructions,
                "packet_aware": preview.packet_aware,
                "execute_policy": preview.execute_policy,
                "warning": preview.warning,
            })
        elif args.quiet:
            emit_quiet(preview.launch_command)
        else:
            render_launch_preview(
                args.console,
                preview.to_agent,
                preview.resume_path,
                preview.launch_command,
                preview.launch_instructions,
                warning=preview.warning,
            )
        return 0

    if not args.json and not args.quiet and not getattr(args, "yes", False) and sys.stdin.isatty():
        from rich.prompt import Confirm

        render_launch_preview(
            args.console,
            preview.to_agent,
            preview.resume_path,
            preview.launch_command,
            preview.launch_instructions,
            warning=preview.warning,
        )
        if not Confirm.ask("\n  [brand]Execute launch?[/]", console=args.console, default=True):
            args.console.print("  [muted]Launch cancelled.[/]")
            return 0

    if not args.json and not args.quiet:
        with render_launch_executing(args.console):
            result = execute_launch_for_command(
                repo_root,
                args.session,
                handoff_id=getattr(args, "handoff_id", None),
                owner="cli:launch",
            )
        render_launch_result(args.console, result.exit_code == 0, result.exit_code)
    else:
        result = execute_launch_for_command(
            repo_root,
            args.session,
            handoff_id=getattr(args, "handoff_id", None),
            owner="cli:launch",
        )

    if args.json:
        emit_json({
            "command": "launch",
            "mode": "execute",
            "session_id": args.session,
            "handoff_id": result.handoff_id,
            "launch_id": result.launch_id,
            "target": result.to_agent,
            "exit_code": result.exit_code,
            "launch_status": result.launch_status,
            "stdout_path": result.stdout_path,
            "stderr_path": result.stderr_path,
            "ownership_transferred": False,
        })
    elif not args.quiet:
        args.console.print(
            "  [muted]Process dispatch does not transfer ownership. "
            "Run `agent-relay resume` after the target agent adopts the packet.[/]",
            highlight=False,
        )

    return result.exit_code


def cmd_resume(args: argparse.Namespace) -> int:
    repo_root = default_repo_root(args.repo)
    result = resume_handoff_for_command(
        repo_root,
        args.session,
        handoff_id=getattr(args, "handoff_id", None),
        owner="cli:resume",
    )
    if args.json:
        emit_json({
            "command": "resume",
            "session_id": args.session,
            "handoff_id": result.handoff_id,
            "current_agent": result.current_agent,
            "status": result.phase,
        })
    elif args.quiet:
        emit_quiet(result.handoff_id)
    else:
        args.console.print(
            f"[success]Resume accepted[/]  [label]handoff:[/] [brand]{result.handoff_id}[/]  "
            f"[label]agent:[/] {result.current_agent}",
            highlight=False,
        )
    return 0


def cmd_repair(args: argparse.Namespace) -> int:
    repo_root = default_repo_root(args.repo)
    selected = [
        ("rebuild_view", bool(getattr(args, "rebuild_view", False))),
        ("rollback_pending", bool(getattr(args, "rollback_pending", False))),
        ("promote_last_good", bool(getattr(args, "promote_last_good", False))),
    ]
    actions = [name for name, enabled in selected if enabled]
    if len(actions) != 1:
        raise SystemExit(
            "repair requires exactly one action: --rebuild-view, --rollback-pending, "
            "or --promote-last-good"
        )

    result = repair_session(
        repo_root,
        args.session,
        action=actions[0],
        owner="cli:repair",
    )
    if args.json:
        emit_json(result.to_dict())
    elif args.quiet:
        emit_quiet(result.repair_log_path)
    else:
        args.console.print(
            f"[success]Repair complete[/]  [label]session:[/] [brand]{args.session}[/]  "
            f"[label]action:[/] {actions[0]}  [label]health:[/] {result.health_before} -> {result.health_after}",
            highlight=False,
        )
        args.console.print(f"  [label]Receipt:[/] [path]{result.repair_log_path}[/]", highlight=False)
        if result.repair_event_id:
            args.console.print(f"  [label]Event:[/] [muted]{result.repair_event_id}[/]", highlight=False)
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    repo_root = default_repo_root(args.repo)
    session = load_session_for_inspect(repo_root, args.session)
    if args.json:
        emit_json(session)
    elif args.quiet:
        emit_quiet(str(session["session_id"]))
    else:
        render_inspect(args.console, session)
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    repo_root = default_repo_root(args.repo)
    sessions = list_sessions_for_dashboard(repo_root)

    if args.json:
        emit_json({
            "command": "dashboard",
            "sessions": [
                {
                    "session_id": s["session_id"],
                    "agent": s["current_agent"],
                    "status": s["current_status"],
                    "objective": s["objective"],
                    "updated_at": s["updated_at"],
                    "storage_model": s.get("storage_model"),
                    "health": s.get("health"),
                    "error": s.get("error"),
                }
                for s in sessions
            ],
        })
    elif args.quiet:
        for s in sessions:
            emit_quiet(str(s["session_id"]))
    else:
        render_dashboard(args.console, sessions)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-relay", add_help=False)
    parser.add_argument("--help", "-h", action="store_true", default=False)
    parser.add_argument("--json", action="store_true", help="Machine-readable JSON output")
    parser.add_argument("--quiet", "-q", action="store_true", help="Minimal output")
    subparsers = parser.add_subparsers(dest="command")

    start = subparsers.add_parser("start", help="Create a new relay session")
    start.add_argument("--agent", required=True, choices=AGENT_NAMES)
    start.add_argument("--task", required=True)
    start.add_argument("--workstream-kind", default="mixed", choices=["research", "implementation", "mixed"])
    start.add_argument("--next-action")
    start.add_argument(
        "--snapshot-mode",
        choices=["full"],
        help="For non-Git repos, require an explicit full workspace snapshot for the initial v2 checkpoint",
    )
    start.add_argument("--repo")
    start.set_defaults(func=cmd_start)

    checkpoint = subparsers.add_parser("checkpoint", help="Update session state and create a checkpoint")
    checkpoint.add_argument("session")
    add_capture_arguments(checkpoint, allow_status=True)
    checkpoint.add_argument("--repo")
    checkpoint.set_defaults(func=cmd_checkpoint)

    pause = subparsers.add_parser("pause", help="Pause a session and write a final checkpoint")
    pause.add_argument("session")
    add_capture_arguments(pause, allow_status=False)
    pause.add_argument("--repo")
    pause.set_defaults(func=cmd_pause)

    prepare = subparsers.add_parser("prepare", help="Capture a clean pre-handoff checkpoint")
    prepare.add_argument("session")
    add_capture_arguments(prepare, allow_status=False)
    prepare.add_argument("--repo")
    prepare.set_defaults(func=cmd_prepare)

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
    launch.add_argument("--handoff-id", help="For v2 sessions, launch this prepared handoff id")
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

    resume = subparsers.add_parser("resume", help="Accept a prepared v2 handoff and transfer ownership")
    resume.add_argument("session")
    resume.add_argument("--repo")
    resume.add_argument("--handoff-id", help="For v2 sessions, resume this prepared handoff id")
    resume.set_defaults(func=cmd_resume)

    repair = subparsers.add_parser("repair", help="Repair v2 session integrity explicitly")
    repair.add_argument("session")
    repair.add_argument("--repo")
    repair.add_argument("--rebuild-view", action="store_true", help="Rebuild refs/ and derived/ from a healthy journal")
    repair.add_argument("--rollback-pending", action="store_true", help="Quarantine pending transaction residue")
    repair.add_argument("--promote-last-good", action="store_true", help="Quarantine the corrupted journal tail and recover to the last verified event")
    repair.set_defaults(func=cmd_repair)

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


def add_capture_arguments(parser: argparse.ArgumentParser, *, allow_status: bool) -> None:
    if allow_status:
        parser.add_argument("--status", choices=sorted(CHECKPOINT_STATUS_DIRECTIVES))
    parser.add_argument(
        "--snapshot-mode",
        choices=["full"],
        help="For v2 sessions, require explicit full workspace snapshot capture instead of Git-backed capture",
    )
    parser.add_argument("--next-action", "-n")
    parser.add_argument("--decision", "-d", action="append")
    parser.add_argument("--blocker", "-b", action="append")
    parser.add_argument("--touched-file", "-f", action="append")
    parser.add_argument("--research-note", "-r", action="append")
    parser.add_argument("--implementation-note", "-i", action="append")
    parser.add_argument("--research-note-file")
    parser.add_argument("--implementation-note-file")
    parser.add_argument("--validation-status", choices=sorted(VALIDATION_STATUSES))
    parser.add_argument("--validation-summary")
    parser.add_argument("--validation-summary-file")
    parser.add_argument("--capture-git-changes", "-g", action="store_true")


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    args.console = create_console(json_mode=args.json, quiet=args.quiet)

    if args.help or args.command is None:
        render_help(args.console)
        return 0

    try:
        return args.func(args)
    except SystemExit as exc:
        message = str(exc) if str(exc) else "Unknown error"
        if args.json:
            emit_json({"error": message})
        elif not args.quiet:
            render_error(args.console, message)
        return exc.code if isinstance(exc.code, int) else 1
    except V2Error as exc:
        message = str(exc) if str(exc) else "Unknown error"
        if args.json:
            emit_json({"error": message})
        elif not args.quiet:
            render_error(args.console, message)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
