from __future__ import annotations

from dataclasses import dataclass

from agent_relay.agents import get_agent_profile
from agent_relay.models import CheckpointRecord, SessionState

EVIDENCE_DEPTHS = {"minimal", "standard", "full"}


@dataclass(frozen=True)
class ResumeRenderOptions:
    evidence_depth: str = "standard"

    def __post_init__(self) -> None:
        if self.evidence_depth not in EVIDENCE_DEPTHS:
            allowed = ", ".join(sorted(EVIDENCE_DEPTHS))
            raise ValueError(f"evidence_depth must be one of: {allowed}")


def render_resume_packet(
    session: SessionState,
    checkpoint: CheckpointRecord,
    target_agent: str,
    *,
    handoff_reason: str,
    prepared_at: str,
    options: ResumeRenderOptions | None = None,
) -> str:
    resume_options = options or ResumeRenderOptions()
    if target_agent == "claude":
        return render_claude_resume_packet(
            session,
            checkpoint,
            handoff_reason=handoff_reason,
            prepared_at=prepared_at,
            options=resume_options,
        )
    if target_agent == "codex":
        return render_codex_resume_packet(
            session,
            checkpoint,
            handoff_reason=handoff_reason,
            prepared_at=prepared_at,
            options=resume_options,
        )
    raise SystemExit(f"Unsupported target agent: {target_agent}")


def render_claude_resume_packet(
    session: SessionState,
    checkpoint: CheckpointRecord,
    *,
    handoff_reason: str,
    prepared_at: str,
    options: ResumeRenderOptions,
) -> str:
    source_profile = get_agent_profile(session.current_agent)
    lines = [
        "# Claude Code Resume Packet",
        "",
        "Resume this Agent Relay session from the structured state below.",
        "",
        "Priority for this turn:",
        "- Reconstruct the current repo state from the listed files and notes.",
        "- Continue from the latest checkpoint instead of re-planning from scratch.",
        "- Write a new checkpoint before another handoff.",
        "",
        "Session snapshot:",
        f"- Objective: {session.objective}",
        f"- Repository root: {session.repo_root}",
        f"- Current status: {session.current_status}",
        f"- Source agent: {source_profile.display_name}",
        f"- Prepared at: {prepared_at}",
        f"- Handoff reason: {handoff_reason}",
        "",
    ]
    append_checkpoint_snapshot(lines, checkpoint)
    lines.extend(
        [
            "Validation:",
            f"- Status: {session.validation.status}",
            f"- Summary: {session.validation.summary or 'None recorded'}",
            "",
        ]
    )
    append_bullet_section(lines, "Decisions:", session.decisions)
    append_bullet_section(lines, "Blockers:", session.blockers)
    append_bullet_section(lines, "Research notes:", session.research_notes)
    append_bullet_section(lines, "Implementation notes:", session.implementation_notes)
    append_bullet_section(lines, "Touched files:", session.touched_files)
    append_bullet_section(lines, "Recent handoffs:", render_recent_handoffs(session))
    append_artifacts_section(lines, checkpoint, options)
    return "\n".join(lines) + "\n"


def render_codex_resume_packet(
    session: SessionState,
    checkpoint: CheckpointRecord,
    *,
    handoff_reason: str,
    prepared_at: str,
    options: ResumeRenderOptions,
) -> str:
    source_profile = get_agent_profile(session.current_agent)
    lines = [
        "# Codex Resume Packet",
        "",
        "You are taking over an in-progress Agent Relay session in this repository.",
        "",
        "Execution brief:",
        f"- Objective: {session.objective}",
        f"- Repository root: {session.repo_root}",
        f"- Current status: {session.current_status}",
        f"- Source agent: {source_profile.display_name}",
        f"- Prepared at: {prepared_at}",
        f"- Handoff reason: {handoff_reason}",
        "",
    ]
    append_checkpoint_snapshot(lines, checkpoint)
    lines.extend(
        [
            "Operational constraints:",
            "- Work from the repository state on disk.",
            "- Preserve repo-local session state under .agent-relay/.",
            "- Update the session checkpoint before another failover.",
            "",
            "Validation:",
            f"- Status: {session.validation.status}",
            f"- Summary: {session.validation.summary or 'None recorded'}",
            "",
        ]
    )
    append_bullet_section(lines, "Decisions to preserve:", session.decisions)
    append_bullet_section(lines, "Blockers to resolve:", session.blockers)
    append_bullet_section(lines, "Research context:", session.research_notes)
    append_bullet_section(lines, "Implementation context:", session.implementation_notes)
    append_bullet_section(lines, "Files to inspect first:", session.touched_files)
    append_bullet_section(lines, "Recent handoffs:", render_recent_handoffs(session))
    append_artifacts_section(lines, checkpoint, options)
    return "\n".join(lines) + "\n"


def append_checkpoint_snapshot(lines: list[str], checkpoint: CheckpointRecord) -> None:
    lines.extend(
        [
            "Latest checkpoint:",
            f"- Checkpoint id: {checkpoint.checkpoint_id}",
            f"- Created at: {checkpoint.created_at}",
            f"- Status: {checkpoint.status}",
            f"- Recorded next action: {checkpoint.next_action or 'Not recorded'}",
            "",
        ]
    )


def append_bullet_section(lines: list[str], heading: str, items: list[str]) -> None:
    lines.append(heading)
    if items:
        lines.extend([f"- {item}" for item in items])
    else:
        lines.append("- None recorded")
    lines.append("")


def render_recent_handoffs(session: SessionState) -> list[str]:
    if not session.handoffs:
        return []

    rendered = []
    for handoff in session.handoffs[-3:]:
        source = get_agent_profile(handoff.from_agent).display_name
        target = get_agent_profile(handoff.to_agent).display_name
        rendered.append(f"{handoff.prepared_at}: {source} -> {target} ({handoff.reason})")
    return rendered


def append_artifacts_section(
    lines: list[str],
    checkpoint: CheckpointRecord,
    options: ResumeRenderOptions,
) -> None:
    if options.evidence_depth == "minimal":
        return

    lines.append("Latest checkpoint artifacts:")
    if not checkpoint.artifacts:
        lines.append("- None recorded")
        lines.append("")
        return

    if options.evidence_depth == "standard":
        for key, value in checkpoint.artifacts.items():
            if isinstance(value, list):
                lines.append(f"- {key}: {len(value)} item(s)")
            else:
                lines.append(f"- {key}: {value}")
        lines.append("")
        return

    for key, value in checkpoint.artifacts.items():
        if isinstance(value, list):
            lines.append(f"- {key}:")
            for item in value:
                lines.append(f"  - {item}")
        else:
            lines.append(f"- {key}: {value}")
    lines.append("")
