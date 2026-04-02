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
from collections.abc import Sequence
from typing import Callable

from agent_relay.agents import (
    AGENT_REGISTRY,
    get_agent_adapter,
    get_agent_display_name,
    require_available,
)
from agent_relay.bootstrap import start_session
from agent_relay.layout import session_root, turn_dir, turns_dir, workspace_log_path
from agent_relay.resumable_state import normalize_resumable_state, resumable_state_text
from agent_relay.storage import is_session
from agent_relay.workspace_log import LogEntry, WorkspaceLog, utc_timestamp


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
    text: str  # Normalized text content from the agent
    summary: str  # Short one-line summary for UI
    done_signal: bool  # True if the turn advanced the completion protocol
    started_at: str
    finished_at: str
    control_status: str = "continue"
    control_reason: str = ""
    remaining_work: tuple[str, ...] = field(default_factory=tuple)
    verification: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class ConverseResult:
    session_id: str
    agents: tuple[str, ...]
    turns_completed: int
    stop_reason: str  # "max_turns" | "all_done" | "done_signal" | "blocked" | "interrupted" | "agent_error"
    turn_results: tuple[TurnResult, ...]
    continued_from_session_id: str | None = None


@dataclass(frozen=True, slots=True)
class TurnControl:
    status: str = "continue"
    reason: str = ""
    remaining_work: tuple[str, ...] = field(default_factory=tuple)
    verification: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class CompletionState:
    active_epoch: int | None = None
    proposed_by_slot: int | None = None
    proposed_turn: int | None = None
    agreeing_slots: tuple[int, ...] = field(default_factory=tuple)


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
                if isinstance(block, dict) and block.get("type") in (
                    "output_text",
                    "text",
                ):
                    text = block.get("text", "")
                    if text:
                        texts.append(text)
    return "\n".join(texts) if texts else raw_stdout.strip()


def normalize_gemini_output(raw_stdout: str) -> str:
    """Parse Gemini --output-format stream-json JSONL, extract model text."""
    texts: list[str] = []
    for line in raw_stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        # Gemini stream-json emits message events with role "model"
        msg = event.get("message") or event
        if isinstance(msg, dict) and msg.get("role") == "model":
            for block in msg.get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "")
                    if text:
                        texts.append(text)
            # Also handle flat text field on the message
            flat_text = msg.get("text", "")
            if isinstance(flat_text, str) and flat_text and flat_text not in texts:
                texts.append(flat_text)
        # Gemini result event contains final output
        if event.get("type") == "result":
            text = event.get("text", "")
            if isinstance(text, str) and text:
                texts.append(text)
    return "\n".join(texts) if texts else raw_stdout.strip()


def _normalize_output(agent_key: str, raw_stdout: str) -> str:
    if agent_key == "claude":
        return normalize_claude_output(raw_stdout)
    elif agent_key == "codex":
        return normalize_codex_output(raw_stdout)
    elif agent_key == "gemini":
        return normalize_gemini_output(raw_stdout)
    # Fallback: return raw output
    return raw_stdout.strip()


def _strip_done_marker(text: str) -> str:
    """Remove the CONVERSATION_COMPLETE marker from display text."""
    import re

    return re.sub(
        r"\s*CONVERSATION_COMPLETE\s*", " ", text, flags=re.IGNORECASE
    ).strip()


def _strip_turn_control(text: str) -> str:
    """Remove machine-readable relay control lines from display text."""
    kept_lines = [
        line
        for line in text.splitlines()
        if not line.strip().startswith("RELAY_STATUS:")
        and not line.strip().startswith("RELAY_STATE:")
    ]
    return "\n".join(kept_lines).strip()


# ---------------------------------------------------------------------------
# Stop detection
# ---------------------------------------------------------------------------

_DONE_MARKER = "CONVERSATION_COMPLETE"
_TURN_STATUS_PREFIX = "RELAY_STATUS:"
_TURN_STATE_PREFIX = "RELAY_STATE:"
_VALID_TURN_STATUSES = frozenset(
    {
        "continue",
        "blocked",
        "propose_done",
        "agree_done",
        "reopen",
    }
)


