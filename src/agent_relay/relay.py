"""One-command handoff: agent-relay relay <target>

Orchestrates the full flow (start/checkpoint → prepare → failover) in a single call,
so users never have to think about sessions, checkpoints, or lifecycle phases.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from agent_relay.agents import AGENT_REGISTRY, get_agent_display_name
from agent_relay.bootstrap import start_session
from agent_relay.capture_support import CaptureOptions
from agent_relay.checkpoints import create_checkpoint_for_command
from agent_relay.handoffs import create_handoff_for_command
from agent_relay.layout import sessions_root
from agent_relay.provider_capture import capture_provider_state
from agent_relay.storage import is_session, load_session_view


@dataclass(frozen=True, slots=True)
class RelayResult:
    session_id: str
    from_agent: str
    to_agent: str
    resume_path: str
    launch_command: str
    launch_instructions: str
    packet_aware: bool
    execute_policy: str
    handoff_id: str
    created_session: bool
    warning: str | None = None


def _find_active_session(repo_root: Path) -> tuple[str, str] | None:
    """Find the most recent active/paused session and return (session_id, current_agent)."""
    root = sessions_root(repo_root)
    if not root.exists():
        return None

    best: tuple[str, str, str] | None = None  # (updated_at, session_id, agent)
    for session_dir in root.iterdir():
        if not session_dir.is_dir():
            continue
        session_id = session_dir.name
        if not is_session(repo_root, session_id):
            continue
        try:
            view = load_session_view(repo_root, session_id)
        except Exception:
            continue
        if view.phase in ("active", "paused"):
            candidate = (view.updated_at, session_id, view.current_agent)
            if best is None or candidate[0] > best[0]:
                best = candidate

    if best is None:
        return None
    return (best[1], best[2])


def relay(
    repo_root: Path,
    *,
    to_agent: str,
    from_agent: str | None = None,
    task: str | None = None,
    planning_note: str | None = None,
    planning_note_file: str | None = None,
    proposed_edits: str | None = None,
    proposed_edits_file: str | None = None,
    no_launch: bool = False,
    owner: str = "cli:relay",
) -> RelayResult:
    """Single-command handoff from one agent to another.

    1. Finds an existing active session or creates a new one.
    2. Captures current workspace state (checkpoint + prepare).
    3. Creates a handoff packet for the target agent.

    Returns everything needed to launch or manually pass the packet.
    """
    if to_agent not in AGENT_REGISTRY:
        allowed = ", ".join(sorted(AGENT_REGISTRY))
        raise SystemExit(f"Unknown agent: {to_agent}. Choose from: {allowed}")

    # Step 1: Find or create a session
    existing = _find_active_session(repo_root)
    created_session = False

    if existing is not None:
        session_id, detected_agent = existing
        if from_agent is None:
            from_agent = detected_agent
        # Ensure we're handing off to a different agent (or same if user insists)
        view = load_session_view(repo_root, session_id)
    else:
        # No active session — create one
        if from_agent is None:
            from_agent = _infer_source_agent(to_agent)
        created_session = True
        session_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
        objective = task or f"Relay handoff to {get_agent_display_name(to_agent)}"
        start_session(
            repo_root,
            session_id=session_id,
            objective=objective,
            workstream_kind="mixed",
            initial_agent=from_agent,
            next_action=f"Hand off to {get_agent_display_name(to_agent)}",
            snapshot_mode=None,
            owner=f"{owner}:start",
        )

    # Step 2: Checkpoint + prepare (capture current state and mark ready for handoff)
    handoff_reason = task or f"Continue work in {get_agent_display_name(to_agent)}"
    next_action = f"Hand off to {get_agent_display_name(to_agent)}"

    view = load_session_view(repo_root, session_id)
    if view.phase in ("active", "paused"):
        provider_capture = capture_provider_state(
            repo_root,
            agent_key=from_agent or view.current_agent,
            session_id=session_id,
        )
        effective_planning_note = planning_note
        effective_planning_note_file = planning_note_file
        if effective_planning_note is None and effective_planning_note_file is None:
            effective_planning_note = provider_capture.planning_snapshot

        effective_proposed_edits = proposed_edits
        effective_proposed_edits_file = proposed_edits_file
        if effective_proposed_edits is None and effective_proposed_edits_file is None:
            effective_proposed_edits = provider_capture.proposed_edits

        # Need to prepare for handoff — checkpoint with git changes + transition
        # If the user provided a task, store it as a research note so the
        # target agent gets context even when there are no code changes.
        notes = [task] if task else []

        create_checkpoint_for_command(
            repo_root,
            session_id,
            command_name="prepare",
            options=CaptureOptions(
                status=None,
                snapshot_mode=None,
                next_action=next_action,
                decisions=[],
                blockers=[],
                touched_files=[],
                research_notes=notes,
                implementation_notes=[],
                planning_snapshot=effective_planning_note,
                planning_snapshot_file=effective_planning_note_file,
                proposed_edits=effective_proposed_edits,
                proposed_edits_file=effective_proposed_edits_file,
                provider_source_agent=provider_capture.source_agent,
                provider_hook_name=provider_capture.hook_name,
                provider_resumable_state=provider_capture.resumable_state,
                provider_transcript=provider_capture.transcript,
                provider_session_metadata=provider_capture.session_metadata,
                provider_warnings=list(provider_capture.warnings),
                validation_status=None,
                validation_summary=None,
                research_note_file=None,
                implementation_note_file=None,
                validation_summary_file=None,
                capture_git_changes=True,
            ),
            owner=f"{owner}:prepare",
        )

    # Step 3: Create the handoff packet
    handoff_result = create_handoff_for_command(
        repo_root,
        session_id,
        to_agent=to_agent,
        reason=handoff_reason,
        evidence_depth="standard",
        owner=f"{owner}:failover",
    )

    return RelayResult(
        session_id=session_id,
        from_agent=from_agent,
        to_agent=to_agent,
        resume_path=handoff_result.resume_path,
        launch_command=handoff_result.launch_command,
        launch_instructions=handoff_result.launch_instructions,
        packet_aware=handoff_result.packet_aware,
        execute_policy=handoff_result.execute_policy,
        handoff_id=handoff_result.handoff_id,
        created_session=created_session,
        warning=handoff_result.warning,
    )


def _infer_source_agent(to_agent: str) -> str:
    """If the user only specifies the target, pick the other agent as source."""
    agents = list(AGENT_REGISTRY)
    for agent in agents:
        if agent != to_agent:
            return agent
    # Fallback: if somehow there's only one agent, use it
    return agents[0]
