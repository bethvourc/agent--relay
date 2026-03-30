"""Concurrent agent execution orchestrator (tmux-backed).

Runs multiple agents simultaneously in tmux panes, giving them live visibility
into each other's work. Each agent can read other panes via tmux capture-pane
and coordinate through a shared workspace log.
"""
from __future__ import annotations

import os
import secrets
import shlex
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class ConcurrentResult:
    session_id: str
    agents: tuple[str, ...]
    stop_reason: str   # "all_done" | "max_time" | "agent_error" | "interrupted"
    elapsed_seconds: float
    outcomes: tuple[AgentOutcome, ...]


# ---------------------------------------------------------------------------
# tmux helpers
# ---------------------------------------------------------------------------

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
- When your part is FULLY COMPLETE, include: CONVERSATION_COMPLETE

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


def _build_shell_command(agent_key: str, prompt_path: Path, repo_root: Path) -> str:
    """Build the interactive shell command for an agent (no output capture — runs in tmux pane)."""
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

        cmd = _build_shell_command(agent_key, prompt_path, repo_root)
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

    try:
        # Attach to tmux session (blocks until user detaches or all panes die)
        if os.isatty(0):
            subprocess.run(
                ["tmux", "attach-session", "-t", tmux_session],
                check=False,
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
                            finished_slots[s] = AgentOutcome(
                                slot=s, agent_key=agents[s], exit_code=None,
                                raw_stdout="", raw_stderr="",
                                text="(session terminated)", summary="Session terminated",
                                done_signal=False,
                                started_at=started_at[s], finished_at=utc_timestamp(),
                            )
                    stop_reason = "interrupted"
                    break

                if _tmux_pane_dead(tmux_session, slot):
                    finished_at = utc_timestamp()
                    # Capture final pane content
                    pane_content = _tmux_capture_pane(tmux_session, slot)
                    agent_key = agents[slot]

                    outcome = AgentOutcome(
                        slot=slot,
                        agent_key=agent_key,
                        exit_code=0,  # Can't reliably get exit code from tmux pane
                        raw_stdout=pane_content,
                        raw_stderr="",
                        text=pane_content.strip(),
                        summary=pane_content.strip()[:120] or "(no output)",
                        done_signal="CONVERSATION_COMPLETE" in pane_content.upper(),
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
                    if on_agent_done:
                        on_agent_done(outcome)

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
            finished_slots[slot] = AgentOutcome(
                slot=slot, agent_key=agents[slot], exit_code=None,
                raw_stdout=pane_content, raw_stderr="",
                text=pane_content.strip(),
                summary=pane_content.strip()[:120] or "(still running)",
                done_signal=False,
                started_at=started_at[slot], finished_at=utc_timestamp(),
            )

    # Clean up tmux session
    if _tmux_session_exists(tmux_session):
        _tmux("kill-session", "-t", tmux_session, check=False)

    outcomes = sorted(finished_slots.values(), key=lambda o: o.slot)
    elapsed = (datetime.now(UTC) - start_time).total_seconds()

    return ConcurrentResult(
        session_id=session_id,
        agents=tuple(agents),
        stop_reason=stop_reason,
        elapsed_seconds=round(elapsed, 1),
        outcomes=tuple(outcomes),
    )