def detect_done_signal(text: str) -> bool:
    """Check if the agent's output signals the conversation is done."""
    return _DONE_MARKER in text.upper()


def _has_legacy_done_line(text: str) -> bool:
    return any(line.strip().upper() == _DONE_MARKER for line in text.splitlines())


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


def parse_turn_control(text: str) -> TurnControl:
    """Parse the machine-readable RELAY_STATUS line from agent output."""
    for raw_line in reversed(text.splitlines()):
        line = raw_line.strip()
        if not line.startswith(_TURN_STATUS_PREFIX):
            continue

        payload = line[len(_TURN_STATUS_PREFIX) :].strip()
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue

        status = str(data.get("status", "continue")).strip().lower()
        if status not in _VALID_TURN_STATUSES:
            continue

        return TurnControl(
            status=status,
            reason=str(data.get("reason", "")).strip(),
            remaining_work=_coerce_string_tuple(data.get("remaining_work")),
            verification=_coerce_string_tuple(data.get("verification")),
        )

    if _has_legacy_done_line(text):
        return TurnControl(
            status="propose_done",
            reason="Legacy CONVERSATION_COMPLETE marker",
        )

    return TurnControl()


def parse_turn_state(text: str) -> dict[str, object] | None:
    """Parse the optional machine-readable RELAY_STATE line from agent output."""
    for raw_line in reversed(text.splitlines()):
        line = raw_line.strip()
        if not line.startswith(_TURN_STATE_PREFIX):
            continue

        payload = line[len(_TURN_STATE_PREFIX) :].strip()
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            continue

        normalized = normalize_resumable_state(data, source="relay_turn")
        if normalized is not None:
            return normalized
    return None


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

_MAX_HISTORY_CHARS = 50_000
_FULL_HISTORY_TURNS = 3

_SYSTEM_PREAMBLE = """\
You are participating in a turn-based conversation with {other_count} other AI agent{plural}.
You are {current_agent_name} ({current_agent_key}).

{participants_section}

Your shared workspace is: {repo_root}

## Rules
- Focus on the task. Be direct and concise.
- Build on what the other agents have done. Don't repeat their work.
- You can read files, edit code, run commands — use your full capabilities.
"""

_SINGLE_AGENT_PREAMBLE = """\
You are participating in a Relay-managed single-agent session.
You are {current_agent_name} ({current_agent_key}).

Your shared workspace is: {repo_root}

## Rules
- Focus on the task. Be direct and concise.
- Work incrementally and leave a clear next step when you are not done.
- You can read files, edit code, run commands — use your full capabilities.
"""


