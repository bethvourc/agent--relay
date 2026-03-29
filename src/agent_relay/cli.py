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
    render_converse_result,
    render_converse_start,
    render_converse_turn_active,
    render_converse_turn_done,
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
            args.console.print("\n  [brand]∴ Launching target agent...[/]\n")
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


def cmd_clean(args: argparse.Namespace) -> int:
    import shutil
    from agent_relay.layout import relay_root, sessions_root, session_root
    from agent_relay.storage import is_session

    repo_root = _resolve_repo(args.repo)
    root = relay_root(repo_root)

    if not root.exists():
        if args.json:
            emit_json({"command": "clean", "removed": 0})
        elif not args.quiet:
            args.console.print("  [muted]Nothing to clean.[/]")
        return 0

    if getattr(args, "all", False):
        # Remove the entire .agent-relay directory
        shutil.rmtree(root)
        if args.json:
            emit_json({"command": "clean", "mode": "all", "removed_path": str(root)})
        elif args.quiet:
            emit_quiet(str(root))
        else:
            args.console.print("[success]Cleaned[/]  Removed all relay data.", highlight=False)
        return 0

    # Remove all sessions
    sess_root = sessions_root(repo_root)
    if not sess_root.exists():
        if args.json:
            emit_json({"command": "clean", "removed": 0})
        elif not args.quiet:
            args.console.print("  [muted]No sessions to clean.[/]")
        return 0

    removed = []
    for session_dir in sorted(sess_root.iterdir()):
        if not session_dir.is_dir():
            continue
        session_id = session_dir.name
        shutil.rmtree(session_dir)
        removed.append(session_id)

    if args.json:
        emit_json({"command": "clean", "removed": len(removed), "session_ids": removed})
    elif args.quiet:
        emit_quiet(str(len(removed)))
    else:
        args.console.print(
            f"[success]Cleaned[/]  Removed {len(removed)} session{'s' if len(removed) != 1 else ''}.",
            highlight=False,
        )
    return 0


def cmd_converse(args: argparse.Namespace) -> int:
    from agent_relay.converse import converse as do_converse

    repo_root = _resolve_repo(args.repo)
    console = args.console
    interactive = not args.json and not args.quiet

    if interactive:
        render_converse_start(console, args.agent1, args.agent2, args.task, args.max_turns)

    # Build UI callbacks for interactive mode
    _spinner_ctx = None

    def on_turn_start(agent_key: str, turn_number: int, max_turns: int) -> None:
        nonlocal _spinner_ctx
        if interactive:
            _spinner_ctx = render_converse_turn_active(console, agent_key, turn_number, max_turns)
            _spinner_ctx.__enter__()

    def on_turn_complete(turn: "TurnResult") -> None:  # noqa: F821
        nonlocal _spinner_ctx
        if _spinner_ctx is not None:
            _spinner_ctx.__exit__(None, None, None)
            _spinner_ctx = None
        if interactive:
            render_converse_turn_done(console, turn.turn_number, turn.agent_key, turn.summary, turn.exit_code, turn.text)

    result = do_converse(
        repo_root,
        agent1=args.agent1,
        agent2=args.agent2,
        task=args.task,
        max_turns=args.max_turns,
        owner="cli:converse",
        on_turn_start=on_turn_start if interactive else None,
        on_turn_complete=on_turn_complete if interactive else None,
    )

    # Clean up spinner if interrupted mid-turn
    if _spinner_ctx is not None:
        _spinner_ctx.__exit__(None, None, None)

    if args.json:
        emit_json({
            "command": "converse",
            "session_id": result.session_id,
            "agent1": result.agent1,
            "agent2": result.agent2,
            "turns_completed": result.turns_completed,
            "stop_reason": result.stop_reason,
            "turns": [
                {
                    "turn": t.turn_number,
                    "agent": t.agent_key,
                    "exit_code": t.exit_code,
                    "summary": t.summary,
                    "done_signal": t.done_signal,
                    "started_at": t.started_at,
                    "finished_at": t.finished_at,
                }
                for t in result.turn_results
            ],
        })
    elif args.quiet:
        emit_quiet(result.session_id)
    else:
        render_converse_result(
            console,
            result.session_id,
            result.agent1,
            result.agent2,
            result.turns_completed,
            result.stop_reason,
        )

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

    # agent-relay clean — remove sessions
    clean = subparsers.add_parser("clean", help="Remove all relay sessions")
    clean.add_argument("--all", action="store_true", help="Remove the entire .agent-relay directory")
    clean.add_argument("--repo")
    clean.set_defaults(func=cmd_clean)

    # agent-relay converse <agent1> <agent2> — turn-based agent conversation
    converse = subparsers.add_parser("converse", help="Turn-based agent-to-agent conversation")
    converse.add_argument("agent1", choices=AGENT_NAMES, help="First agent (speaks first)")
    converse.add_argument("agent2", choices=AGENT_NAMES, help="Second agent")
    converse.add_argument("--task", "-t", required=True, help="Task for the agents to work on")
    converse.add_argument("--max-turns", type=int, default=10, help="Maximum turns (default: 10)")
    converse.add_argument("--yes", "-y", action="store_true", help="Skip confirmation")
    converse.add_argument("--repo", help="Repository path (default: cwd)")
    converse.set_defaults(func=cmd_converse)

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
