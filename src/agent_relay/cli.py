from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agent_relay.agents import AGENT_NAMES, AGENT_REGISTRY, resolve_agent_key
from agent_relay.errors import RelayError
from agent_relay.relay import relay as do_relay
from agent_relay.read_views import list_sessions_for_dashboard
from agent_relay.ui import (
    create_console,
    emit_json,
    emit_quiet,
    render_concurrent_result,
    render_concurrent_start,
    render_converse_result,
    render_converse_start,
    render_converse_turn_active,
    render_converse_turn_done,
    render_dashboard,
    render_discover_results,
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


def cmd_discover(args: argparse.Namespace) -> int:
    from agent_relay.agents import discover

    results = discover()

    if args.json:
        emit_json({
            "command": "discover",
            "agents": [
                {
                    "key": r.key,
                    "display_name": r.display_name,
                    "cli_command": r.cli_command,
                    "alias": r.alias,
                    "available": r.available,
                    "cli_path": r.cli_path,
                    "version": r.version,
                }
                for r in results
            ],
        })
    elif args.quiet:
        for r in results:
            if r.available:
                emit_quiet(r.key)
    else:
        render_discover_results(args.console, results)

    return 0


def _parse_agents_and_task(args: argparse.Namespace, min_agents: int = 2) -> tuple[list[str], str]:
    """Parse the positional args into agent keys and a task string.

    Supports two forms:
        agent-relay chat c x "fix tests"       # task as last positional
        agent-relay chat c x -t "fix tests"    # task via flag
    """
    from agent_relay.agents import AGENT_ALIASES, AGENT_REGISTRY

    raw_args: list[str] = args.args
    task_flag: str | None = getattr(args, "task_flag", None)

    # If -t was given, all positional args are agents
    if task_flag:
        agents = [resolve_agent_key(a) for a in raw_args]
        task = task_flag
    else:
        # Last arg is the task if it's not a known agent key/alias
        if not raw_args:
            raise SystemExit("Usage: agent-relay chat <agent> [<agent>...] <task>")

        last = raw_args[-1]
        if last in AGENT_REGISTRY or last in AGENT_ALIASES:
            raise SystemExit(
                "Missing task. Provide a task as the last argument or with -t:\n"
                '  agent-relay chat c x "fix the tests"\n'
                '  agent-relay chat c x -t "fix the tests"'
            )
        agents = [resolve_agent_key(a) for a in raw_args[:-1]]
        task = last

    if len(agents) < min_agents:
        raise SystemExit(f"Need at least {min_agents} agents.")

    return agents, task


def cmd_chat(args: argparse.Namespace) -> int:
    from agent_relay.converse import converse as do_converse

    repo_root = _resolve_repo(args.repo)
    console = args.console
    interactive = not args.json and not args.quiet
    agents, task = _parse_agents_and_task(args)

    if interactive:
        render_converse_start(console, agents, task, args.max_turns)

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
        agents=agents,
        task=task,
        max_turns=args.max_turns,
        owner="cli:chat",
        on_turn_start=on_turn_start if interactive else None,
        on_turn_complete=on_turn_complete if interactive else None,
    )

    if _spinner_ctx is not None:
        _spinner_ctx.__exit__(None, None, None)

    if args.json:
        emit_json({
            "command": "chat",
            "session_id": result.session_id,
            "agents": list(result.agents),
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
            result.agents,
            result.turns_completed,
            result.stop_reason,
        )

    return 0


def cmd_race(args: argparse.Namespace) -> int:
    from agent_relay.concurrent import run_concurrent

    repo_root = _resolve_repo(args.repo)
    console = args.console
    interactive = not args.json and not args.quiet
    agents, task = _parse_agents_and_task(args)
    max_time = args.max_time

    if interactive:
        render_concurrent_start(console, agents, task, max_time)

    def on_agent_start(slot: int, agent_key: str) -> None:
        if interactive:
            from agent_relay.agents import get_agent_display_name
            name = get_agent_display_name(agent_key)
            console.print(f"  [brand]▸[/] Slot {slot}: [bold]{name}[/] started", highlight=False)

    def on_agent_done(outcome: "AgentOutcome") -> None:  # noqa: F821
        if interactive:
            from agent_relay.agents import get_agent_display_name
            name = get_agent_display_name(outcome.agent_key)
            status = "[success]done[/]" if outcome.exit_code == 0 else f"[warning]exit {outcome.exit_code}[/]"
            console.print(f"  [brand]▸[/] Slot {outcome.slot}: [bold]{name}[/] {status} — {outcome.summary}", highlight=False)

    result = run_concurrent(
        repo_root,
        agents=agents,
        task=task,
        max_time_seconds=max_time,
        owner="cli:race",
        on_agent_start=on_agent_start if interactive else None,
        on_agent_done=on_agent_done if interactive else None,
    )

    if args.json:
        emit_json({
            "command": "race",
            "session_id": result.session_id,
            "agents": list(result.agents),
            "stop_reason": result.stop_reason,
            "elapsed_seconds": result.elapsed_seconds,
            "outcomes": [
                {
                    "slot": o.slot,
                    "agent": o.agent_key,
                    "exit_code": o.exit_code,
                    "summary": o.summary,
                    "done_signal": o.done_signal,
                    "started_at": o.started_at,
                    "finished_at": o.finished_at,
                }
                for o in result.outcomes
            ],
        })
    elif args.quiet:
        emit_quiet(result.session_id)
    else:
        render_concurrent_result(console, result)

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

    # agent-relay discover — detect available agent CLIs
    disc = subparsers.add_parser("discover", help="Detect available agent CLIs")
    disc.set_defaults(func=cmd_discover)

    # agent-relay chat <agent> [<agent>...] <task> — turn-based conversation
    chat = subparsers.add_parser("chat", help="Turn-based agent-to-agent conversation")
    chat.add_argument("args", nargs="+", metavar="AGENT_OR_TASK", help="Agents and task (last arg is task, or use -t)")
    chat.add_argument("--task", "-t", dest="task_flag", default=None, help="Task (alternative to positional)")
    chat.add_argument("--max-turns", "-n", type=int, default=10, help="Maximum turns (default: 10)")
    chat.add_argument("--yes", "-y", action="store_true", help="Skip confirmation")
    chat.add_argument("--repo", help="Repository path (default: cwd)")
    chat.set_defaults(func=cmd_chat)

    # agent-relay race <agent> [<agent>...] <task> — concurrent agents with tmux
    race = subparsers.add_parser("race", help="Run agents concurrently with live visibility")
    race.add_argument("args", nargs="+", metavar="AGENT_OR_TASK", help="Agents and task (last arg is task, or use -t)")
    race.add_argument("--task", "-t", dest="task_flag", default=None, help="Task (alternative to positional)")
    race.add_argument("--max-time", type=int, default=600, help="Max seconds (default: 600)")
    race.add_argument("--yes", "-y", action="store_true", help="Skip confirmation")
    race.add_argument("--repo", help="Repository path (default: cwd)")
    race.set_defaults(func=cmd_race)

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
