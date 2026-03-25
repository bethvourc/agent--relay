from __future__ import annotations

import argparse
import json
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_relay.agents import (
    AGENT_NAMES,
    get_agent_profile,
    render_launch_command,
    render_launch_instructions,
)

STATE_DIRNAME = ".agent-relay"


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_repo_root(repo: str | None) -> Path:
    return Path(repo or os.getcwd()).resolve()


def session_root(repo_root: Path, session_id: str) -> Path:
    return repo_root / STATE_DIRNAME / "sessions" / session_id


def state_path(repo_root: Path, session_id: str) -> Path:
    return session_root(repo_root, session_id) / "state.json"


def load_state(repo_root: Path, session_id: str) -> dict[str, Any]:
    path = state_path(repo_root, session_id)
    if not path.exists():
        raise SystemExit(f"Session not found: {session_id}")
    return json.loads(path.read_text())


def save_state(repo_root: Path, session_id: str, state: dict[str, Any]) -> Path:
    root = session_root(repo_root, session_id)
    (root / "resume").mkdir(parents=True, exist_ok=True)
    (root / "artifacts").mkdir(parents=True, exist_ok=True)
    path = root / "state.json"
    path.write_text(json.dumps(state, indent=2) + "\n")
    return path


def render_resume_packet(
    state: dict[str, Any],
    target_agent: str,
    *,
    handoff_reason: str,
    prepared_at: str,
) -> str:
    if target_agent == "claude":
        return render_claude_resume_packet(state, handoff_reason=handoff_reason, prepared_at=prepared_at)
    if target_agent == "codex":
        return render_codex_resume_packet(state, handoff_reason=handoff_reason, prepared_at=prepared_at)
    raise SystemExit(f"Unsupported target agent: {target_agent}")


def render_claude_resume_packet(state: dict[str, Any], *, handoff_reason: str, prepared_at: str) -> str:
    source_profile = get_agent_profile(state["current_agent"])
    lines = [
        "# Claude Code Resume Packet",
        "",
        "Resume this Agent Relay session from the structured state below.",
        "",
        "Priority for this turn:",
        "- Reconstruct the current repo state from the listed files and notes.",
        "- Continue from the recorded next action instead of re-planning from scratch.",
        "- Write a new checkpoint before another handoff.",
        "",
        "Session snapshot:",
        f"- Objective: {state['objective']}",
        f"- Repository root: {state['repo_root']}",
        f"- Current status: {state['current_status']}",
        f"- Source agent: {source_profile.display_name}",
        f"- Prepared at: {prepared_at}",
        f"- Handoff reason: {handoff_reason}",
        f"- Next action: {state.get('next_action') or 'Not recorded'}",
        "",
        "Validation:",
        f"- Status: {render_validation_status(state)}",
        f"- Summary: {render_validation_summary(state)}",
        "",
    ]
    append_bullet_section(lines, "Decisions:", state.get("decisions"))
    append_bullet_section(lines, "Blockers:", state.get("blockers"))
    append_bullet_section(lines, "Research notes:", state.get("research_notes"))
    append_bullet_section(lines, "Implementation notes:", state.get("implementation_notes"))
    append_bullet_section(lines, "Touched files:", state.get("touched_files"))
    append_bullet_section(lines, "Recent handoffs:", render_recent_handoffs(state))
    return "\n".join(lines) + "\n"


def render_codex_resume_packet(state: dict[str, Any], *, handoff_reason: str, prepared_at: str) -> str:
    source_profile = get_agent_profile(state["current_agent"])
    lines = [
        "# Codex Resume Packet",
        "",
        "You are taking over an in-progress Agent Relay session in this repository.",
        "",
        "Execution brief:",
        f"- Objective: {state['objective']}",
        f"- Repository root: {state['repo_root']}",
        f"- Current status: {state['current_status']}",
        f"- Source agent: {source_profile.display_name}",
        f"- Prepared at: {prepared_at}",
        f"- Handoff reason: {handoff_reason}",
        f"- Immediate next step: {state.get('next_action') or 'Not recorded'}",
        "",
        "Operational constraints:",
        "- Work from the repository state on disk.",
        "- Preserve repo-local session state under .agent-relay/.",
        "- Update the session checkpoint before another failover.",
        "",
        "Validation:",
        f"- Status: {render_validation_status(state)}",
        f"- Summary: {render_validation_summary(state)}",
        "",
    ]
    append_bullet_section(lines, "Decisions to preserve:", state.get("decisions"))
    append_bullet_section(lines, "Blockers to resolve:", state.get("blockers"))
    append_bullet_section(lines, "Research context:", state.get("research_notes"))
    append_bullet_section(lines, "Implementation context:", state.get("implementation_notes"))
    append_bullet_section(lines, "Files to inspect first:", state.get("touched_files"))
    append_bullet_section(lines, "Recent handoffs:", render_recent_handoffs(state))
    return "\n".join(lines) + "\n"


def append_bullet_section(lines: list[str], heading: str, items: list[str] | None) -> None:
    lines.append(heading)
    if items:
        lines.extend([f"- {item}" for item in items])
    else:
        lines.append("- None recorded")
    lines.append("")


def render_validation_status(state: dict[str, Any]) -> str:
    validation = state.get("validation") or {}
    return validation.get("status") or "not_run"


def render_validation_summary(state: dict[str, Any]) -> str:
    validation = state.get("validation") or {}
    return validation.get("summary") or "None recorded"


