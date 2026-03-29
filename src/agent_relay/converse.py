"""Turn-based agent-to-agent conversation orchestrator.

Runs two agents in alternating --print mode, captures their full structured
output, and feeds it as context to the next agent's turn.
"""
from __future__ import annotations

import json
import secrets
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from agent_relay.agents import AGENT_REGISTRY, get_agent_adapter, get_agent_display_name
from agent_relay.bootstrap import start_session
from agent_relay.layout import turn_dir, turns_dir


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class TurnResult:
    turn_number: int
    agent_key: str
    exit_code: int
    raw_stdout: str
    raw_stderr: str
    text: str           # Normalized text content from the agent
    summary: str        # Short one-line summary for UI
    done_signal: bool   # True if agent signaled CONVERSATION_COMPLETE
    started_at: str
    finished_at: str


@dataclass(frozen=True, slots=True)
class ConverseResult:
    session_id: str
    agent1: str
    agent2: str
    turns_completed: int
    stop_reason: str   # "max_turns" | "done_signal" | "interrupted" | "agent_error"
    turn_results: tuple[TurnResult, ...]


# ---------------------------------------------------------------------------
# Output normalization
# ---------------------------------------------------------------------------

def normalize_claude_output(raw_stdout: str) -> str:
    """Parse Claude --output-format stream-json JSONL, extract assistant text."""
    texts: list[str] = []
    for line in raw_stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        # Claude stream-json emits message objects with content blocks
        msg = event.get("message") or event
        if msg.get("role") != "assistant":
            continue
        for block in msg.get("content", []):
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                if text:
                    texts.append(text)
    return "\n".join(texts) if texts else raw_stdout.strip()


def normalize_codex_output(raw_stdout: str) -> str:
    """Parse Codex exec --json JSONL, extract agent message text."""
    texts: list[str] = []
    for line in raw_stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        # Codex emits item.completed with item.type=agent_message
        if event.get("type") == "item.completed":
            item = event.get("item", {})
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text", "")
                if text:
                    texts.append(text)
        # Also handle message events with content blocks
        if event.get("type") == "message" and event.get("role") == "assistant":
            for block in event.get("content", []):
                if isinstance(block, dict) and block.get("type") in ("output_text", "text"):
                    text = block.get("text", "")
                    if text:
                        texts.append(text)
    return "\n".join(texts) if texts else raw_stdout.strip()


def _normalize_output(agent_key: str, raw_stdout: str) -> str:
    if agent_key == "claude":
        return normalize_claude_output(raw_stdout)
    elif agent_key == "codex":
        return normalize_codex_output(raw_stdout)
    # Fallback: return raw output
    return raw_stdout.strip()


def _strip_done_marker(text: str) -> str:
    """Remove the CONVERSATION_COMPLETE marker from display text."""
    import re
    return re.sub(r'\s*CONVERSATION_COMPLETE\s*', ' ', text, flags=re.IGNORECASE).strip()


# ---------------------------------------------------------------------------
# Stop detection
# ---------------------------------------------------------------------------

_DONE_MARKER = "CONVERSATION_COMPLETE"


def detect_done_signal(text: str) -> bool:
    """Check if the agent's output signals the conversation is done."""
    return _DONE_MARKER in text.upper()


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

_MAX_HISTORY_CHARS = 100_000

_SYSTEM_PREAMBLE = """\
You are participating in a turn-based conversation with another AI agent.
You are {current_agent_name} ({current_agent_key}).
The other agent is {other_agent_name} ({other_agent_key}).

Your shared workspace is: {repo_root}

## Rules
- Focus on the task. Be direct and concise.
- Build on what the other agent has done. Don't repeat their work.
- You can read files, edit code, run commands — use your full capabilities.
- When you believe the task is FULLY COMPLETE, include the exact text: CONVERSATION_COMPLETE
- Do NOT include CONVERSATION_COMPLETE if there is still meaningful work to do.
- The other agent will always get a chance to respond after you signal completion.
- The conversation only ends when both agents agree, or after the other agent's final response.
"""


