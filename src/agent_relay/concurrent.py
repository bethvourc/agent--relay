"""Concurrent agent execution orchestrator (tmux-backed).

Runs multiple agents simultaneously in tmux panes, giving them live visibility
into each other's work. Each agent can read other panes via tmux capture-pane
and coordinate through a shared workspace log.
"""
from __future__ import annotations

import json
import os
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
You are {current_agent_name} ({current_agent_key}), running in pane {pane_index}.

{participants_section}

Your shared workspace is: {repo_root}
tmux session: {tmux_session}

## Concurrent Mode Rules
- You are running AT THE SAME TIME as the other agents — not taking turns.
- To see what another agent is doing RIGHT NOW, run:
  tmux capture-pane -t {tmux_session}:0.<pane_number> -p
  {pane_instructions}
- A shared activity log is at: {workspace_log}
- Before editing a file, check its current state — another agent may have changed it.
- Coordinate: decide who handles what. Don't duplicate work.
- End with a machine-readable status line:
  RELAY_STATUS: {{"status":"continue","reason":"...","remaining_work":["..."],"verification":[]}}
- Allowed statuses: continue, blocked, done, error
- Use done only when your part is truly complete and remaining_work is [].
- Use error if you hit a terminal failure you could not resolve.
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
    tmux_session: str,
) -> str:
    agent_name = get_agent_display_name(agent_key)
    others = [(i, a) for i, a in enumerate(all_agents) if i != slot]
    unique_others = list(dict.fromkeys(a for _, a in others))

    if unique_others:
        lines = ["Other agents running concurrently:"]
        for i, a in others:
            lines.append(f"- Pane {i}: {get_agent_display_name(a)} ({a})")
        participants_section = "\n".join(lines)
    else:
        participants_section = ""

    # Build pane reading instructions
    pane_lines = []
    for i, a in others:
        name = get_agent_display_name(a)
        pane_lines.append(f"  Pane {i} ({name}): tmux capture-pane -t {tmux_session}:0.{i} -p")
    pane_instructions = "\n".join(pane_lines) if pane_lines else ""

    return _CONCURRENT_PREAMBLE.format(
        current_agent_name=agent_name,
        current_agent_key=agent_key,
        pane_index=slot,
        participants_section=participants_section,
        repo_root=str(repo_root),
        workspace_log=str(workspace_log),
        tmux_session=tmux_session,
        pane_instructions=pane_instructions,
        task=task,
    )


def _build_agent_command(agent_key: str, prompt_path: Path, repo_root: Path) -> str:
    """Build the underlying agent command for a pane."""
    adapter = get_agent_adapter(agent_key)
    cli = shlex.quote(adapter.cli_command)
    pp = shlex.quote(str(prompt_path))
    rr = shlex.quote(str(repo_root))

    if agent_key == "claude":
        # Interactive mode in pane — no stream-json needed since user watches directly
        return f'cd {rr} && {cli} -p "$(cat {pp})"'
    elif agent_key == "codex":
        return f'cd {rr} && {cli} "$(cat {pp})"'
    else:
        return f'cd {rr} && {cli} "$(cat {pp})"'


