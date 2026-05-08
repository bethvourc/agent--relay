from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC
from pathlib import Path

from agent_relay.agents import AGENT_NAMES, AGENT_REGISTRY, resolve_agent_key
from agent_relay.errors import RelayError
from agent_relay.exporters.jsonl import parse_header_pairs, tail_jsonl
from agent_relay.exporters.otlp import serve_otlp
from agent_relay.exporters.prometheus import serve_prometheus
from agent_relay.metrics import (
    extract_cross_session_metrics,
    extract_session_metrics,
)
from agent_relay.metrics_ui import (
    emit_cross_session_metrics_json,
    emit_cross_session_metrics_quiet,
    emit_session_metrics_json,
    emit_session_metrics_quiet,
    render_cross_session_metrics,
    render_session_metrics,
)
from agent_relay.read_views import list_sessions_for_dashboard
from agent_relay.relay import relay as do_relay
from agent_relay.storage import is_session
from agent_relay.ui import (
    create_console,
    emit_json,
    emit_quiet,
    render_concurrent_result,
    render_concurrent_start,
    render_conflict_inspect,
    render_converse_result,
    render_converse_start,
    render_converse_turn_active,
    render_converse_turn_done,
    render_dashboard,
    render_discover_results,
    render_error,
    render_help,
    render_relay_launch_result,
    render_relay_success,
)
from agent_relay.watch import (
    WatchSource,
    pick_latest_active_session,
    pick_latest_session,
)
from agent_relay.watch_ui import (
    render_watch_live,
    stream_json_events,
    stream_quiet_lines,
)


def _resolve_repo(repo: str | None) -> Path:
    return Path(repo or os.getcwd()).resolve()


def _should_auto_open_terminals(
    *,
    interactive: bool,
    requested: bool | None,
) -> bool:
    if not interactive:
        return False
    if requested is not None:
        return requested
    env_value = os.getenv("AGENT_RELAY_OPEN_TERMINALS")
    if env_value is not None:
        return env_value.strip().lower() in {"1", "true", "yes", "on"}
    return sys.platform == "darwin"


def _open_tmux_session_in_terminal(tmux_session: str) -> str | None:
    if sys.platform != "darwin":
        return "Automatic terminal opening is currently only supported on macOS."

    command = f"tmux attach-session -t {tmux_session}"
    term_program = os.getenv("TERM_PROGRAM", "")
    if term_program == "iTerm.app":
        script = "\n".join(
            [
                'tell application "iTerm"',
                "activate",
                "set newWindow to (create window with default profile)",
                "tell current session of newWindow",
                f"write text {json.dumps(command)}",
                "end tell",
                "end tell",
            ]
        )
    else:
        script = "\n".join(
            [
                'tell application "Terminal"',
                "activate",
                f"do script {json.dumps(command)}",
                "end tell",
            ]
        )

    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        return f"Automatic terminal opening failed: {exc}"
    if result.returncode == 0:
        return None

    error = result.stderr.strip() or result.stdout.strip() or "Unknown osascript error"
    return f"Automatic terminal opening failed: {error}"