def build_turn_prompt(
    task: str,
    turn_history: list[TurnResult],
    current_agent: str,
    all_agents: Sequence[str],
    turn_number: int,
    repo_root: Path,
    completion_state: CompletionState | None = None,
    continuation_context: str | None = None,
) -> str:
    """Build the prompt for the next agent turn with full conversation history."""
    current_name = get_agent_display_name(current_agent)
    completion_state = completion_state or CompletionState()
    single_agent = len(all_agents) == 1

    # Build participants section listing all other agents
    others = [a for a in all_agents if a != current_agent]
    unique_others = list(dict.fromkeys(others))  # dedupe preserving order
    if unique_others:
        participant_lines = ["Other participants:"]
        for a in unique_others:
            participant_lines.append(f"- {get_agent_display_name(a)} ({a})")
        participants_section = "\n".join(participant_lines)
    else:
        participants_section = ""

    other_count = len(set(all_agents) - {current_agent})
    if turn_number == 1:
        # Full preamble with rules on the first turn.
        if single_agent:
            preamble = _SINGLE_AGENT_PREAMBLE.format(
                current_agent_name=current_name,
                current_agent_key=current_agent,
                repo_root=str(repo_root),
            )
        else:
            preamble = _SYSTEM_PREAMBLE.format(
                current_agent_name=current_name,
                current_agent_key=current_agent,
                other_count=other_count,
                plural="s" if other_count != 1 else "",
                participants_section=participants_section,
                repo_root=str(repo_root),
            )
    else:
        # Abbreviated preamble on subsequent turns.
        preamble = f"You are {current_name} ({current_agent}). Workspace: {repo_root}\n"

    lines: list[str] = [
        preamble,
        "## Task",
        "",
        task,
        "",
    ]

    if continuation_context:
        lines.extend(
            [
                "## Continuation Context",
                "",
                continuation_context,
                "",
            ]
        )

    if turn_number == 1:
        # Full protocol on first turn so the agent learns the format.
        if single_agent:
            lines.extend(
                [
                    "## Completion Protocol",
                    "",
                    "You may include one optional structured line immediately before the status line:",
                    'RELAY_STATE: {"summary":"...","current_plan":["..."],"assumptions":[],"blockers":[],"intended_edits":["..."],"next_step":"..."}',
                    "",
                    "End every turn with exactly one machine-readable line in this format:",
                    'RELAY_STATUS: {"status":"continue","reason":"...","remaining_work":["..."],"verification":[]}',
                    "",
                    "Allowed status values:",
                    "- continue: there is still meaningful work to do.",
                    "- blocked: you cannot proceed without human input or an external dependency.",
                    "- propose_done: the task is complete and verification lists concrete checks.",
                    "",
                    "Protocol rules:",
                    "- Use propose_done when the task is complete.",
                    "- Use blocked when you need human input or an external dependency.",
                    "- When using propose_done, remaining_work must be [].",
                    "",
                    "## Completion State",
                    "",
                    "- This is a single-agent run.",
                    "- Use status propose_done when the task is complete.",
                    "- Use status blocked when you cannot continue without outside help.",
                    "",
                ]
            )
        else:
            lines.extend(
                [
                    "## Completion Protocol",
                    "",
                    "You may include one optional structured line immediately before the status line:",
                    'RELAY_STATE: {"summary":"...","current_plan":["..."],"assumptions":[],"blockers":[],"intended_edits":["..."],"next_step":"..."}',
                    "",
                    "End every turn with exactly one machine-readable line in this format:",
                    'RELAY_STATUS: {"status":"continue","reason":"...","remaining_work":["..."],"verification":[]}',
                    "",
                    "Allowed status values:",
                    "- continue: there is still meaningful work to do.",
                    "- blocked: you cannot proceed without human input or an external dependency.",
                    "- propose_done: you believe the task is complete. Only use this after every agent has spoken at least once, remaining_work is empty, and verification lists concrete checks.",
                    "- agree_done: another agent already proposed completion and you agree the task is complete.",
                    "- reopen: there is an active completion proposal, but you found remaining work, a bug, or a disagreement.",
                    "",
                    "Protocol rules:",
                    "- NEVER use propose_done or agree_done on your first turn.",
                    "- If there is an active completion proposal and you disagree, use reopen, not continue.",
                    "- If you made edits, found a bug, or uncovered missing verification after a completion proposal, use reopen.",
                    "- When using propose_done or agree_done, remaining_work must be [].",
                    "",
                    "## Completion State",
                    "",
                ]
            )
    else:
        # Abbreviated protocol reminder on subsequent turns.
        lines.extend(
            [
                "## Completion Protocol",
                "",
                "(Same as Turn 1.) End your response with a RELAY_STATUS JSON line.",
                "",
            ]
        )

    # Multi-agent completion state (epoch tracking) — needed every turn.
    if not single_agent and turn_number > 1:
        lines.extend(["## Completion State", ""])

    if not single_agent:
        if completion_state.active_epoch is None:
            lines.extend(
                [
                    "- No active completion proposal.",
                    "- If you believe the task is complete, use status propose_done.",
                    "",
                ]
            )
        else:
            agreeing = (
                ", ".join(
                    f"Slot {slot} — {get_agent_display_name(all_agents[slot])}"
                    for slot in completion_state.agreeing_slots
                )
                or "none yet"
            )
            proposer = "Unknown agent"
            if completion_state.proposed_by_slot is not None:
                proposer = (
                    f"Slot {completion_state.proposed_by_slot} — "
                    f"{get_agent_display_name(all_agents[completion_state.proposed_by_slot])}"
                )
            lines.extend(
                [
                    (
                        f"- Active completion proposal: epoch {completion_state.active_epoch}, "
                        f"proposed on Turn {completion_state.proposed_turn} by {proposer}."
                    ),
                    f"- Agents already agreeing in this epoch: {agreeing}.",
                    "- If you agree the task is complete, use status agree_done.",
                    "- If you disagree or found more work, use status reopen.",
                    "",
                ]
            )

    if turn_history:
        lines.append("## Conversation so far")
        lines.append("")

        # Keep the last N turns in full; summarize older turns to save tokens.
        recent_cutoff = len(turn_history) - _FULL_HISTORY_TURNS

        # Build history, truncating oldest turns if over budget
        history_parts: list[str] = []
        total_chars = 0
        for turn in reversed(turn_history):
            agent_name = get_agent_display_name(turn.agent_key)
            idx = turn_history.index(turn)
            if idx < recent_cutoff:
                # Compact summary for older turns
                remaining = ", ".join(turn.remaining_work) if turn.remaining_work else "none noted"
                part = f"### Turn {turn.turn_number} — {agent_name} (summary)\n\n{turn.summary}\nRemaining: {remaining}\n"
            else:
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

    lines.extend(
        [
            f"## Your turn (Turn {turn_number})",
            "",
            f"You are {current_name}. Continue working on the task above.",
            "Review what has been done so far and take the next step.",
            "",
        ]
    )

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
        return (
            f'cd {rr} && {cli} -p "$(cat {pp})" --output-format stream-json --verbose'
        )
    elif agent_key == "codex":
        return f'cd {rr} && {cli} exec "$(cat {pp})" --json'
    elif agent_key == "gemini":
        return f'cd {rr} && {cli} -p "$(cat {pp})" --output-format stream-json'
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
    state_payload: dict[str, object] | None = None,
) -> Path:
    """Write turn prompt and output to the session's turns directory."""
    tdir = turn_dir(repo_root, session_id, turn_number)
    tdir.mkdir(parents=True, exist_ok=True)

    (tdir / "prompt.md").write_text(prompt_text, encoding="utf-8")
    (tdir / "output.jsonl").write_text(raw_stdout, encoding="utf-8")
    if raw_stderr.strip():
        (tdir / "stderr.log").write_text(raw_stderr, encoding="utf-8")
    if state_payload is not None:
        (tdir / "state.json").write_text(
            resumable_state_text(state_payload), encoding="utf-8"
        )

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
                return stripped[: max_len - 3] + "..."
            return stripped
    return "(no output)"


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _build_turn_state(
    *,
    task: str,
    turn_number: int,
    agent_key: str,
    display_text: str,
    started_at: str,
    finished_at: str,
    control: TurnControl,
    explicit_state: dict[str, object] | None,
) -> dict[str, object]:
    summary = _make_summary(display_text)
    remaining_work = list(control.remaining_work)
    verification = list(control.verification)
    next_step = (
        _state_string(explicit_state, "next_step")
        or (remaining_work[0] if remaining_work else "")
        or control.reason
        or summary
    )
    state_payload = {
        "source": "relay_turn",
        "objective": _state_string(explicit_state, "objective") or task,
        "summary": _state_string(explicit_state, "summary") or summary,
        "status": control.status,
        "reason": _state_string(explicit_state, "reason") or control.reason,
        "current_plan": _state_list(explicit_state, "current_plan") or remaining_work,
        "assumptions": _state_list(explicit_state, "assumptions"),
        "blockers": _state_list(explicit_state, "blockers"),
        "intended_edits": _state_list(explicit_state, "intended_edits"),
        "remaining_work": _state_list(explicit_state, "remaining_work")
        or remaining_work,
        "verification": _state_list(explicit_state, "verification") or verification,
        "next_step": next_step,
        "agent_key": agent_key,
        "agent_display_name": get_agent_display_name(agent_key),
        "turn_number": turn_number,
        "captured_at": finished_at,
        "metadata": {
            "started_at": started_at,
            "finished_at": finished_at,
        },
    }
    normalized = normalize_resumable_state(state_payload, source="relay_turn")
    if normalized is None:
        raise RuntimeError("failed to build relay turn state")
    return normalized


