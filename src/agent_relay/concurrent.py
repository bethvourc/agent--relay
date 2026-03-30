"""Concurrent agent execution orchestrator (tmux-backed).

Runs multiple agents simultaneously in separate tmux sessions, giving them live
visibility into each other's work through relay-managed snapshot files and a
shared workspace log.
"""
from __future__ import annotations

import json
import secrets
import shlex
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from agent_relay.agents import (
    get_agent_adapter,
    get_agent_display_name,
    require_available,
)
from agent_relay.bootstrap import start_session
from agent_relay.fs import write_text_atomic
from agent_relay.layout import (
    concurrent_agent_dir,
    concurrent_dir,
    workspace_log_path,
)
from agent_relay.workspace_log import LogEntry, WorkspaceLog, utc_timestamp


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class AgentOutcome:
    slot: int
    agent_key: str
    tmux_session: str
    exit_code: int | None   # None if still running / killed
    raw_stdout: str
    raw_stderr: str
    text: str
    summary: str
    done_signal: bool
    started_at: str
    finished_at: str
    control_status: str = "continue"
    control_reason: str = ""
    remaining_work: tuple[str, ...] = field(default_factory=tuple)
    verification: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class ConcurrentResult:
    session_id: str
    agents: tuple[str, ...]
    tmux_sessions: tuple[str, ...]
    stop_reason: str   # "all_done" | "incomplete" | "max_time" | "agent_error" | "interrupted"
    elapsed_seconds: float
    outcomes: tuple[AgentOutcome, ...]


@dataclass(frozen=True, slots=True)
class ConcurrentControl:
    status: str = "continue"
    reason: str = ""
    remaining_work: tuple[str, ...] = field(default_factory=tuple)
    verification: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# tmux helpers
# ---------------------------------------------------------------------------

_DONE_MARKER = "CONVERSATION_COMPLETE"
_STATUS_PREFIX = "RELAY_STATUS:"
_VALID_CONTROL_STATUSES = frozenset({
    "continue",
    "blocked",
    "done",
    "error",
})

def _require_tmux() -> str:
    """Return tmux path or raise SystemExit."""
    path = shutil.which("tmux")
    if not path:
        raise SystemExit(
            "tmux is required for concurrent mode (race).\n"
            "Install it: brew install tmux (macOS) or apt install tmux (Linux)"
        )
    return path


def _tmux(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", *args],
        capture_output=True, text=True, check=check,
    )


def _tmux_session_exists(session_name: str) -> bool:
    result = _tmux("has-session", "-t", session_name, check=False)
    return result.returncode == 0


def _tmux_capture_pane(session_name: str, pane_index: int) -> str:
    """Capture the visible content of a tmux pane."""
    result = _tmux(
        "capture-pane", "-t", f"{session_name}:{0}.{pane_index}",
        "-p",  # print to stdout
        check=False,
    )
    return result.stdout if result.returncode == 0 else ""