def _load_conflict_paths(conflict_artifact_path: str | None) -> list[str]:
    if not conflict_artifact_path:
        return []
    try:
        payload = json.loads(Path(conflict_artifact_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict):
        return []
    raw_paths = payload.get("paths")
    if not isinstance(raw_paths, list):
        return []
    return list(
        dict.fromkeys(
            str(item.get("path", "")).strip()
            for item in raw_paths
            if isinstance(item, dict) and str(item.get("path", "")).strip()
        )
    )


def _race_next_action(result: ConcurrentResult) -> str | None:  # noqa: F821
    if result.stop_reason == "manual_resolution_required":
        return f"agent-relay resolve {result.session_id}"
    if result.stop_reason == "merge_conflict":
        return f"agent-relay resolve {result.session_id}"
    if result.stop_reason in {"max_time", "interrupted", "incomplete", "agent_error"}:
        return f'agent-relay race --continue {result.session_id} <agents> "continue the task"'
    return None


def _default_resolution_task(status: str) -> str:
    normalized = status.strip().lower()
    if normalized == "manual_resolution_required":
        return "Resolve the remaining conflict and review the final merged result."
    return "Resolve the merge conflict and review the final merged result."


def _race_result_metadata(result: ConcurrentResult) -> dict[str, object]:  # noqa: F821
    conflict_paths = _load_conflict_paths(result.conflict_artifact_path)
    if not conflict_paths:
        conflict_paths = list(
            dict.fromkeys(path for outcome in result.outcomes for path in outcome.merge_conflicts)
        )
    scope_violation_paths = list(
        dict.fromkeys(path for outcome in result.outcomes for path in outcome.scope_violations)
    )
    metadata: dict[str, object] = {
        "conflict_paths": conflict_paths,
        "scope_violation_paths": scope_violation_paths,
    }
    next_action = _race_next_action(result)
    if next_action is not None:
        metadata["next_action"] = next_action
    return metadata


def cmd_inspect_conflicts(args: argparse.Namespace) -> int:
    from agent_relay.concurrent import load_conflict_artifact_summary

    repo_root = _resolve_repo(args.repo)
    summary = load_conflict_artifact_summary(repo_root, args.session_id)

    if args.json:
        emit_json(
            {
                "command": "inspect-conflicts",
                **summary,
            }
        )
    elif args.quiet:
        emit_quiet(str(summary.get("conflict_artifact_path", "")))
    else:
        render_conflict_inspect(args.console, summary)

    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    from agent_relay.concurrent import (
        infer_conflict_resolution_context,
        latest_unresolved_conflict_session_id,
        run_concurrent,
    )

    repo_root = _resolve_repo(args.repo)
    console = args.console
    interactive = not args.json and not args.quiet

    if getattr(args, "latest", False) and getattr(args, "session_id", None):
        raise SystemExit("Choose either a session id or --latest, not both.")

    target_session_id = getattr(args, "session_id", None)
    if getattr(args, "latest", False) or target_session_id is None:
        target_session_id = latest_unresolved_conflict_session_id(repo_root)
        if target_session_id is None:
            raise SystemExit("No unresolved conflict sessions found.")

    context = infer_conflict_resolution_context(repo_root, target_session_id)
    override_agents = getattr(args, "agent_overrides", None) or []
    agents = (
        [resolve_agent_key(agent) for agent in override_agents]
        if override_agents
        else list(context["agents"])
    )
    if len(agents) < 2:
        raise SystemExit("Conflict resolution requires at least 2 agents.")
    task = getattr(args, "task_flag", None) or _default_resolution_task(str(context["status"]))
    max_time = args.max_time
    auto_open_terminals = _should_auto_open_terminals(
        interactive=interactive,
        requested=getattr(args, "open_terminals", None),
    )

    if interactive:
        render_concurrent_start(
            console,
            agents,
            task,
            max_time,
            continue_session=target_session_id,
        )

    def on_agent_start(slot: int, agent_key: str, tmux_session: str) -> None:
        if interactive:
            from agent_relay.agents import get_agent_display_name

            name = get_agent_display_name(agent_key)
            console.print(
                f"  [brand]▸[/] Slot {slot}: [bold]{name}[/] started  [muted]({tmux_session})[/]",
                highlight=False,
            )
            console.print(
                f"      [muted]Open another terminal to control it:[/] tmux attach-session -t {tmux_session}",
                highlight=False,
            )
            if auto_open_terminals:
                error = _open_tmux_session_in_terminal(tmux_session)
                if error is not None:
                    console.print(f"      [warning]{error}[/]", highlight=False)

    def on_agent_done(outcome: AgentOutcome) -> None:  # noqa: F821
        if interactive:
            from agent_relay.agents import get_agent_display_name

            name = get_agent_display_name(outcome.agent_key)
            if outcome.exit_code == 0 and outcome.control_status == "done":
                status = "[success]done[/]"
            elif outcome.exit_code == 0:
                status = f"[warning]{outcome.control_status}[/]"
            else:
                status = f"[warning]exit {outcome.exit_code}[/]"
            phase_label = f"[muted]{outcome.phase}[/] " if outcome.phase != "implementation" else ""
            console.print(
                f"  [brand]▸[/] Slot {outcome.slot}: [bold]{name}[/] {phase_label}{status} — {outcome.summary}",
                highlight=False,
            )

    result = run_concurrent(
        repo_root,
        agents=agents,
        task=task,
        continue_from_session_id=target_session_id,
        max_time_seconds=max_time,
        owner="cli:resolve",
        on_agent_start=on_agent_start if interactive else None,
        on_agent_done=on_agent_done if interactive else None,
    )

    if args.json:
        emit_json(
            {
                "command": "resolve",
                "session_id": result.session_id,
                "source_session_id": target_session_id,
                "agents": list(result.agents),
                "tmux_sessions": list(result.tmux_sessions),
                "continued_from_session_id": result.continued_from_session_id,
                "claim_ledger_path": result.claim_ledger_path,
                "conflict_artifact_path": result.conflict_artifact_path,
                "stop_reason": result.stop_reason,
                "elapsed_seconds": result.elapsed_seconds,
                **_race_result_metadata(result),
                "outcomes": [
                    {
                        "slot": o.slot,
                        "agent": o.agent_key,
                        "tmux_session": o.tmux_session,
                        "phase": o.phase,
                        "worktree_path": o.worktree_path,
                        "exit_code": o.exit_code,
                        "summary": o.summary,
                        "done_signal": o.done_signal,
                        "completion_status": o.control_status,
                        "completion_reason": o.control_reason,
                        "claims": list(o.claims),
                        "claim_specs": [
                            {"path": claim.path, "role": claim.role} for claim in o.claim_specs
                        ],
                        "changed_paths": list(o.changed_paths),
                        "merged_paths": list(o.merged_paths),
                        "merge_conflicts": list(o.merge_conflicts),
                        "scope_violations": list(o.scope_violations),
                        "remaining_work": list(o.remaining_work),
                        "verification": list(o.verification),
                        "started_at": o.started_at,
                        "finished_at": o.finished_at,
                    }
                    for o in result.outcomes
                ],
            }
        )
    elif args.quiet:
        emit_quiet(result.session_id)
    else:
        render_concurrent_result(console, result)

    return 0


def cmd_relay(args: argparse.Namespace) -> int:
    repo_root = _resolve_repo(args.repo)
    to_agent = args.to
    from_agent = getattr(args, "from_agent", None)
    task = getattr(args, "task", None)
    planning_note = getattr(args, "planning_note", None)
    planning_note_file = getattr(args, "planning_note_file", None)
    proposed_edits = getattr(args, "proposed_edits", None)
    proposed_edits_file = getattr(args, "proposed_edits_file", None)
    no_launch = getattr(args, "no_launch", False)

    result = do_relay(
        repo_root,
        to_agent=to_agent,
        from_agent=from_agent,
        task=task,
        planning_note=planning_note,
        planning_note_file=planning_note_file,
        proposed_edits=proposed_edits,
        proposed_edits_file=proposed_edits_file,
        no_launch=no_launch,
        owner="cli:relay",
    )

    if args.json:
        emit_json(
            {
                "command": "relay",
                "session_id": result.session_id,
                "from_agent": result.from_agent,
                "to_agent": result.to_agent,
                "handoff_id": result.handoff_id,
                "resume_path": result.resume_path,
                "launch_command": result.launch_command,
                "created_session": result.created_session,
            }
        )
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

        if (
            not args.json
            and not args.quiet
            and not getattr(args, "yes", False)
            and sys.stdin.isatty()
        ):
            from rich.prompt import Confirm

            if not Confirm.ask(
                "\n  [brand]Launch target agent?[/]", console=args.console, default=True
            ):
                args.console.print(
                    "  [muted]Launch skipped. Run manually with the command above.[/]"
                )
                return 0

        if not args.json and not args.quiet:
            args.console.print("\n  [brand]∴ Launching target agent...[/]\n")
            launch_result = execute_launch_for_command(
                repo_root,
                result.session_id,
                handoff_id=result.handoff_id,
                owner="cli:relay:launch",
            )
            render_relay_launch_result(
                args.console, launch_result.exit_code == 0, launch_result.exit_code
            )
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
        emit_json(
            {
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
            }
        )
    elif args.quiet:
        for s in sessions:
            emit_quiet(str(s["session_id"]))
    else:
        render_dashboard(args.console, sessions)
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    """Live view of an in-progress session.

    With no session id, picks the most recently updated active session.
    Default mode: full Rich TUI. ``--json`` streams one JSON event per line.
    ``--quiet`` streams one terse line per event.
    """
    repo_root = _resolve_repo(args.repo)

    session_id = args.session_id
    fallback_notice: str | None = None
    if session_id is None:
        session_id = pick_latest_active_session(repo_root)
        if session_id is None:
            # Fall back to the newest session of any status so the user gets
            # *something* useful instead of a hard error. Surface the fact
            # that nothing is currently active so the experience isn't
            # silently misleading.
            latest = pick_latest_session(repo_root)
            if latest is None:
                render_error(
                    args.console,
                    "No relay sessions found in this repo. Start one with "
                    "`agent-relay run/chat/race`.",
                )
                return 2
            session_id = latest["session_id"]
            fallback_notice = (
                f"No active session — falling back to the most recent session "
                f"({session_id}, status: {latest.get('current_status') or 'unknown'}). "
                f"Use `agent-relay watch <session-id>` to pin a different one."
            )

    if not is_session(repo_root, session_id):
        render_error(args.console, f"Session not found: {session_id}")
        return 2

    try:
        source = WatchSource(
            repo_root,
            session_id,
            poll_interval=args.poll_interval,
            follow=not args.no_follow,
        )
    except ValueError as exc:
        render_error(args.console, str(exc))
        return 2

    if args.json:
        return stream_json_events(source)
    if args.quiet:
        if fallback_notice:
            sys.stderr.write(fallback_notice + "\n")
        return stream_quiet_lines(source)
    if fallback_notice:
        args.console.print(f"[warning]{fallback_notice}[/]")
    return render_watch_live(args.console, source, show_metrics=getattr(args, "metrics", False))


def cmd_metrics(args: argparse.Namespace) -> int:
    """Token / cost / latency metrics for one or all sessions.

    With a session id (or no args), shows that session's rollup. With
    ``--all``, aggregates across every session in the repo. ``--json`` and
    ``--quiet`` are honored as on every other command.
    """
    repo_root = _resolve_repo(args.repo)

    since_dt = None
    if getattr(args, "since", None):
        try:
            since_dt = _parse_since(args.since)
        except ValueError as exc:
            render_error(args.console, str(exc))
            return 2

    agents_filter = getattr(args, "agent", None) or None

    if args.all:
        cross = extract_cross_session_metrics(repo_root, since=since_dt, agents=agents_filter)
        if args.json:
            emit_cross_session_metrics_json(cross)
        elif args.quiet:
            emit_cross_session_metrics_quiet(cross)
        else:
            render_cross_session_metrics(args.console, cross)
        return 0

    session_id = args.session_id
    if session_id is None:
        # Auto-pick the newest session of any status — metrics are most
        # useful right after a run finishes.
        latest = pick_latest_session(repo_root)
        if latest is None:
            render_error(
                args.console,
                "No relay sessions found in this repo. Start one with `agent-relay run/chat/race`.",
            )
            return 2
        session_id = latest["session_id"]

    if not is_session(repo_root, session_id):
        render_error(args.console, f"Session not found: {session_id}")
        return 2

    metrics = extract_session_metrics(repo_root, session_id)
    if args.json:
        emit_session_metrics_json(metrics)
    elif args.quiet:
        emit_session_metrics_quiet(metrics)
    else:
        render_session_metrics(args.console, metrics)
    return 0


def cmd_metrics_serve(args: argparse.Namespace) -> int:
    """Run a metrics exporter (Prometheus and/or OTLP)."""
    repo_root = _resolve_repo(args.repo)

    if not args.prometheus and not args.otlp:
        render_error(
            args.console,
            "metrics-serve requires --prometheus HOST:PORT and/or --otlp URL",
        )
        return 2

    try:
        otlp_headers = parse_header_pairs(args.otlp_header)
    except ValueError as exc:
        render_error(args.console, str(exc))
        return 2

    host: str | None = None
    port: int = 0
    if args.prometheus:
        host, port = _parse_host_port(args.prometheus)
        if host is None:
            render_error(
                args.console,
                f"--prometheus must be HOST:PORT or :PORT (got: {args.prometheus!r})",
            )
            return 2

    # Run OTLP in a background thread when requested. Prometheus blocks
    # the main thread; if no Prometheus, OTLP blocks the main thread.
    import threading as _threading

    otlp_stop = _threading.Event()
    otlp_thread: _threading.Thread | None = None
    if args.otlp:
        sys.stderr.write(f"OTLP exporter pushing to {args.otlp} every {args.otlp_interval:.1f}s\n")
        otlp_thread = _threading.Thread(
            target=serve_otlp,
            kwargs={
                "repo_root": repo_root,
                "endpoint": args.otlp,
                "interval_seconds": args.otlp_interval,
                "headers": otlp_headers or None,
                "stop_event": otlp_stop,
            },
            daemon=True,
        )
        otlp_thread.start()

    try:
        if args.prometheus:
            assert host is not None
            sys.stderr.write(
                f"Prometheus exporter listening on http://{host}:{port}/metrics "
                f"(refresh: {args.prometheus_refresh:.1f}s)\n"
            )
            try:
                rc = serve_prometheus(
                    repo_root,
                    host,
                    port,
                    refresh_interval=args.prometheus_refresh,
                )
            except OSError as exc:
                render_error(args.console, f"failed to bind {host}:{port}: {exc}")
                return 2
        else:
            # OTLP-only — block on the OTLP thread.
            rc = 0
            try:
                while otlp_thread and otlp_thread.is_alive():
                    otlp_thread.join(timeout=0.5)
            except KeyboardInterrupt:
                rc = 130
    finally:
        otlp_stop.set()
        if otlp_thread is not None:
            otlp_thread.join(timeout=2.0)

    return rc


def _parse_host_port(value: str) -> tuple[str | None, int]:
    """Parse 'host:port' or ':port' into (host, port). Returns (None, 0) on failure."""
    if not value:
        return None, 0
    text = value.strip()
    if text.startswith(":"):
        host = "127.0.0.1"
        port_str = text[1:]
    elif ":" in text:
        host, port_str = text.rsplit(":", 1)
    else:
        return None, 0
    try:
        port = int(port_str)
    except ValueError:
        return None, 0
    if not (0 < port < 65536):
        return None, 0
    return host, port


def cmd_metrics_tail(args: argparse.Namespace) -> int:
    """Stream metric events as JSONL — one line per turn_completed plus a
    final session rollup. Optional webhook delivery via ``--webhook URL``.
    """
    repo_root = _resolve_repo(args.repo)

    session_id = args.session_id
    fallback_notice: str | None = None
    if session_id is None:
        session_id = pick_latest_active_session(repo_root)
        if session_id is None:
            latest = pick_latest_session(repo_root)
            if latest is None:
                render_error(
                    args.console,
                    "No relay sessions found in this repo. Start one with "
                    "`agent-relay run/chat/race`.",
                )
                return 2
            session_id = latest["session_id"]
            fallback_notice = (
                f"No active session — falling back to {session_id} "
                f"(status: {latest.get('current_status') or 'unknown'})."
            )

    if not is_session(repo_root, session_id):
        render_error(args.console, f"Session not found: {session_id}")
        return 2

    try:
        webhook_headers = parse_header_pairs(args.webhook_header)
    except ValueError as exc:
        render_error(args.console, str(exc))
        return 2

    try:
        source = WatchSource(
            repo_root,
            session_id,
            poll_interval=args.poll_interval,
            follow=not args.no_follow,
        )
    except ValueError as exc:
        render_error(args.console, str(exc))
        return 2

    if fallback_notice:
        sys.stderr.write(fallback_notice + "\n")

    return tail_jsonl(
        source,
        webhook_url=args.webhook,
        webhook_headers=webhook_headers,
        webhook_timeout=args.webhook_timeout,
    )


def _parse_since(value: str):
    """Parse `--since` (YYYY-MM-DD or ISO-8601). Raises ValueError on bad input."""
    from datetime import datetime

    text = value.strip()
    try:
        if len(text) == 10:
            dt = datetime.strptime(text, "%Y-%m-%d")
            return dt.replace(tzinfo=UTC)
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"--since must be YYYY-MM-DD or ISO-8601 (got: {value!r})") from exc