def _build_shell_command(
    agent_key: str,
    prompt_path: Path,
    repo_root: Path,
    exit_code_path: Path,
) -> str:
    """Build the pane shell command, persisting the agent's real exit code."""
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
    on_agent_start: Callable[[int, str], None] | None = None,
    on_agent_done: Callable[[AgentOutcome], None] | None = None,
) -> ConcurrentResult:
    """Run agents concurrently in tmux panes with live visibility."""
    if len(agents) < 2:
        raise SystemExit("Concurrent mode requires at least 2 agents.")

    _require_tmux()
    require_available(agents)

    session_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
    tmux_session = f"relay-{session_id}"
    agent_names = [get_agent_display_name(a) for a in agents]

    start_session(
        repo_root,
        session_id=session_id,
        objective=task,
        workstream_kind="concurrent",
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

    # Write prompts
    prompt_paths: list[Path] = []
    commands: list[str] = []
    exit_code_paths: list[Path] = []
    for slot, agent_key in enumerate(agents):
        agent_dir = concurrent_agent_dir(repo_root, session_id, slot)
        agent_dir.mkdir(parents=True, exist_ok=True)

        prompt_text = _build_concurrent_prompt(
            task=task,
            slot=slot,
            agent_key=agent_key,
            all_agents=agents,
            repo_root=repo_root,
            workspace_log=wlog_path,
            tmux_session=tmux_session,
        )
        prompt_path = agent_dir / "prompt.md"
        prompt_path.write_text(prompt_text, encoding="utf-8")
        prompt_paths.append(prompt_path)
        exit_code_path = agent_dir / "exit-code.txt"
        exit_code_paths.append(exit_code_path)

        cmd = _build_shell_command(agent_key, prompt_path, repo_root, exit_code_path)
        commands.append(cmd)

    # Create tmux session with first agent
    start_time = datetime.now(UTC)
    started_at = [utc_timestamp() for _ in agents]

    _tmux(
        "new-session", "-d",
        "-s", tmux_session,
        "-x", "200", "-y", "50",
        commands[0],
    )
    _tmux(
        "set-window-option",
        "-t", f"{tmux_session}:0",
        "remain-on-exit",
        "on",
        check=False,
    )

    wlog.append(LogEntry(
        timestamp=started_at[0],
        agent_key=agents[0],
        agent_slot=0,
        entry_type="agent_started",
        summary=f"Started in tmux pane 0.",
    ))
    if on_agent_start:
        on_agent_start(0, agents[0])

    # Add panes for remaining agents
    for slot in range(1, len(agents)):
        _tmux(
            "split-window", "-t", f"{tmux_session}:{0}",
            "-h" if slot % 2 == 1 else "-v",
            commands[slot],
        )
        # Rebalance panes evenly
        _tmux("select-layout", "-t", f"{tmux_session}:{0}", "tiled", check=False)

        wlog.append(LogEntry(
            timestamp=started_at[slot],
            agent_key=agents[slot],
            agent_slot=slot,
            entry_type="agent_started",
            summary=f"Started in tmux pane {slot}.",
        ))
        if on_agent_start:
            on_agent_start(slot, agents[slot])

    # Attach the user's terminal to the tmux session so they can watch
    # We do this in a subprocess that we don't wait on — instead we poll
    # for pane completion in the background.
    #
    # If we're in a terminal, attach interactively. The user sees all panes.
    # The polling happens after the user detaches or all panes finish.

    # Poll for completion
    stop_reason = "all_done"
    finished_slots: dict[int, AgentOutcome] = {}
    attach_proc: subprocess.Popen[bytes] | None = None
    reported_slots: set[int] = set()

    def maybe_report_outcome(outcome: AgentOutcome) -> None:
        if not on_agent_done or outcome.slot in reported_slots:
            return
        if attach_proc is not None and attach_proc.poll() is None:
            return
        on_agent_done(outcome)
        reported_slots.add(outcome.slot)

    try:
        # Attach to tmux session (blocks until user detaches or all panes die)
        if os.isatty(0):
            attach_proc = subprocess.Popen(
                ["tmux", "attach-session", "-t", tmux_session],
            )

        # After detach (or if not a tty), poll until all panes are done or timeout
        deadline = start_time.timestamp() + max_time_seconds
        while len(finished_slots) < len(agents):
            if time.time() > deadline:
                stop_reason = "max_time"
                break

            for slot in range(len(agents)):
                if slot in finished_slots:
                    continue

                if not _tmux_session_exists(tmux_session):
                    # Session was killed entirely
                    for s in range(len(agents)):
                        if s not in finished_slots:
                            finished_slots[s] = _build_outcome(
                                slot=s,
                                agent_key=agents[s],
                                pane_content="(session terminated)",
                                exit_code=None,
                                started_at=started_at[s],
                                finished_at=utc_timestamp(),
                            )
                    stop_reason = "interrupted"
                    break

                if _tmux_pane_dead(tmux_session, slot):
                    finished_at = utc_timestamp()
                    # Capture final pane content
                    pane_content = _tmux_capture_pane(tmux_session, slot)
                    agent_key = agents[slot]
                    exit_code = _read_exit_code(exit_code_paths[slot])

                    outcome = _build_outcome(
                        slot=slot,
                        agent_key=agent_key,
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
            if _tmux_session_exists(tmux_session):
                pane_content = _tmux_capture_pane(tmux_session, slot)
            finished_slots[slot] = _build_outcome(
                slot=slot,
                agent_key=agents[slot],
                exit_code=None,
                pane_content=pane_content,
                started_at=started_at[slot],
                finished_at=utc_timestamp(),
            )

    # Clean up tmux session
    if _tmux_session_exists(tmux_session):
        _tmux("kill-session", "-t", tmux_session, check=False)
    if attach_proc is not None:
        try:
            attach_proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            attach_proc.terminate()
            try:
                attach_proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                attach_proc.kill()
                attach_proc.wait()

    outcomes = sorted(finished_slots.values(), key=lambda o: o.slot)
    stop_reason = _classify_stop_reason(stop_reason, outcomes)
    for outcome in outcomes:
        maybe_report_outcome(outcome)
    elapsed = (datetime.now(UTC) - start_time).total_seconds()

    return ConcurrentResult(
        session_id=session_id,
        agents=tuple(agents),
        stop_reason=stop_reason,
        elapsed_seconds=round(elapsed, 1),
        outcomes=tuple(outcomes),
    )