def _tmux_pane_pid(session_name: str, pane_index: int) -> int | None:
    """Get the PID of the process running in a pane."""
    result = _tmux(
        "display-message", "-t", f"{session_name}:{0}.{pane_index}",
        "-p", "#{pane_pid}",
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip().isdigit():
        return int(result.stdout.strip())
    return None


def _tmux_pane_dead(session_name: str, pane_index: int) -> bool:
    """Check if the pane's process has exited."""
    result = _tmux(
        "display-message", "-t", f"{session_name}:{0}.{pane_index}",
        "-p", "#{pane_dead}",
        check=False,
    )
    return result.stdout.strip() == "1"


def _tmux_session_name(session_id: str, slot: int) -> str:
    return f"relay-{session_id}-{slot:02d}"


def _coerce_string_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        stripped = value.strip()
        return (stripped,) if stripped else ()
    if isinstance(value, (list, tuple)):
        items: list[str] = []
        for item in value:
            if isinstance(item, str):
                stripped = item.strip()
                if stripped:
                    items.append(stripped)
        return tuple(items)
    return ()


def _has_legacy_done_line(text: str) -> bool:
    return any(line.strip().upper() == _DONE_MARKER for line in text.splitlines())


def _strip_concurrent_control(text: str) -> str:
    kept_lines = [
        line
        for line in text.splitlines()
        if not line.strip().startswith(_STATUS_PREFIX)
        and line.strip().upper() != _DONE_MARKER
    ]
    return "\n".join(kept_lines).strip()


def parse_concurrent_control(text: str) -> ConcurrentControl:
    """Parse the last machine-readable RELAY_STATUS line from pane content."""
    for raw_line in reversed(text.splitlines()):
        line = raw_line.strip()
        if not line.startswith(_STATUS_PREFIX):
            continue

        payload = line[len(_STATUS_PREFIX):].strip()
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue

        status = str(data.get("status", "continue")).strip().lower()
        if status not in _VALID_CONTROL_STATUSES:
            continue

        return ConcurrentControl(
            status=status,
            reason=str(data.get("reason", "")).strip(),
            remaining_work=_coerce_string_tuple(data.get("remaining_work")),
            verification=_coerce_string_tuple(data.get("verification")),
        )

    if _has_legacy_done_line(text):
        return ConcurrentControl(
            status="done",
            reason="Legacy CONVERSATION_COMPLETE marker",
        )

    return ConcurrentControl()


def _make_summary(text: str, *, exit_code: int | None) -> str:
    for line in _strip_concurrent_control(text).splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:117] + "..." if len(stripped) > 120 else stripped
    if exit_code not in (None, 0):
        return f"(exited with code {exit_code})"
    if exit_code is None:
        return "(still running)"
    return "(no output)"


def _read_exit_code(path: Path) -> int | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError:
        return None
    return int(text) if text and text.lstrip("-").isdigit() else None


def _build_outcome(
    *,
    slot: int,
    agent_key: str,
    tmux_session: str,
    pane_content: str,
    exit_code: int | None,
    started_at: str,
    finished_at: str,
) -> AgentOutcome:
    control = parse_concurrent_control(pane_content)
    display_text = _strip_concurrent_control(pane_content)
    return AgentOutcome(
        slot=slot,
        agent_key=agent_key,
        tmux_session=tmux_session,
        exit_code=exit_code,
        raw_stdout=pane_content,
        raw_stderr="",
        text=display_text,
        summary=_make_summary(pane_content, exit_code=exit_code),
        done_signal=control.status == "done",
        started_at=started_at,
        finished_at=finished_at,
        control_status=control.status,
        control_reason=control.reason,
        remaining_work=control.remaining_work,
        verification=control.verification,
    )


def _classify_stop_reason(
    current_stop_reason: str,
    outcomes: Sequence[AgentOutcome],
) -> str:
    if current_stop_reason in {"max_time", "interrupted", "agent_error"}:
        return current_stop_reason
    if any(outcome.exit_code is None for outcome in outcomes):
        return "agent_error"
    if any(
        outcome.exit_code != 0 or outcome.control_status == "error"
        for outcome in outcomes
    ):
        return "agent_error"
    if all(
        outcome.exit_code == 0 and outcome.control_status == "done"
        for outcome in outcomes
    ):
        return "all_done"
    return "incomplete"


# ---------------------------------------------------------------------------
# Concurrent prompt builder
# ---------------------------------------------------------------------------