def cmd_clean(args: argparse.Namespace) -> int:
    import shutil

    from agent_relay.layout import relay_root, sessions_root

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
        emit_json(
            {
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
            }
        )
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


def _parse_run_task(args: argparse.Namespace, repo_root: Path) -> str:
    positional_task: str | None = getattr(args, "task", None)
    task_flag: str | None = getattr(args, "task_flag", None)
    continue_session: str | None = getattr(args, "continue_session", None)

    if positional_task and task_flag:
        raise SystemExit("Choose either a positional task or --task, not both.")

    if task_flag:
        return task_flag
    if positional_task:
        return positional_task
    if continue_session:
        from agent_relay.storage import load_session_view

        return load_session_view(repo_root, continue_session).objective

    raise SystemExit(
        "Missing task. Provide a task positionally or with -t:\n"
        '  agent-relay run claude "fix the tests"\n'
        '  agent-relay run claude -t "fix the tests"'
    )


def cmd_run(args: argparse.Namespace) -> int:
    from agent_relay.run_session import run_session as do_run

    repo_root = _resolve_repo(args.repo)
    console = args.console
    interactive = not args.json and not args.quiet
    agent = resolve_agent_key(args.agent)
    task = _parse_run_task(args, repo_root)

    if interactive:
        render_converse_start(console, [agent], task, args.max_turns)

    _spinner_ctx = None

    def on_turn_start(agent_key: str, turn_number: int, max_turns: int) -> None:
        nonlocal _spinner_ctx
        if interactive:
            _spinner_ctx = render_converse_turn_active(console, agent_key, turn_number, max_turns)
            _spinner_ctx.__enter__()

    def on_turn_complete(turn: TurnResult) -> None:  # noqa: F821
        nonlocal _spinner_ctx
        if _spinner_ctx is not None:
            _spinner_ctx.__exit__(None, None, None)
            _spinner_ctx = None
        if interactive:
            render_converse_turn_done(
                console,
                turn.turn_number,
                turn.agent_key,
                turn.summary,
                turn.exit_code,
                turn.text,
            )

    result = do_run(
        repo_root,
        agent=agent,
        task=task,
        max_turns=args.max_turns,
        continue_from_session_id=getattr(args, "continue_session", None),
        owner="cli:run",
        on_turn_start=on_turn_start if interactive else None,
        on_turn_complete=on_turn_complete if interactive else None,
    )

    if _spinner_ctx is not None:
        _spinner_ctx.__exit__(None, None, None)

    if args.json:
        emit_json(
            {
                "command": "run",
                "session_id": result.session_id,
                "agent": agent,
                "continued_from_session_id": result.continued_from_session_id,
                "turns_completed": result.turns_completed,
                "stop_reason": result.stop_reason,
                "turns": [
                    {
                        "turn": t.turn_number,
                        "agent": t.agent_key,
                        "exit_code": t.exit_code,
                        "summary": t.summary,
                        "done_signal": t.done_signal,
                        "completion_status": t.control_status,
                        "completion_reason": t.control_reason,
                        "remaining_work": list(t.remaining_work),
                        "verification": list(t.verification),
                        "started_at": t.started_at,
                        "finished_at": t.finished_at,
                    }
                    for t in result.turn_results
                ],
            }
        )
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

    def on_turn_complete(turn: TurnResult) -> None:  # noqa: F821
        nonlocal _spinner_ctx
        if _spinner_ctx is not None:
            _spinner_ctx.__exit__(None, None, None)
            _spinner_ctx = None
        if interactive:
            render_converse_turn_done(
                console,
                turn.turn_number,
                turn.agent_key,
                turn.summary,
                turn.exit_code,
                turn.text,
            )

    result = do_converse(
        repo_root,
        agents=agents,
        task=task,
        max_turns=args.max_turns,
        continue_from_session_id=getattr(args, "continue_session", None),
        owner="cli:chat",
        on_turn_start=on_turn_start if interactive else None,
        on_turn_complete=on_turn_complete if interactive else None,
    )

    if _spinner_ctx is not None:
        _spinner_ctx.__exit__(None, None, None)

    if args.json:
        emit_json(
            {
                "command": "chat",
                "session_id": result.session_id,
                "agents": list(result.agents),
                "continued_from_session_id": result.continued_from_session_id,
                "turns_completed": result.turns_completed,
                "stop_reason": result.stop_reason,
                "turns": [
                    {
                        "turn": t.turn_number,
                        "agent": t.agent_key,
                        "exit_code": t.exit_code,
                        "summary": t.summary,
                        "done_signal": t.done_signal,
                        "completion_status": t.control_status,
                        "completion_reason": t.control_reason,
                        "remaining_work": list(t.remaining_work),
                        "verification": list(t.verification),
                        "started_at": t.started_at,
                        "finished_at": t.finished_at,
                    }
                    for t in result.turn_results
                ],
            }
        )
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
    auto_open_terminals = _should_auto_open_terminals(
        interactive=interactive,
        requested=getattr(args, "open_terminals", None),
    )
    agents, task = _parse_agents_and_task(args)
    max_time = args.max_time

    if interactive:
        render_concurrent_start(
            console,
            agents,
            task,
            max_time,
            continue_session=getattr(args, "continue_session", None),
        )

    def on_agent_start(slot: int, agent_key: str, tmux_session: str) -> None:
        if interactive:
            from agent_relay.agents import get_agent_display_name

            name = get_agent_display_name(agent_key)
            console.print(
                f"  [brand]▸[/] Slot {slot}: [bold]{name}[/] started  [muted]({tmux_session})[/]",
                highlight=False,
            )
            console.print(
                f"      [muted]Open another terminal to control it:[/] tmux attach-session -t {tmux_session}",
                highlight=False,
            )
            if auto_open_terminals:
                error = _open_tmux_session_in_terminal(tmux_session)
                if error is not None:
                    console.print(f"      [warning]{error}[/]", highlight=False)

    def on_agent_done(outcome: AgentOutcome) -> None:  # noqa: F821
        if interactive:
            from agent_relay.agents import get_agent_display_name

            name = get_agent_display_name(outcome.agent_key)
            if outcome.exit_code == 0 and outcome.control_status == "done":
                status = "[success]done[/]"
            elif outcome.exit_code == 0:
                status = f"[warning]{outcome.control_status}[/]"
            else:
                status = f"[warning]exit {outcome.exit_code}[/]"
            phase_label = f"[muted]{outcome.phase}[/] " if outcome.phase != "implementation" else ""
            console.print(
                f"  [brand]▸[/] Slot {outcome.slot}: [bold]{name}[/] {phase_label}{status} — {outcome.summary}",
                highlight=False,
            )

    result = run_concurrent(
        repo_root,
        agents=agents,
        task=task,
        continue_from_session_id=getattr(args, "continue_session", None),
        max_time_seconds=max_time,
        owner="cli:race",
        on_agent_start=on_agent_start if interactive else None,
        on_agent_done=on_agent_done if interactive else None,
    )

    if args.json:
        emit_json(
            {
                "command": "race",
                "session_id": result.session_id,
                "agents": list(result.agents),
                "tmux_sessions": list(result.tmux_sessions),
                "continued_from_session_id": result.continued_from_session_id,
                "claim_ledger_path": result.claim_ledger_path,
                "conflict_artifact_path": result.conflict_artifact_path,
                "stop_reason": result.stop_reason,
                "elapsed_seconds": result.elapsed_seconds,
                **_race_result_metadata(result),
                "outcomes": [
                    {
                        "slot": o.slot,
                        "agent": o.agent_key,
                        "tmux_session": o.tmux_session,
                        "phase": o.phase,
                        "worktree_path": o.worktree_path,
                        "exit_code": o.exit_code,
                        "summary": o.summary,
                        "done_signal": o.done_signal,
                        "completion_status": o.control_status,
                        "completion_reason": o.control_reason,
                        "claims": list(o.claims),
                        "claim_specs": [
                            {"path": claim.path, "role": claim.role} for claim in o.claim_specs
                        ],
                        "changed_paths": list(o.changed_paths),
                        "merged_paths": list(o.merged_paths),
                        "merge_conflicts": list(o.merge_conflicts),
                        "scope_violations": list(o.scope_violations),
                        "remaining_work": list(o.remaining_work),
                        "verification": list(o.verification),
                        "started_at": o.started_at,
                        "finished_at": o.finished_at,
                    }
                    for o in result.outcomes
                ],
            }
        )
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
        agent_cmd = subparsers.add_parser(
            agent_key, help=f"Relay to {AGENT_REGISTRY[agent_key].display_name}"
        )
        agent_cmd.add_argument(
            "--from",
            dest="from_agent",
            choices=AGENT_NAMES,
            help="Source agent (auto-detected)",
        )
        agent_cmd.add_argument("--task", "-t", help="What the next agent should do")
        agent_cmd.add_argument(
            "--planning-note",
            help="Planning snapshot to preserve even when no code changed",
        )
        agent_cmd.add_argument("--planning-note-file", help="Path to a planning snapshot file")
        agent_cmd.add_argument(
            "--proposed-edits",
            help="UI-only or not-yet-applied proposed edits to preserve",
        )
        agent_cmd.add_argument(
            "--proposed-edits-file", help="Path to a proposed edits file or diff"
        )
        agent_cmd.add_argument("--no-launch", action="store_true", help="Just create the packet")
        agent_cmd.add_argument("--yes", "-y", action="store_true", help="Skip confirmation")
        agent_cmd.add_argument("--repo", help="Repository path (default: cwd)")
        agent_cmd.set_defaults(func=cmd_relay, to=agent_key)

    # agent-relay status — view sessions
    status = subparsers.add_parser("status", help="Show relay sessions")
    status.add_argument("--repo")
    status.set_defaults(func=cmd_status)

    # agent-relay watch — live view of an in-progress session
    watch = subparsers.add_parser(
        "watch",
        help="Live view of an in-progress session (auto-picks newest active)",
    )
    watch.add_argument(
        "session_id",
        nargs="?",
        default=None,
        help="Session id (omit to auto-pick the newest active session)",
    )
    watch.add_argument(
        "--no-follow",
        action="store_true",
        help="Print one snapshot pass and exit instead of following",
    )
    watch.add_argument(
        "--poll-interval",
        type=float,
        default=0.25,
        help="Seconds between polls (default: 0.25)",
    )
    watch.add_argument(
        "--metrics",
        action="store_true",
        help="Show a token / cost / duration panel that refreshes per turn",
    )
    watch.add_argument("--repo")
    watch.set_defaults(func=cmd_watch)

    # agent-relay metrics — cost / token / latency rollups
    metrics = subparsers.add_parser(
        "metrics",
        help="Token / cost / latency metrics for sessions",
    )
    metrics.add_argument(
        "session_id",
        nargs="?",
        default=None,
        help="Session id (omit to auto-pick the most recent session)",
    )
    metrics.add_argument(
        "--all",
        action="store_true",
        help="Aggregate metrics across every session in the repo",
    )
    metrics.add_argument(
        "--since",
        help="Lower bound on session start (YYYY-MM-DD or ISO-8601)",
    )
    metrics.add_argument(
        "--agent",
        action="append",
        help="Filter to one or more agent keys (repeatable)",
    )
    metrics.add_argument("--repo")
    metrics.set_defaults(func=cmd_metrics)

    # agent-relay metrics-tail — JSONL stream of metric events
    metrics_tail = subparsers.add_parser(
        "metrics-tail",
        help="Stream metric events as JSONL (one line per turn) with optional webhook",
    )
    metrics_tail.add_argument(
        "session_id",
        nargs="?",
        default=None,
        help="Session id (omit to auto-pick the newest active session)",
    )
    metrics_tail.add_argument(
        "--no-follow",
        action="store_true",
        help="Emit a single rollup line and exit instead of following",
    )
    metrics_tail.add_argument(
        "--poll-interval",
        type=float,
        default=0.25,
        help="Seconds between polls (default: 0.25)",
    )
    metrics_tail.add_argument(
        "--webhook",
        help="POST each JSONL line to this URL (Content-Type: application/json)",
    )
    metrics_tail.add_argument(
        "--webhook-header",
        action="append",
        default=[],
        help="Header for webhook requests (repeatable, 'Key: Value' or 'Key=Value')",
    )
    metrics_tail.add_argument(
        "--webhook-timeout",
        type=float,
        default=5.0,
        help="Seconds to wait for webhook response (default: 5.0)",
    )
    metrics_tail.add_argument("--repo")
    metrics_tail.set_defaults(func=cmd_metrics_tail)

    # agent-relay metrics-serve — Prometheus / OTLP exporters
    metrics_serve = subparsers.add_parser(
        "metrics-serve",
        help="Run a metrics exporter (Prometheus scrape, OTLP push)",
    )
    metrics_serve.add_argument(
        "--prometheus",
        metavar="HOST:PORT",
        help="Listen address for Prometheus /metrics (e.g. ':9464' or '0.0.0.0:9464')",
    )
    metrics_serve.add_argument(
        "--prometheus-refresh",
        type=float,
        default=5.0,
        help="Seconds to cache scraped metrics (default: 5.0)",
    )
    metrics_serve.add_argument(
        "--otlp",
        metavar="URL",
        help="OTLP HTTP/JSON metrics endpoint (e.g. http://localhost:4318/v1/metrics)",
    )
    metrics_serve.add_argument(
        "--otlp-header",
        action="append",
        default=[],
        help="Header for OTLP requests (repeatable, 'Key: Value' or 'Key=Value')",
    )
    metrics_serve.add_argument(
        "--otlp-interval",
        type=float,
        default=30.0,
        help="Seconds between OTLP pushes (default: 30.0)",
    )
    metrics_serve.add_argument("--repo")
    metrics_serve.set_defaults(func=cmd_metrics_serve)

    # agent-relay clean — remove sessions
    clean = subparsers.add_parser("clean", help="Remove all relay sessions")
    clean.add_argument(
        "--all", action="store_true", help="Remove the entire .agent-relay directory"
    )
    clean.add_argument("--repo")
    clean.set_defaults(func=cmd_clean)

    # agent-relay discover — detect available agent CLIs
    disc = subparsers.add_parser("discover", help="Detect available agent CLIs")
    disc.set_defaults(func=cmd_discover)

    # agent-relay run <agent> <task> — single-agent managed session
    run = subparsers.add_parser("run", help="Run a single agent in a Relay-managed session")
    run.add_argument("agent", help="Agent key or alias")
    run.add_argument("task", nargs="?", help="Task (omit when using -t or --continue)")
    run.add_argument(
        "--task",
        "-t",
        dest="task_flag",
        default=None,
        help="Task (alternative to positional)",
    )
    run.add_argument(
        "--continue",
        dest="continue_session",
        help="Continue from a prior relay session id",
    )
    run.add_argument("--max-turns", "-n", type=int, default=10, help="Maximum turns (default: 10)")
    run.add_argument("--yes", "-y", action="store_true", help="Skip confirmation")
    run.add_argument("--repo", help="Repository path (default: cwd)")
    run.set_defaults(func=cmd_run)

    # agent-relay chat <agent> [<agent>...] <task> — turn-based conversation
    chat = subparsers.add_parser("chat", help="Turn-based agent-to-agent conversation")
    chat.add_argument(
        "args",
        nargs="+",
        metavar="AGENT_OR_TASK",
        help="Agents and task (last arg is task, or use -t)",
    )
    chat.add_argument(
        "--task",
        "-t",
        dest="task_flag",
        default=None,
        help="Task (alternative to positional)",
    )
    chat.add_argument(
        "--continue",
        dest="continue_session",
        help="Continue from a prior relay session id",
    )
    chat.add_argument("--max-turns", "-n", type=int, default=10, help="Maximum turns (default: 10)")
    chat.add_argument("--yes", "-y", action="store_true", help="Skip confirmation")
    chat.add_argument("--repo", help="Repository path (default: cwd)")
    chat.set_defaults(func=cmd_chat)

    # agent-relay race <agent> [<agent>...] <task> — concurrent agents with tmux
    race = subparsers.add_parser(
        "race", help="Concurrent workflow with planning, worktrees, and conflict recovery"
    )
    race.add_argument(
        "args",
        nargs="+",
        metavar="AGENT_OR_TASK",
        help="Agents and task (last arg is task, or use -t)",
    )
    race.add_argument(
        "--task",
        "-t",
        dest="task_flag",
        default=None,
        help="Task (alternative to positional)",
    )
    race.add_argument(
        "--continue",
        dest="continue_session",
        help="Continue from a prior race session id",
    )
    race.add_argument("--max-time", type=int, default=600, help="Max seconds (default: 600)")
    race.add_argument(
        "--open-terminals",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Auto-open a terminal window/tab for each tmux session on supported platforms",
    )
    race.add_argument("--yes", "-y", action="store_true", help="Skip confirmation")
    race.add_argument("--repo", help="Repository path (default: cwd)")
    race.set_defaults(func=cmd_race)

    resolve = subparsers.add_parser(
        "resolve", help="Resume unresolved race conflicts from saved artifacts"
    )
    resolve.add_argument(
        "session_id",
        nargs="?",
        help="Relay session id (defaults to latest unresolved conflict)",
    )
    resolve.add_argument(
        "--latest",
        action="store_true",
        help="Resolve the latest unresolved conflict session",
    )
    resolve.add_argument(
        "--agent",
        dest="agent_overrides",
        action="append",
        help="Override the inferred resolver agents",
    )
    resolve.add_argument(
        "--task", "-t", dest="task_flag", default=None, help="Resolution task override"
    )
    resolve.add_argument("--max-time", type=int, default=600, help="Max seconds (default: 600)")
    resolve.add_argument(
        "--open-terminals",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Auto-open a terminal window/tab for each tmux session on supported platforms",
    )
    resolve.add_argument("--yes", "-y", action="store_true", help="Skip confirmation")
    resolve.add_argument("--repo", help="Repository path (default: cwd)")
    resolve.set_defaults(func=cmd_resolve)

    inspect_conflicts = subparsers.add_parser(
        "inspect-conflicts", help="Inspect saved race conflict artifacts and versions"
    )
    inspect_conflicts.add_argument("session_id", help="Relay session id")
    inspect_conflicts.add_argument("--repo", help="Repository path (default: cwd)")
    inspect_conflicts.set_defaults(func=cmd_inspect_conflicts)

    return parser