def _state_string(state: dict[str, object] | None, key: str) -> str | None:
    if state is None:
        return None
    value = state.get(key)
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped if stripped else None


def _state_list(state: dict[str, object] | None, key: str) -> list[str]:
    if state is None:
        return []
    value = state.get(key)
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    if not isinstance(value, list):
        return []
    items: list[str] = []
    for item in value:
        if isinstance(item, str):
            stripped = item.strip()
            if stripped:
                items.append(stripped)
    return items


def converse(
    repo_root: Path,
    *,
    agents: Sequence[str],
    task: str,
    max_turns: int = 10,
    continue_from_session_id: str | None = None,
    owner: str = "cli:converse",
    on_turn_start: Callable[[str, int, int], None] | None = None,
    on_turn_complete: Callable[[TurnResult], None] | None = None,
) -> ConverseResult:
    """Run a turn-based conversation between N agents.

    Args:
        repo_root: Repository root path.
        agents: Ordered list of agent keys (round-robin). Must have >= 1.
        task: The task prompt for the conversation.
        max_turns: Maximum number of turns before stopping.
        continue_from_session_id: Optional prior session to continue from.
        owner: Owner string for journal events.
        on_turn_start: Callback(agent_key, turn_number, max_turns) before each turn.
        on_turn_complete: Callback(TurnResult) after each turn.

    Returns:
        ConverseResult with all turn data and stop reason.
    """
    if len(agents) < 1:
        raise SystemExit("Converse requires at least 1 agent.")

    # Validate continuation session exists
    if continue_from_session_id and not is_session(repo_root, continue_from_session_id):
        raise SystemExit(f"Session not found: {continue_from_session_id}")

    # Validate agents are registered and installed
    require_available(agents)

    n = len(agents)
    agent_names = [get_agent_display_name(a) for a in agents]

    # Create session
    session_id = (
        datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
    )
    start_session(
        repo_root,
        session_id=session_id,
        objective=task,
        workstream_kind="mixed",
        initial_agent=agents[0],
        next_action=(
            f"Continue managed run in {agent_names[0]}"
            if n == 1
            else f"Converse with {', '.join(agent_names[1:])}"
        ),
        snapshot_mode=None,
        owner=f"{owner}:start",
    )

    # Ensure turns directory exists
    turns_root = turns_dir(repo_root, session_id)
    turns_root.mkdir(parents=True, exist_ok=True)

    # Initialize workspace log
    wlog = WorkspaceLog(workspace_log_path(repo_root, session_id))

    # Build continuation context from prior session
    continuation_context: str | None = None
    if continue_from_session_id:
        ctx_parts: list[str] = []
        ctx_parts.append(
            f"This run continues prior relay session: {continue_from_session_id}"
        )
        prior_session_dir = session_root(repo_root, continue_from_session_id)
        ctx_parts.append(f"Prior session root: {prior_session_dir}")

        # Load prior workspace log
        prior_wlog_path = workspace_log_path(repo_root, continue_from_session_id)
        if prior_wlog_path.exists():
            ctx_parts.append(f"Prior workspace log: {prior_wlog_path}")

        # Load latest turn state from prior session for resumable context
        prior_turns = turns_dir(repo_root, continue_from_session_id)
        if prior_turns.exists():
            turn_dirs = sorted(prior_turns.iterdir())
            for tdir in reversed(turn_dirs):
                state_file = tdir / "state.json"
                if state_file.exists():
                    try:
                        import json as _json

                        state_data = _json.loads(state_file.read_text(encoding="utf-8"))
                        if isinstance(state_data, dict):
                            summary = state_data.get("summary", "")
                            next_step = state_data.get("next_step", "")
                            remaining = state_data.get("remaining_work", [])
                            if summary:
                                ctx_parts.append(f"Last agent summary: {summary}")
                            if next_step:
                                ctx_parts.append(f"Planned next step: {next_step}")
                            if remaining:
                                ctx_parts.append(
                                    f"Remaining work: {', '.join(remaining)}"
                                )
                    except (OSError, json.JSONDecodeError):
                        pass
                    break

        ctx_parts.append("Build on that existing work. Do not restart from scratch.")
        continuation_context = "\n".join(f"- {p}" for p in ctx_parts)

    turn_history: list[TurnResult] = []
    completion_epoch = 0
    active_completion_epoch: int | None = None
    proposed_by_slot: int | None = None
    proposed_turn: int | None = None
    agreeing_slots: set[int] = set()
    stop_reason = "max_turns"

    try:
        for turn_number in range(1, max_turns + 1):
            slot = (turn_number - 1) % n
            current_agent = agents[slot]

            if on_turn_start:
                on_turn_start(current_agent, turn_number, max_turns)

            # Build prompt
            prompt_text = build_turn_prompt(
                task=task,
                turn_history=turn_history,
                current_agent=current_agent,
                all_agents=agents,
                turn_number=turn_number,
                repo_root=repo_root,
                completion_state=CompletionState(
                    active_epoch=active_completion_epoch,
                    proposed_by_slot=proposed_by_slot,
                    proposed_turn=proposed_turn,
                    agreeing_slots=tuple(i for i in range(n) if i in agreeing_slots),
                ),
                continuation_context=continuation_context if turn_number == 1 else None,
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
            control = parse_turn_control(raw_text)
            explicit_state = parse_turn_state(raw_text)
            status = control.status
            if n == 1:
                if status == "agree_done":
                    status = "propose_done"
                elif status == "reopen":
                    status = "continue"
            else:
                if status in {"propose_done", "agree_done"} and turn_number < n:
                    status = "continue"
                elif status == "agree_done" and active_completion_epoch is None:
                    status = "continue"
                elif status == "propose_done" and active_completion_epoch is not None:
                    status = "agree_done"
                elif status == "reopen" and active_completion_epoch is None:
                    status = "continue"

            done = status in {"propose_done", "agree_done"}
            display_text = _strip_done_marker(_strip_turn_control(raw_text))
            turn_state = _build_turn_state(
                task=task,
                turn_number=turn_number,
                agent_key=current_agent,
                display_text=display_text,
                started_at=started_at,
                finished_at=finished_at,
                control=control,
                explicit_state=explicit_state,
            )

            # Store artifacts
            _store_turn_artifacts(
                repo_root,
                session_id,
                turn_number,
                prompt_text,
                result.stdout,
                result.stderr,
                state_payload=turn_state,
            )

            # Build turn result
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
                control_status=status,
                control_reason=control.reason,
                remaining_work=control.remaining_work,
                verification=control.verification,
            )
            turn_history.append(turn)

            # Write workspace log entry
            wlog.append(
                LogEntry(
                    timestamp=finished_at,
                    agent_key=current_agent,
                    agent_slot=slot,
                    entry_type="signal" if done else "turn_complete",
                    summary=turn.summary,
                )
            )

            if on_turn_complete:
                on_turn_complete(turn)

            if result.returncode != 0:
                stop_reason = "agent_error"
                break
            if n == 1:
                if status == "blocked":
                    stop_reason = "blocked"
                    break
                if status in {"propose_done", "agree_done"}:
                    stop_reason = "done_signal"
                    break
                continue

            # --- Multi-agent stop conditions ---
            if status == "propose_done":
                if active_completion_epoch is None:
                    completion_epoch += 1
                    active_completion_epoch = completion_epoch
                    proposed_by_slot = slot
                    proposed_turn = turn_number
                    agreeing_slots = {slot}
                else:
                    agreeing_slots.add(slot)
            elif status == "agree_done":
                if active_completion_epoch is not None:
                    agreeing_slots.add(slot)
            elif (
                status in {"continue", "blocked", "reopen"}
                and active_completion_epoch is not None
            ):
                active_completion_epoch = None
                proposed_by_slot = None
                proposed_turn = None
                agreeing_slots.clear()

            if active_completion_epoch is not None and len(agreeing_slots) == n:
                stop_reason = "all_done"
                break

    except KeyboardInterrupt:
        stop_reason = "interrupted"

    return ConverseResult(
        session_id=session_id,
        agents=tuple(agents),
        turns_completed=len(turn_history),
        stop_reason=stop_reason,
        turn_results=tuple(turn_history),
        continued_from_session_id=continue_from_session_id,
    )