_CONCURRENT_PREAMBLE = """\
You are participating in a CONCURRENT multi-agent session.
You are {current_agent_name} ({current_agent_key}), running in slot {slot_index}.

{participants_section}

Your shared workspace is: {repo_root}

## Concurrent Mode Rules
- You are running AT THE SAME TIME as the other agents — not taking turns.
- The relay writes local pane snapshot files for you. Read those files instead of invoking tmux commands yourself.
  {pane_snapshot_instructions}
- A shared activity log is at: {workspace_log}
- There is no interactive approval loop in concurrent mode. Do not wait for the user to approve commands.
- Before editing a file, check its current state — another agent may have changed it.
- Coordinate: decide who handles what. Don't duplicate work.
- End with a machine-readable status line:
  RELAY_STATUS: {{"status":"continue","reason":"...","remaining_work":["..."],"verification":[]}}
- Allowed statuses: continue, blocked, done, error
- Use done only when your part is truly complete and remaining_work is [].
- Use error if you hit a terminal failure you could not resolve.
- If a command is blocked or denied, adapt your approach and report that in RELAY_STATUS instead of asking for approval.
- If you post multiple RELAY_STATUS lines during the session, the last one wins.

## Task

{task}
"""


def _build_concurrent_prompt(
    task: str,
    slot: int,
    agent_key: str,
    all_agents: Sequence[str],
    repo_root: Path,
    workspace_log: Path,
    pane_snapshot_paths: Sequence[Path],
) -> str:
    agent_name = get_agent_display_name(agent_key)
    others = [(i, a) for i, a in enumerate(all_agents) if i != slot]
    unique_others = list(dict.fromkeys(a for _, a in others))

    if unique_others:
        lines = ["Other agents running concurrently:"]
        for i, a in others:
            lines.append(f"- Slot {i}: {get_agent_display_name(a)} ({a})")
        participants_section = "\n".join(lines)
    else:
        participants_section = ""

    # Build pane snapshot instructions
    pane_lines = []
    for i, a in others:
        name = get_agent_display_name(a)
        pane_lines.append(f"  Slot {i} ({name}): {pane_snapshot_paths[i]}")
    pane_snapshot_instructions = "\n".join(pane_lines) if pane_lines else "  No other agent snapshots."

    return _CONCURRENT_PREAMBLE.format(
        current_agent_name=agent_name,
        current_agent_key=agent_key,
        slot_index=slot,
        participants_section=participants_section,
        repo_root=str(repo_root),
        workspace_log=str(workspace_log),
        pane_snapshot_instructions=pane_snapshot_instructions,
        task=task,
    )


def _build_agent_command(agent_key: str, prompt_path: Path, repo_root: Path) -> str:
    """Build the underlying agent command for a concurrent slot."""
    adapter = get_agent_adapter(agent_key)
    cli = shlex.quote(adapter.cli_command)
    pp = shlex.quote(str(prompt_path))
    rr = shlex.quote(str(repo_root))

    if agent_key == "claude":
        # Concurrent mode must not depend on pane-local approval prompts.
        return f'cd {rr} && {cli} --permission-mode dontAsk -p "$(cat {pp})"'
    elif agent_key == "codex":
        return f'cd {rr} && {cli} -a never -s workspace-write "$(cat {pp})"'
    else:
        return f'cd {rr} && {cli} "$(cat {pp})"'


def _build_shell_command(
    agent_key: str,
    prompt_path: Path,
    repo_root: Path,
    exit_code_path: Path,
) -> str:
    """Build the slot shell command, persisting the agent's real exit code."""
    exit_path = shlex.quote(str(exit_code_path))
    inner = _build_agent_command(agent_key, prompt_path, repo_root)
    script = (
        f"rm -f {exit_path}; "
        f"{inner}; "
        'code=$?; '
        f'printf "%s\\n" "$code" > {exit_path}; '
        'exit "$code"'
    )
    return f"/bin/sh -lc {shlex.quote(script)}"


def _write_pane_snapshot(snapshot_path: Path, pane_content: str) -> None:
    write_text_atomic(snapshot_path, pane_content)