def build_turn_prompt(
    task: str,
    turn_history: list[TurnResult],
    current_agent: str,
    other_agent: str,
    turn_number: int,
    repo_root: Path,
) -> str:
    """Build the prompt for the next agent turn with full conversation history."""
    current_name = get_agent_display_name(current_agent)
    other_name = get_agent_display_name(other_agent)

    lines: list[str] = [
        _SYSTEM_PREAMBLE.format(
            current_agent_name=current_name,
            current_agent_key=current_agent,
            other_agent_name=other_name,
            other_agent_key=other_agent,
            repo_root=str(repo_root),
        ),
        "## Task",
        "",
        task,
        "",
    ]

    if turn_history:
        lines.append("## Conversation so far")
        lines.append("")

        # Build history, truncating oldest turns if over budget
        history_parts: list[str] = []
        total_chars = 0
        for turn in reversed(turn_history):
            agent_name = get_agent_display_name(turn.agent_key)
            part = f"### Turn {turn.turn_number} — {agent_name}\n\n{turn.text}\n"
            total_chars += len(part)
            if total_chars > _MAX_HISTORY_CHARS:
                history_parts.append(
                    f"(Earlier turns truncated — {len(turn_history) - len(history_parts)} total turns)\n"
                )
                break
            history_parts.append(part)

        history_parts.reverse()
        lines.extend(history_parts)

    lines.extend([
        f"## Your turn (Turn {turn_number})",
        "",
        f"You are {current_name}. Continue working on the task above.",
        "Review what has been done so far and take the next step.",
        "",
    ])

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Agent runner
# ---------------------------------------------------------------------------

def _build_agent_command(agent_key: str, prompt_path: Path, repo_root: Path) -> str:
    """Build the shell command to run an agent turn with captured output."""
    adapter = get_agent_adapter(agent_key)
    cli = shlex.quote(adapter.cli_command)
    pp = shlex.quote(str(prompt_path))
    rr = shlex.quote(str(repo_root))

    if agent_key == "claude":
        return f'cd {rr} && {cli} -p "$(cat {pp})" --output-format stream-json --verbose'
    elif agent_key == "codex":
        return f'cd {rr} && {cli} exec "$(cat {pp})" --json'
    else:
        # Generic fallback
        return f'cd {rr} && {cli} "$(cat {pp})"'


