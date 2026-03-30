"""Concurrent agent execution orchestrator.

Runs multiple agents simultaneously as background subprocesses, coordinating
through a shared workspace log. Each agent gets its own prompt with concurrent
instructions and a pointer to the workspace log for inter-agent visibility.
"""
from __future__ import annotations

import asyncio
import secrets
import shlex
import signal
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from agent_relay.agents import (
    AGENT_REGISTRY,
    get_agent_adapter,
    get_agent_display_name,
    require_available,
)
from agent_relay.bootstrap import start_session
from agent_relay.converse import _normalize_output, detect_done_signal, _make_summary
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
    exit_code: int | None   # None if killed/timeout
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
# Concurrent prompt builder
# ---------------------------------------------------------------------------

_CONCURRENT_PREAMBLE = """\
You are participating in a CONCURRENT multi-agent session.
You are {current_agent_name} ({current_agent_key}), running in slot {slot}.

{participants_section}

Your shared workspace is: {repo_root}

## Concurrent Mode Rules
- You are running AT THE SAME TIME as the other agents — not taking turns.
- A shared activity log is at: {workspace_log}
  Read it periodically to see what other agents are doing.
- Before editing a file, check its current state — another agent may have changed it.
- Coordinate through the workspace log: describe what you're working on.
- Focus on your strengths. Don't duplicate work you see others doing.
- When you believe your part of the task is FULLY COMPLETE, include: CONVERSATION_COMPLETE

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
) -> str:
    agent_name = get_agent_display_name(agent_key)
    others = [a for a in all_agents if a != agent_key]
    unique_others = list(dict.fromkeys(others))
    if unique_others:
        lines = ["Other agents running concurrently:"]
        for a in unique_others:
            lines.append(f"- {get_agent_display_name(a)} ({a})")
        participants_section = "\n".join(lines)
    else:
        participants_section = ""

    return _CONCURRENT_PREAMBLE.format(
        current_agent_name=agent_name,
        current_agent_key=agent_key,
        slot=slot,
        participants_section=participants_section,
        repo_root=str(repo_root),
        workspace_log=str(workspace_log),
        task=task,
    )


# ---------------------------------------------------------------------------
# Async agent runner
# ---------------------------------------------------------------------------

def _build_shell_command(agent_key: str, prompt_path: Path, repo_root: Path) -> str:
    adapter = get_agent_adapter(agent_key)
    cli = shlex.quote(adapter.cli_command)
    pp = shlex.quote(str(prompt_path))
    rr = shlex.quote(str(repo_root))

    if agent_key == "claude":
        return f'cd {rr} && {cli} -p "$(cat {pp})" --output-format stream-json --verbose'
    elif agent_key == "codex":
        return f'cd {rr} && {cli} exec "$(cat {pp})" --json'
    else:
        return f'cd {rr} && {cli} "$(cat {pp})"'


async def _run_agent(
    slot: int,
    agent_key: str,
    prompt_path: Path,
    repo_root: Path,
    wlog: WorkspaceLog,
) -> AgentOutcome:
    """Run a single agent as an async subprocess."""
    command = _build_shell_command(agent_key, prompt_path, repo_root)
    started_at = utc_timestamp()

    wlog.append(LogEntry(
        timestamp=started_at,
        agent_key=agent_key,
        agent_slot=slot,
        entry_type="agent_started",
        summary=f"Starting concurrent execution.",
    ))

    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=str(repo_root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout_bytes, stderr_bytes = await proc.communicate()
    finished_at = utc_timestamp()

    raw_stdout = stdout_bytes.decode("utf-8", errors="replace")
    raw_stderr = stderr_bytes.decode("utf-8", errors="replace")
    text = _normalize_output(agent_key, raw_stdout)
    done = detect_done_signal(text)
    summary = _make_summary(text)

    wlog.append(LogEntry(
        timestamp=finished_at,
        agent_key=agent_key,
        agent_slot=slot,
        entry_type="signal" if done else "turn_complete",
        summary=summary,
    ))

    return AgentOutcome(
        slot=slot,
        agent_key=agent_key,
        exit_code=proc.returncode,
        raw_stdout=raw_stdout,
        raw_stderr=raw_stderr,
        text=text,
        summary=summary,
        done_signal=done,
        started_at=started_at,
        finished_at=finished_at,
    )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

async def _run_concurrent_async(
    repo_root: Path,
    *,
    agents: Sequence[str],
    task: str,
    max_time_seconds: int = 600,
    owner: str = "cli:concurrent",
    on_agent_start: Callable[[int, str], None] | None = None,
    on_agent_done: Callable[[AgentOutcome], None] | None = None,
) -> ConcurrentResult:
    if len(agents) < 2:
        raise SystemExit("Concurrent mode requires at least 2 agents.")

    require_available(agents)

    session_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
    agent_names = [get_agent_display_name(a) for a in agents]

    start_session(
        repo_root,
        session_id=session_id,
        objective=task,
        workstream_kind="concurrent",
        initial_agent=agents[0],
        next_action=f"Concurrent with {', '.join(agent_names)}",
        snapshot_mode=None,
        owner=f"{owner}:start",
    )

    # Setup directories and workspace log
    cdir = concurrent_dir(repo_root, session_id)
    cdir.mkdir(parents=True, exist_ok=True)
    wlog_path = workspace_log_path(repo_root, session_id)
    wlog = WorkspaceLog(wlog_path)

    # Write prompts and create agent tasks
    tasks: list[asyncio.Task[AgentOutcome]] = []
    start_time = datetime.now(UTC)

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
        )
        prompt_path = agent_dir / "prompt.md"
        prompt_path.write_text(prompt_text, encoding="utf-8")

        if on_agent_start:
            on_agent_start(slot, agent_key)

        t = asyncio.create_task(
            _run_agent(slot, agent_key, prompt_path, repo_root, wlog),
            name=f"agent-{slot}-{agent_key}",
        )
        tasks.append(t)

    # Wait for all agents with timeout
    stop_reason = "all_done"
    outcomes: list[AgentOutcome] = []

    try:
        done, pending = await asyncio.wait(
            tasks,
            timeout=max_time_seconds,
            return_when=asyncio.ALL_COMPLETED,
        )

        for t in done:
            outcome = t.result()
            outcomes.append(outcome)
            if on_agent_done:
                on_agent_done(outcome)

        if pending:
            stop_reason = "max_time"
            for t in pending:
                t.cancel()
            # Give cancelled tasks a moment to clean up
            cancelled_done, _ = await asyncio.wait(pending, timeout=5)
            for t in cancelled_done:
                try:
                    outcome = t.result()
                    outcomes.append(outcome)
                except (asyncio.CancelledError, Exception):
                    pass

    except asyncio.CancelledError:
        stop_reason = "interrupted"
        for t in tasks:
            if not t.done():
                t.cancel()

    # Check for errors
    if stop_reason == "all_done":
        for o in outcomes:
            if o.exit_code is not None and o.exit_code != 0:
                stop_reason = "agent_error"
                break

    # Sort outcomes by slot
    outcomes.sort(key=lambda o: o.slot)
    elapsed = (datetime.now(UTC) - start_time).total_seconds()

    return ConcurrentResult(
        session_id=session_id,
        agents=tuple(agents),
        stop_reason=stop_reason,
        elapsed_seconds=round(elapsed, 1),
        outcomes=tuple(outcomes),
    )


def run_concurrent(
    repo_root: Path,
    *,
    agents: Sequence[str],
    task: str,
    max_time_seconds: int = 600,
    owner: str = "cli:concurrent",
    on_agent_start: Callable[[int, str], None] | None = None,
    on_agent_done: Callable[[AgentOutcome], None] | None = None,
) -> ConcurrentResult:
    """Run agents concurrently. Synchronous wrapper around the async implementation."""
    return asyncio.run(
        _run_concurrent_async(
            repo_root,
            agents=agents,
            task=task,
            max_time_seconds=max_time_seconds,
            owner=owner,
            on_agent_start=on_agent_start,
            on_agent_done=on_agent_done,
        )
    )