def _refresh_pane_snapshots(
    tmux_sessions: Sequence[str],
    snapshot_paths: Sequence[Path],
) -> None:
    for session_name, snapshot_path in zip(tmux_sessions, snapshot_paths, strict=False):
        if _tmux_session_exists(session_name):
            pane_content = _tmux_capture_pane(session_name, 0)
        else:
            pane_content = "(session terminated)"
        _write_pane_snapshot(snapshot_path, pane_content)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

_POLL_INTERVAL = 5  # seconds between completion checks


def run_concurrent(
    repo_root: Path,
    *,
    agents: Sequence[str],
    task: str,
    max_time_seconds: int = 600,
    owner: str = "cli:race",
    on_agent_start: Callable[[int, str, str], None] | None = None,
    on_agent_done: Callable[[AgentOutcome], None] | None = None,
) -> ConcurrentResult:
    """Run agents concurrently in separate tmux sessions with shared visibility."""
    if len(agents) < 2:
        raise SystemExit("Concurrent mode requires at least 2 agents.")

    _require_tmux()
    require_available(agents)

    session_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
    tmux_sessions = [_tmux_session_name(session_id, slot) for slot in range(len(agents))]
    agent_names = [get_agent_display_name(a) for a in agents]

    start_session(
        repo_root,
        session_id=session_id,
        objective=task,
        # Concurrency is the execution mode; the persisted workstream kind
        # still needs to satisfy the session schema.
        workstream_kind="mixed",
        initial_agent=agents[0],
        next_action=f"Race with {', '.join(agent_names)}",
        snapshot_mode=None,
        owner=f"{owner}:start",
    )

    # Setup directories and workspace log
    cdir = concurrent_dir(repo_root, session_id)
    cdir.mkdir(parents=True, exist_ok=True)
    wlog_path = workspace_log_path(repo_root, session_id)
    wlog = WorkspaceLog(wlog_path)

    # Prepare per-agent files up front so prompts can reference other sessions.
    prompt_paths: list[Path] = []
    exit_code_paths: list[Path] = []
    pane_snapshot_paths: list[Path] = []
    agent_dirs: list[Path] = []
    for slot, agent_key in enumerate(agents):
        agent_dir = concurrent_agent_dir(repo_root, session_id, slot)
        agent_dir.mkdir(parents=True, exist_ok=True)
        agent_dirs.append(agent_dir)
        prompt_paths.append(agent_dir / "prompt.md")
        exit_code_paths.append(agent_dir / "exit-code.txt")
        pane_snapshot_path = agent_dir / "pane.txt"
        pane_snapshot_paths.append(pane_snapshot_path)
        write_text_atomic(pane_snapshot_path, "")

    # Write prompts and commands
    commands: list[str] = []
    for slot, agent_key in enumerate(agents):
        agent_dir = agent_dirs[slot]

        prompt_text = _build_concurrent_prompt(
            task=task,
            slot=slot,
            agent_key=agent_key,
            all_agents=agents,
            repo_root=repo_root,
            workspace_log=wlog_path,
            pane_snapshot_paths=pane_snapshot_paths,
        )
        prompt_path = prompt_paths[slot]
        prompt_path.write_text(prompt_text, encoding="utf-8")

        cmd = _build_shell_command(agent_key, prompt_path, repo_root, exit_code_paths[slot])
        commands.append(cmd)

    start_time = datetime.now(UTC)
    started_at = [utc_timestamp() for _ in agents]
    session_names_by_slot = dict(enumerate(tmux_sessions))

    for slot in range(len(agents)):
        tmux_session = tmux_sessions[slot]
        _tmux(
            "new-session", "-d",
            "-s", tmux_session,
            "-x", "200", "-y", "50",
            commands[slot],
        )
        _tmux(
            "set-window-option",
            "-t", f"{tmux_session}:0",
            "remain-on-exit",
            "on",
            check=False,
        )
        _tmux(
            "set-option",
            "-t", tmux_session,
            "mouse",
            "on",
            check=False,
        )

        wlog.append(LogEntry(
            timestamp=started_at[slot],
            agent_key=agents[slot],
            agent_slot=slot,
            entry_type="agent_started",
            summary=f"Started in tmux session {tmux_session}.",
        ))
        if on_agent_start:
            on_agent_start(slot, agents[slot], tmux_session)

    _refresh_pane_snapshots(tmux_sessions, pane_snapshot_paths)

    # Poll for completion
    stop_reason = "all_done"
    finished_slots: dict[int, AgentOutcome] = {}
    reported_slots: set[int] = set()

    def maybe_report_outcome(outcome: AgentOutcome) -> None:
        if not on_agent_done or outcome.slot in reported_slots:
            return
        on_agent_done(outcome)
        reported_slots.add(outcome.slot)

    try:
        deadline = start_time.timestamp() + max_time_seconds
        while len(finished_slots) < len(agents):
            if time.time() > deadline:
                stop_reason = "max_time"
                break

            _refresh_pane_snapshots(tmux_sessions, pane_snapshot_paths)

            for slot in range(len(agents)):
                if slot in finished_slots:
                    continue

                tmux_session = session_names_by_slot[slot]
                if not _tmux_session_exists(tmux_session):
                    finished_slots[slot] = _build_outcome(
                        slot=slot,
                        agent_key=agents[slot],
                        tmux_session=tmux_session,
                        pane_content="(session terminated)",
                        exit_code=None,
                        started_at=started_at[slot],
                        finished_at=utc_timestamp(),
                    )
                    stop_reason = "interrupted"
                    continue

                if _tmux_pane_dead(tmux_session, 0):
                    finished_at = utc_timestamp()
                    pane_content = _tmux_capture_pane(tmux_session, 0)
                    agent_key = agents[slot]
                    exit_code = _read_exit_code(exit_code_paths[slot])

                    outcome = _build_outcome(
                        slot=slot,
                        agent_key=agent_key,
                        tmux_session=tmux_session,
                        exit_code=exit_code,
                        pane_content=pane_content,
                        started_at=started_at[slot],
                        finished_at=finished_at,
                    )
                    finished_slots[slot] = outcome

                    wlog.append(LogEntry(
                        timestamp=finished_at,
                        agent_key=agent_key,
                        agent_slot=slot,
                        entry_type="signal" if outcome.done_signal else "turn_complete",
                        summary=outcome.summary,
                    ))
                    maybe_report_outcome(outcome)

            if stop_reason != "all_done":
                break
            if len(finished_slots) < len(agents):
                time.sleep(_POLL_INTERVAL)

    except KeyboardInterrupt:
        stop_reason = "interrupted"

    # Fill in any unfinished slots
    for slot in range(len(agents)):
        if slot not in finished_slots:
            pane_content = ""
            tmux_session = session_names_by_slot[slot]
            if _tmux_session_exists(tmux_session):
                pane_content = _tmux_capture_pane(tmux_session, 0)
            finished_slots[slot] = _build_outcome(
                slot=slot,
                agent_key=agents[slot],
                tmux_session=tmux_session,
                exit_code=None,
                pane_content=pane_content,
                started_at=started_at[slot],
                finished_at=utc_timestamp(),
            )

    for tmux_session in tmux_sessions:
        if _tmux_session_exists(tmux_session):
            _tmux("kill-session", "-t", tmux_session, check=False)

    outcomes = sorted(finished_slots.values(), key=lambda o: o.slot)
    stop_reason = _classify_stop_reason(stop_reason, outcomes)
    for outcome in outcomes:
        maybe_report_outcome(outcome)
    elapsed = (datetime.now(UTC) - start_time).total_seconds()

    return ConcurrentResult(
        session_id=session_id,
        agents=tuple(agents),
        tmux_sessions=tuple(tmux_sessions),
        stop_reason=stop_reason,
        elapsed_seconds=round(elapsed, 1),
        outcomes=tuple(outcomes),
    )