def iter_commands(parser: argparse.ArgumentParser) -> list[tuple[str, str]]:
    """Return ``(usage, help_text)`` for every subcommand in declaration order.

    Source of truth for `agent-relay --help` so a new subparser cannot drift
    out of the help output. Agent-named subparsers (`claude`, `codex`,
    `gemini`) collapse into a single ``agent-relay <agent>`` row to keep the
    list terse — their per-agent flags live on each subparser's own --help.
    """
    subparsers_action = next(
        (a for a in parser._actions if isinstance(a, argparse._SubParsersAction)),
        None,
    )
    if subparsers_action is None:
        return []

    help_by_name = {
        action.dest: (action.help or "") for action in subparsers_action._choices_actions
    }

    rows: list[tuple[str, str]] = []
    seen_agents = False
    for name in subparsers_action.choices:
        if name in AGENT_NAMES:
            if seen_agents:
                continue
            seen_agents = True
            rows.append(
                (
                    "agent-relay <agent>",
                    "Relay to a target agent (claude, codex, gemini)",
                )
            )
            continue
        rows.append((f"agent-relay {name}", help_by_name.get(name, "")))
    return rows


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    args.console = create_console(json_mode=args.json, quiet=args.quiet)

    if args.help or args.command is None:
        render_help(args.console, commands=iter_commands(parser))
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