def render_recent_handoffs(state: dict[str, Any]) -> list[str]:
    handoffs = state.get("handoffs") or []
    if not handoffs:
        return []

    rendered = []
    for handoff in handoffs[-3:]:
        source = describe_agent(handoff.get("from_agent"))
        target = describe_agent(handoff.get("to_agent"))
        reason = handoff.get("reason") or "No reason recorded"
        prepared_at = handoff.get("prepared_at") or "Unknown time"
        rendered.append(f"{prepared_at}: {source} -> {target} ({reason})")
    return rendered


def describe_agent(agent: str | None) -> str:
    if not agent:
        return "Unknown agent"
    try:
        return get_agent_profile(agent).display_name
    except SystemExit:
        return agent


def build_handoff_record(
    state: dict[str, Any],
    *,
    repo_root: Path,
    to_agent: str,
    reason: str,
    prepared_at: str,
    resume_path: Path,
) -> dict[str, Any]:
    profile = get_agent_profile(to_agent)
    launch_command, launch_template, launch_template_source = render_launch_command(profile, repo_root, resume_path)
    return {
        "from_agent": state["current_agent"],
        "to_agent": to_agent,
        "reason": reason,
        "prepared_at": prepared_at,
        "resume_packet_path": str(resume_path),
        "launch_status": "ready",
        "launch_profile": profile.display_name,
        "launch_cwd": str(repo_root),
        "launch_command": launch_command,
        "launch_template": launch_template,
        "launch_template_source": launch_template_source,
        "launch_instructions": render_launch_instructions(profile, repo_root, resume_path),
    }


def cmd_start(args: argparse.Namespace) -> int:
    repo_root = default_repo_root(args.repo)
    session_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
    now = utc_now()
    state = {
        "schema_version": 1,
        "session_id": session_id,
        "repo_root": str(repo_root),
        "objective": args.task,
        "workstream_kind": args.workstream_kind,
        "current_agent": args.agent,
        "current_status": "active",
        "created_at": now,
        "updated_at": now,
        "next_action": args.next_action or "",
        "decisions": [],
        "blockers": [],
        "research_notes": [],
        "implementation_notes": [],
        "touched_files": [],
        "validation": {
            "status": "not_run",
            "summary": "",
        },
        "handoffs": [],
    }
    path = save_state(repo_root, session_id, state)
    print(f"Created session {session_id}")
    print(path)
    return 0


def cmd_checkpoint(args: argparse.Namespace) -> int:
    repo_root = default_repo_root(args.repo)
    state = load_state(repo_root, args.session)
    state["updated_at"] = utc_now()
    if args.status:
        state["current_status"] = args.status
    if args.next_action is not None:
        state["next_action"] = args.next_action
    if args.decision:
        state.setdefault("decisions", []).extend(args.decision)
    if args.blocker:
        state.setdefault("blockers", []).extend(args.blocker)
    if args.touched_file:
        state.setdefault("touched_files", []).extend(args.touched_file)
    save_state(repo_root, args.session, state)
    print(f"Updated session {args.session}")
    return 0


def cmd_failover(args: argparse.Namespace) -> int:
    repo_root = default_repo_root(args.repo)
    state = load_state(repo_root, args.session)
    prepared_at = utc_now()
    resume_packet = render_resume_packet(
        state,
        args.to_agent,
        handoff_reason=args.reason,
        prepared_at=prepared_at,
    )
    resume_path = session_root(repo_root, args.session) / "resume" / f"{args.to_agent}.md"
    resume_path.write_text(resume_packet)

    handoff = build_handoff_record(
        state,
        repo_root=repo_root,
        to_agent=args.to_agent,
        reason=args.reason,
        prepared_at=prepared_at,
        resume_path=resume_path,
    )
    state.setdefault("handoffs", []).append(handoff)
    state["current_status"] = "handoff_prepared"
    state["updated_at"] = prepared_at
    save_state(repo_root, args.session, state)
    print(f"Prepared handoff from {handoff['from_agent']} to {handoff['to_agent']}")
    print(resume_path)
    print(handoff["launch_command"])
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    repo_root = default_repo_root(args.repo)
    state = load_state(repo_root, args.session)
    print(json.dumps(state, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-relay")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="Create a new relay session")
    start.add_argument("--agent", required=True, choices=AGENT_NAMES)
    start.add_argument("--task", required=True)
    start.add_argument("--workstream-kind", default="mixed", choices=["research", "implementation", "mixed"])
    start.add_argument("--next-action")
    start.add_argument("--repo")
    start.set_defaults(func=cmd_start)

    checkpoint = subparsers.add_parser("checkpoint", help="Update session state")
    checkpoint.add_argument("session")
    checkpoint.add_argument("--status")
    checkpoint.add_argument("--next-action")
    checkpoint.add_argument("--decision", action="append")
    checkpoint.add_argument("--blocker", action="append")
    checkpoint.add_argument("--touched-file", action="append")
    checkpoint.add_argument("--repo")
    checkpoint.set_defaults(func=cmd_checkpoint)

    failover = subparsers.add_parser("failover", help="Prepare a handoff packet")
    failover.add_argument("session")
    failover.add_argument("--to-agent", required=True, choices=AGENT_NAMES)
    failover.add_argument("--reason", required=True)
    failover.add_argument("--repo")
    failover.set_defaults(func=cmd_failover)

    inspect = subparsers.add_parser("inspect", help="Print session state")
    inspect.add_argument("session")
    inspect.add_argument("--repo")
    inspect.set_defaults(func=cmd_inspect)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
