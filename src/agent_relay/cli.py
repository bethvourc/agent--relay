from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agent_relay.agents import AGENT_NAMES, AGENT_REGISTRY
from agent_relay.errors import RelayError
from agent_relay.relay import relay as do_relay
from agent_relay.read_views import list_sessions_for_dashboard
from agent_relay.ui import (
    create_console,
    emit_json,
    emit_quiet,
    render_dashboard,
    render_error,
    render_help,
    render_relay_launch_result,
    render_relay_launching,
    render_relay_success,
)


def _resolve_repo(repo: str | None) -> Path:
    import os
    return Path(repo or os.getcwd()).resolve()


def cmd_relay(args: argparse.Namespace) -> int:
    repo_root = _resolve_repo(args.repo)
    to_agent = args.to
    from_agent = getattr(args, "from_agent", None)
    task = getattr(args, "task", None)
    no_launch = getattr(args, "no_launch", False)

    result = do_relay(
        repo_root,
        to_agent=to_agent,
        from_agent=from_agent,
        task=task,
        no_launch=no_launch,
        owner="cli:relay",
    )

    if args.json:
        emit_json({
            "command": "relay",
            "session_id": result.session_id,
            "from_agent": result.from_agent,
            "to_agent": result.to_agent,
            "handoff_id": result.handoff_id,
            "resume_path": result.resume_path,
            "launch_command": result.launch_command,
            "created_session": result.created_session,
        })
    elif args.quiet:
        emit_quiet(result.resume_path)
    else:
        render_relay_success(
            args.console,
            result.from_agent,
            result.to_agent,
            result.session_id,
            result.resume_path,
            result.launch_command,
            created_session=result.created_session,
            no_launch=no_launch,
        )

    if not no_launch:
        from agent_relay.handoffs import execute_launch_for_command

        if not args.json and not args.quiet and not getattr(args, "yes", False) and sys.stdin.isatty():
            from rich.prompt import Confirm

            if not Confirm.ask("\n  [brand]Launch target agent?[/]", console=args.console, default=True):
                args.console.print("  [muted]Launch skipped. Run manually with the command above.[/]")
                return 0

        if not args.json and not args.quiet:
            with render_relay_launching(args.console):
                launch_result = execute_launch_for_command(
                    repo_root,
                    result.session_id,
                    handoff_id=result.handoff_id,
                    owner="cli:relay:launch",
                )
            render_relay_launch_result(args.console, launch_result.exit_code == 0, launch_result.exit_code)
        else:
            launch_result = execute_launch_for_command(
                repo_root,
                result.session_id,
                handoff_id=result.handoff_id,
                owner="cli:relay:launch",
            )

        return launch_result.exit_code

    return 0


def cmd_status(args: argparse.Namespace) -> int:
    repo_root = _resolve_repo(args.repo)
    sessions = list_sessions_for_dashboard(repo_root)

    if args.json:
        emit_json({
            "command": "status",
            "sessions": [
                {
                    "session_id": s["session_id"],
                    "agent": s["current_agent"],
                    "status": s["current_status"],
                    "objective": s["objective"],
                    "updated_at": s["updated_at"],
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

    # agent-relay <agent> — the one command users need
    for agent_key in AGENT_NAMES:
        agent_cmd = subparsers.add_parser(agent_key, help=f"Relay to {AGENT_REGISTRY[agent_key].display_name}")
        agent_cmd.add_argument("--from", dest="from_agent", choices=AGENT_NAMES, help="Source agent (auto-detected)")
        agent_cmd.add_argument("--task", "-t", help="What the next agent should do")
        agent_cmd.add_argument("--no-launch", action="store_true", help="Just create the packet")
        agent_cmd.add_argument("--yes", "-y", action="store_true", help="Skip confirmation")
        agent_cmd.add_argument("--repo", help="Repository path (default: cwd)")
        agent_cmd.set_defaults(func=cmd_relay, to=agent_key)

    # agent-relay status — view sessions
    status = subparsers.add_parser("status", help="Show relay sessions")
    status.add_argument("--repo")
    status.set_defaults(func=cmd_status)

    return parser


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
    except RelayError as exc:
        message = str(exc) if str(exc) else "Unknown error"
        if args.json:
            emit_json({"error": message})
        elif not args.quiet:
            render_error(args.console, message)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