def run_agent_turn(
    agent_key: str,
    prompt_path: Path,
    repo_root: Path,
) -> subprocess.CompletedProcess[str]:
    """Run a single agent turn with full output capture."""
    command = _build_agent_command(agent_key, prompt_path, repo_root)
    return subprocess.run(
        command,
        cwd=str(repo_root),
        shell=True,
        check=False,
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# Turn artifact storage
# ---------------------------------------------------------------------------

def _store_turn_artifacts(
    repo_root: Path,
    session_id: str,
    turn_number: int,
    prompt_text: str,
    raw_stdout: str,
    raw_stderr: str,
) -> Path:
    """Write turn prompt and output to the session's turns directory."""
    tdir = turn_dir(repo_root, session_id, turn_number)
    tdir.mkdir(parents=True, exist_ok=True)

    (tdir / "prompt.md").write_text(prompt_text, encoding="utf-8")
    (tdir / "output.jsonl").write_text(raw_stdout, encoding="utf-8")
    if raw_stderr.strip():
        (tdir / "stderr.log").write_text(raw_stderr, encoding="utf-8")

    return tdir


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def _make_summary(text: str, max_len: int = 120) -> str:
    """Extract a one-line summary from agent output."""
    # Take first non-empty line
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            if len(stripped) > max_len:
                return stripped[:max_len - 3] + "..."
            return stripped
    return "(no output)"


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def converse(
    repo_root: Path,
    *,
    agent1: str,
    agent2: str,
    task: str,
    max_turns: int = 10,
    owner: str = "cli:converse",
    on_turn_start: Callable[[str, int, int], None] | None = None,
    on_turn_complete: Callable[[TurnResult], None] | None = None,
) -> ConverseResult:
    """Run a turn-based conversation between two agents.

    Args:
        repo_root: Repository root path.
        agent1: First agent key (speaks first).
        agent2: Second agent key.
        task: The task prompt for the conversation.
        max_turns: Maximum number of turns before stopping.
        owner: Owner string for journal events.
        on_turn_start: Callback(agent_key, turn_number, max_turns) before each turn.
        on_turn_complete: Callback(TurnResult) after each turn.

    Returns:
        ConverseResult with all turn data and stop reason.
    """
    # Validate agents
    for agent_key in (agent1, agent2):
        if agent_key not in AGENT_REGISTRY:
            allowed = ", ".join(sorted(AGENT_REGISTRY))
            raise SystemExit(f"Unknown agent: {agent_key}. Choose from: {allowed}")

    # Create session
    session_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
    start_session(
        repo_root,
        session_id=session_id,
        objective=task,
        workstream_kind="mixed",
        initial_agent=agent1,
        next_action=f"Converse with {get_agent_display_name(agent2)}",
        snapshot_mode=None,
        owner=f"{owner}:start",
    )

    # Ensure turns directory exists
    turns_root = turns_dir(repo_root, session_id)
    turns_root.mkdir(parents=True, exist_ok=True)

    agents = [agent1, agent2]
    turn_history: list[TurnResult] = []
    done_agents: set[str] = set()  # Track which agents have signaled completion
    stop_reason = "max_turns"

    try:
        for turn_number in range(1, max_turns + 1):
            current_agent = agents[(turn_number - 1) % 2]
            other_agent = agents[turn_number % 2]

            if on_turn_start:
                on_turn_start(current_agent, turn_number, max_turns)

            # Build prompt
            prompt_text = build_turn_prompt(
                task=task,
                turn_history=turn_history,
                current_agent=current_agent,
                other_agent=other_agent,
                turn_number=turn_number,
                repo_root=repo_root,
            )

            # Write prompt to turn directory
            tdir = turn_dir(repo_root, session_id, turn_number)
            tdir.mkdir(parents=True, exist_ok=True)
            prompt_path = tdir / "prompt.md"
            prompt_path.write_text(prompt_text, encoding="utf-8")

            # Run agent
            started_at = _utc_now()
            result = run_agent_turn(current_agent, prompt_path, repo_root)
            finished_at = _utc_now()

            # Normalize output
            raw_text = _normalize_output(current_agent, result.stdout)
            done = detect_done_signal(raw_text)
            display_text = _strip_done_marker(raw_text)

            # Store artifacts
            _store_turn_artifacts(
                repo_root, session_id, turn_number,
                prompt_text, result.stdout, result.stderr,
            )

            # Build turn result — text is cleaned for display, raw kept in stdout
            turn = TurnResult(
                turn_number=turn_number,
                agent_key=current_agent,
                exit_code=result.returncode,
                raw_stdout=result.stdout,
                raw_stderr=result.stderr,
                text=display_text,
                summary=_make_summary(display_text),
                done_signal=done,
                started_at=started_at,
                finished_at=finished_at,
            )
            turn_history.append(turn)

            if on_turn_complete:
                on_turn_complete(turn)

            # Check stop conditions — bilateral completion
            if done:
                done_agents.add(current_agent)
            if result.returncode != 0:
                stop_reason = "agent_error"
                break
            # Both agents agreed — conversation is done
            if len(done_agents) == 2:
                stop_reason = "done_signal"
                break
            # One agent signaled done, but the other already had their response turn
            # (i.e., the other agent spoke after the first done signal without also signaling)
            if len(done_agents) == 1 and other_agent in done_agents:
                stop_reason = "done_signal"
                break

    except KeyboardInterrupt:
        stop_reason = "interrupted"

    return ConverseResult(
        session_id=session_id,
        agent1=agent1,
        agent2=agent2,
        turns_completed=len(turn_history),
        stop_reason=stop_reason,
        turn_results=tuple(turn_history),
    )
