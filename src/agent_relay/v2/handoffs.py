from __future__ import annotations

import json
import secrets
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_relay.agents import LaunchSpec, get_agent_adapter, get_agent_display_name
from agent_relay.resume import EVIDENCE_DEPTHS, ResumeRenderOptions
from agent_relay.v2.errors import V2CorruptionError
from agent_relay.v2.hashing import sha256_bytes, sha256_path, sha256_text
from agent_relay.v2.layout import object_dir
from agent_relay.v2.locks import acquire_session_lock
from agent_relay.v2.models import (
    CheckpointManifest,
    DerivedHandoffView,
    DerivedSessionView,
    HandoffManifest,
    LaunchManifest,
    ManifestFile,
    SCHEMA_VERSION,
)
from agent_relay.v2.storage import (
    load_journal_events,
    load_latest_journal_event,
    load_referenced_object,
    load_session_view,
)
from agent_relay.v2.tx import JournalCommitRequest, SessionTransaction, recover_session_transactions


@dataclass(frozen=True, slots=True)
class HandoffCommandResult:
    handoff_id: str
    to_agent: str
    resume_path: str
    launch_command: str
    launch_instructions: str


@dataclass(frozen=True, slots=True)
class LaunchPreviewResult:
    handoff_id: str
    to_agent: str
    resume_path: str
    launch_command: str
    launch_instructions: str


@dataclass(frozen=True, slots=True)
class LaunchExecutionResult:
    handoff_id: str
    launch_id: str
    to_agent: str
    launch_status: str
    exit_code: int
    stdout_path: str
    stderr_path: str


@dataclass(frozen=True, slots=True)
class ResumeCommandResult:
    handoff_id: str
    current_agent: str
    phase: str


@dataclass(frozen=True, slots=True)
class StoredLaunchSpec:
    profile: str
    cwd: str
    command: str
    template: str
    template_source: str
    instructions: str


@dataclass(frozen=True, slots=True)
class LoadedHandoff:
    view: DerivedSessionView
    derived_handoff: DerivedHandoffView
    manifest: HandoffManifest
    packet_path: Path
    packet_sha_path: Path
    launch_spec_path: Path
    launch_spec: StoredLaunchSpec


def handoff_id_now() -> str:
    return _id_now("ho")


def launch_id_now() -> str:
    return _id_now("la")


def create_handoff_for_command(
    repo_root: Path,
    session_id: str,
    *,
    to_agent: str,
    reason: str,
    evidence_depth: str,
    owner: str,
) -> HandoffCommandResult:
    _validate_evidence_depth(evidence_depth)
    recover_interrupted_launches(repo_root, session_id, owner=f"{owner}:recover")
    view = load_session_view(repo_root, session_id)
    if view.phase != "ready_for_handoff":
        raise SystemExit(f"failover is not allowed while session phase is {view.phase}")
    if not view.latest_checkpoint_id:
        raise SystemExit("Session has no checkpoint to hand off")

    checkpoint = _load_checkpoint_manifest(repo_root, session_id, view.latest_checkpoint_id)
    prepared_at = utc_now()
    handoff_id = handoff_id_now()
    packet_path = object_dir(repo_root, session_id, "handoff", handoff_id) / "packet.md"
    adapter = get_agent_adapter(to_agent)
    launch_spec = adapter.render_launch_spec(repo_root, packet_path)
    packet_text = _render_resume_packet(
        view,
        checkpoint,
        target_agent=to_agent,
        handoff_reason=reason,
        prepared_at=prepared_at,
        options=ResumeRenderOptions(evidence_depth=evidence_depth),
    )
    packet_sha = sha256_text(packet_text)
    packet_sha_text = packet_sha + "\n"
    launch_spec_text = json.dumps(
        {
            "profile": adapter.display_name,
            "cwd": launch_spec.cwd,
            "command": launch_spec.command,
            "template": launch_spec.template,
            "template_source": launch_spec.template_source,
            "instructions": launch_spec.instructions,
        },
        indent=2,
        sort_keys=True,
    ) + "\n"

    files = (
        _manifest_file_for_text("packet.md", packet_text),
        _manifest_file_for_text("packet.sha256", packet_sha_text),
        _manifest_file_for_text("launch-spec.json", launch_spec_text),
    )
    manifest = HandoffManifest(
        schema_version=SCHEMA_VERSION,
        kind="handoff_manifest",
        object_id=handoff_id,
        session_id=session_id,
        created_at=prepared_at,
        from_agent=view.current_agent,
        to_agent=to_agent,
        reason=reason,
        source_checkpoint_id=checkpoint.object_id,
        source_event_hash=view.last_event_hash,
        launch_profile=adapter.display_name,
        launch_cwd=launch_spec.cwd,
        launch_command=launch_spec.command,
        launch_template=launch_spec.template,
        launch_template_source=launch_spec.template_source,
        launch_instructions=launch_spec.instructions,
        packet_file="packet.md",
        packet_sha256_file="packet.sha256",
        launch_spec_file="launch-spec.json",
        files=files,
    )

    with SessionTransaction.begin(
        repo_root,
        session_id,
        operation=f"handoff:prepare:{handoff_id}",
        owner=owner,
    ) as tx:
        tx.stage_manifest_object(
            manifest,
            file_contents={
                "packet.md": packet_text,
                "packet.sha256": packet_sha_text,
                "launch-spec.json": launch_spec_text,
            },
        )
        tx.commit(
            JournalCommitRequest(
                event_type="handoff.prepared",
                phase_before=view.phase,
                phase_after="ready_for_handoff",
                payload={"handoff_id": handoff_id, "to_agent": to_agent},
                timestamp=prepared_at,
            )
        )

    return HandoffCommandResult(
        handoff_id=handoff_id,
        to_agent=to_agent,
        resume_path=str(packet_path),
        launch_command=launch_spec.command,
        launch_instructions=launch_spec.instructions,
    )


def preview_launch_for_command(
    repo_root: Path,
    session_id: str,
    *,
    handoff_id: str | None,
    owner: str,
) -> LaunchPreviewResult:
    recover_interrupted_launches(repo_root, session_id, owner=f"{owner}:recover")
    loaded = _load_prepared_handoff(repo_root, session_id, handoff_id=handoff_id, command_name="launch")
    if loaded.view.phase != "ready_for_handoff":
        raise SystemExit(f"launch is not allowed while session phase is {loaded.view.phase}")
    return LaunchPreviewResult(
        handoff_id=loaded.manifest.object_id,
        to_agent=loaded.manifest.to_agent,
        resume_path=str(loaded.packet_path),
        launch_command=loaded.launch_spec.command,
        launch_instructions=loaded.launch_spec.instructions,
    )


def execute_launch_for_command(
    repo_root: Path,
    session_id: str,
    *,
    handoff_id: str | None,
    owner: str,
) -> LaunchExecutionResult:
    with acquire_session_lock(repo_root, session_id, owner=owner) as lock:
        recover_session_transactions(repo_root, session_id)
        recovered = _recover_interrupted_launch_locked(repo_root, session_id, owner=owner, lock=lock)
        if recovered:
            recover_session_transactions(repo_root, session_id)
        loaded = _load_prepared_handoff(repo_root, session_id, handoff_id=handoff_id, command_name="launch")
        if loaded.view.phase != "ready_for_handoff":
            raise SystemExit(f"launch is not allowed while session phase is {loaded.view.phase}")

        launch_id = launch_id_now()
        started_at = utc_now()
        with SessionTransaction.begin_with_lock(
            repo_root,
            session_id,
            operation=f"launch:start:{launch_id}",
            owner=owner,
            lock=lock,
        ) as tx:
            tx.commit(
                JournalCommitRequest(
                    event_type="launch.started",
                    phase_before=loaded.view.phase,
                    phase_after="launching",
                    payload={"handoff_id": loaded.manifest.object_id, "launch_id": launch_id},
                    timestamp=started_at,
                )
            )

        status: str
        exit_code: int
        stdout_text = ""
        stderr_text = ""
        try:
            completed = _run_launch_command(loaded.launch_spec.command, cwd=loaded.launch_spec.cwd or str(repo_root))
            status = "succeeded" if completed.returncode == 0 else "failed"
            exit_code = completed.returncode
            stdout_text = completed.stdout or ""
            stderr_text = completed.stderr or ""
        except KeyboardInterrupt:
            status = "interrupted"
            exit_code = 130
            stderr_text = "Launch interrupted by signal.\n"
        except Exception as exc:
            status = "failed"
            exit_code = 1
            stderr_text = f"{type(exc).__name__}: {exc}\n"

        finished_at = utc_now()
        launch_manifest, file_contents = _build_launch_manifest(
            session_id=session_id,
            launch_id=launch_id,
            handoff=loaded.manifest,
            started_at=started_at,
            finished_at=finished_at,
            status=status,
            exit_code=exit_code,
            dispatched_command=loaded.launch_spec.command,
            stdout_text=stdout_text,
            stderr_text=stderr_text,
        )

        with SessionTransaction.begin_with_lock(
            repo_root,
            session_id,
            operation=f"launch:finish:{launch_id}",
            owner=owner,
            lock=lock,
        ) as tx:
            tx.stage_manifest_object(launch_manifest, file_contents=file_contents)
            tx.commit(
                JournalCommitRequest(
                    event_type="launch.finished",
                    phase_before="launching",
                    phase_after="awaiting_resume" if status == "succeeded" else "ready_for_handoff",
                    payload={"handoff_id": loaded.manifest.object_id, "launch_id": launch_id},
                    timestamp=finished_at,
                )
            )

    launch_dir = object_dir(repo_root, session_id, "launch", launch_id)
    return LaunchExecutionResult(
        handoff_id=loaded.manifest.object_id,
        launch_id=launch_id,
        to_agent=loaded.manifest.to_agent,
        launch_status=status,
        exit_code=exit_code,
        stdout_path=str(launch_dir / "stdout.log"),
        stderr_path=str(launch_dir / "stderr.log"),
    )


def resume_handoff_for_command(
    repo_root: Path,
    session_id: str,
    *,
    handoff_id: str | None,
    owner: str,
) -> ResumeCommandResult:
    recover_interrupted_launches(repo_root, session_id, owner=f"{owner}:recover")
    loaded = _load_prepared_handoff(repo_root, session_id, handoff_id=handoff_id, command_name="resume")
    if loaded.view.phase not in {"ready_for_handoff", "awaiting_resume"}:
        raise SystemExit(f"resume is not allowed while session phase is {loaded.view.phase}")
    resumed_at = utc_now()
    with SessionTransaction.begin(
        repo_root,
        session_id,
        operation=f"resume:accept:{loaded.manifest.object_id}",
        owner=owner,
    ) as tx:
        tx.commit(
            JournalCommitRequest(
                event_type="resume.accepted",
                phase_before=loaded.view.phase,
                phase_after="active",
                payload={
                    "handoff_id": loaded.manifest.object_id,
                    "accepted_by_agent": loaded.manifest.to_agent,
                },
                timestamp=resumed_at,
            )
        )
    return ResumeCommandResult(
        handoff_id=loaded.manifest.object_id,
        current_agent=loaded.manifest.to_agent,
        phase="active",
    )


def recover_interrupted_launches(repo_root: Path, session_id: str, *, owner: str) -> str | None:
    with acquire_session_lock(repo_root, session_id, owner=owner) as lock:
        recover_session_transactions(repo_root, session_id)
        return _recover_interrupted_launch_locked(repo_root, session_id, owner=owner, lock=lock)


def _recover_interrupted_launch_locked(repo_root: Path, session_id: str, *, owner: str, lock) -> str | None:
    latest_event = load_latest_journal_event(repo_root, session_id)
    if latest_event.type != "launch.started":
        return None
    handoff_id = latest_event.payload.get("handoff_id")
    launch_id = latest_event.payload.get("launch_id")
    if not isinstance(handoff_id, str) or not isinstance(launch_id, str):
        raise V2CorruptionError("launch.started payload must include handoff_id and launch_id", session_id=session_id)

    handoff = _load_handoff_manifest(repo_root, session_id, handoff_id)
    finished_at = utc_now()
    launch_manifest, file_contents = _build_launch_manifest(
        session_id=session_id,
        launch_id=launch_id,
        handoff=handoff,
        started_at=latest_event.timestamp,
        finished_at=finished_at,
        status="interrupted",
        exit_code=130,
        dispatched_command=handoff.launch_command,
        stdout_text="",
        stderr_text="Launch interrupted before a terminal receipt was recorded; recovered by Agent Relay.\n",
    )

    with SessionTransaction.begin_with_lock(
        repo_root,
        session_id,
        operation=f"launch:recover:{launch_id}",
        owner=owner,
        lock=lock,
    ) as tx:
        current_latest = load_latest_journal_event(repo_root, session_id)
        if current_latest.event_id != latest_event.event_id or current_latest.type != "launch.started":
            return None
        tx.stage_manifest_object(launch_manifest, file_contents=file_contents)
        tx.commit(
            JournalCommitRequest(
                event_type="launch.finished",
                phase_before="launching",
                phase_after="ready_for_handoff",
                payload={"handoff_id": handoff_id, "launch_id": launch_id},
                timestamp=finished_at,
            )
        )
    return launch_id


def _load_checkpoint_manifest(repo_root: Path, session_id: str, checkpoint_id: str) -> CheckpointManifest:
    manifest = load_referenced_object(repo_root, session_id, "checkpoint", checkpoint_id)
    if not isinstance(manifest, CheckpointManifest):
        raise V2CorruptionError("checkpoint id resolved to the wrong manifest type", session_id=session_id)
    return manifest


def _load_handoff_manifest(repo_root: Path, session_id: str, handoff_id: str) -> HandoffManifest:
    manifest = load_referenced_object(repo_root, session_id, "handoff", handoff_id)
    if not isinstance(manifest, HandoffManifest):
        raise V2CorruptionError("handoff id resolved to the wrong manifest type", session_id=session_id)
    return manifest


def _load_prepared_handoff(
    repo_root: Path,
    session_id: str,
    *,
    handoff_id: str | None,
    command_name: str,
) -> LoadedHandoff:
    view = load_session_view(repo_root, session_id)
    selected_handoff_id = handoff_id or view.prepared_handoff_id
    if not selected_handoff_id:
        raise SystemExit(f"No prepared handoff is available for {command_name}")
    derived_handoff = _find_handoff_view(view, selected_handoff_id)
    if view.prepared_handoff_id != selected_handoff_id:
        raise SystemExit(
            f"Handoff {selected_handoff_id} is superseded; current prepared handoff is {view.prepared_handoff_id or 'none'}"
        )
    if derived_handoff.checkpoint_id != view.latest_checkpoint_id:
        raise SystemExit(
            f"Handoff {selected_handoff_id} is stale; latest checkpoint is {view.latest_checkpoint_id or 'none'}"
        )
    manifest = _load_handoff_manifest(repo_root, session_id, selected_handoff_id)
    if manifest.source_checkpoint_id != view.latest_checkpoint_id:
        raise SystemExit(
            f"Handoff {selected_handoff_id} is stale; latest checkpoint is {view.latest_checkpoint_id or 'none'}"
        )
    handoff_dir = object_dir(repo_root, session_id, "handoff", selected_handoff_id)
    packet_path = handoff_dir / manifest.packet_file
    packet_sha_path = handoff_dir / manifest.packet_sha256_file
    launch_spec_path = handoff_dir / manifest.launch_spec_file
    packet_sha_text = packet_sha_path.read_text(encoding="utf-8").strip()
    actual_packet_sha = sha256_path(packet_path)
    if packet_sha_text != actual_packet_sha:
        raise V2CorruptionError("handoff packet sha file does not match packet.md", session_id=session_id, path=packet_sha_path)
    launch_spec = _load_launch_spec_file(
        launch_spec_path,
        expected=StoredLaunchSpec(
            profile=manifest.launch_profile,
            cwd=manifest.launch_cwd,
            command=manifest.launch_command,
            template=manifest.launch_template,
            template_source=manifest.launch_template_source,
            instructions=manifest.launch_instructions,
        ),
        session_id=session_id,
    )
    return LoadedHandoff(
        view=view,
        derived_handoff=derived_handoff,
        manifest=manifest,
        packet_path=packet_path,
        packet_sha_path=packet_sha_path,
        launch_spec_path=launch_spec_path,
        launch_spec=launch_spec,
    )


def _find_handoff_view(view: DerivedSessionView, handoff_id: str) -> DerivedHandoffView:
    for item in view.handoffs:
        if item.handoff_id == handoff_id:
            return item
    raise SystemExit(f"Handoff not found: {handoff_id}")


def _load_launch_spec_file(path: Path, *, expected: StoredLaunchSpec, session_id: str) -> StoredLaunchSpec:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise V2CorruptionError(f"launch-spec.json is not valid JSON: {exc}", session_id=session_id, path=path) from exc
    required = {"profile", "cwd", "command", "template", "template_source", "instructions"}
    if not isinstance(data, dict) or set(data) != required:
        raise V2CorruptionError("launch-spec.json has an unexpected shape", session_id=session_id, path=path)
    spec = StoredLaunchSpec(
        profile=_require_non_empty_str(data["profile"], "launch_spec.profile"),
        cwd=_require_non_empty_str(data["cwd"], "launch_spec.cwd"),
        command=_require_non_empty_str(data["command"], "launch_spec.command"),
        template=_require_non_empty_str(data["template"], "launch_spec.template"),
        template_source=_require_non_empty_str(data["template_source"], "launch_spec.template_source"),
        instructions=_require_non_empty_str(data["instructions"], "launch_spec.instructions"),
    )
    if spec != expected:
        raise V2CorruptionError("launch-spec.json does not match the handoff manifest", session_id=session_id, path=path)
    return spec


def _run_launch_command(command: str, *, cwd: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        shell=True,
        check=False,
        capture_output=True,
        text=True,
    )


def _build_launch_manifest(
    *,
    session_id: str,
    launch_id: str,
    handoff: HandoffManifest,
    started_at: str,
    finished_at: str,
    status: str,
    exit_code: int,
    dispatched_command: str,
    stdout_text: str,
    stderr_text: str,
) -> tuple[LaunchManifest, dict[str, str]]:
    files = (
        _manifest_file_for_text("stdout.log", stdout_text),
        _manifest_file_for_text("stderr.log", stderr_text),
    )
    manifest = LaunchManifest(
        schema_version=SCHEMA_VERSION,
        kind="launch_manifest",
        object_id=launch_id,
        session_id=session_id,
        created_at=finished_at,
        handoff_id=handoff.object_id,
        target_agent=handoff.to_agent,
        started_at=started_at,
        finished_at=finished_at,
        status=status,
        exit_code=exit_code,
        dispatched_command=dispatched_command,
        stdout_file="stdout.log",
        stderr_file="stderr.log",
        files=files,
    )
    return manifest, {"stdout.log": stdout_text, "stderr.log": stderr_text}


def _render_resume_packet(
    view: DerivedSessionView,
    checkpoint: CheckpointManifest,
    *,
    target_agent: str,
    handoff_reason: str,
    prepared_at: str,
    options: ResumeRenderOptions,
) -> str:
    if get_agent_adapter(target_agent).resume_packet_target == "claude":
        title = "# Claude Code Resume Packet"
    else:
        title = "# Codex Resume Packet"

    lines = [
        title,
        "",
        "Resume this Agent Relay session from the immutable state captured below.",
        "",
        "Session snapshot:",
        f"- Objective: {view.objective}",
        f"- Repository root: {view.repo_root}",
        f"- Current phase: {view.phase}",
        f"- Source agent: {get_agent_display_name(view.current_agent)}",
        f"- Prepared at: {prepared_at}",
        f"- Handoff reason: {handoff_reason}",
        "",
        "Latest checkpoint:",
        f"- Checkpoint id: {checkpoint.object_id}",
        f"- Created at: {checkpoint.created_at}",
        f"- Phase hint: {checkpoint.phase_hint}",
        f"- Task status: {checkpoint.task_status}",
        f"- Recorded next action: {checkpoint.next_action or 'Not recorded'}",
        "",
        "Validation:",
        f"- Status: {checkpoint.validation.status}",
        f"- Summary: {checkpoint.validation.summary or 'None recorded'}",
        "",
    ]
    _append_section(lines, "Decisions:", checkpoint.decisions)
    _append_section(lines, "Blockers:", checkpoint.blockers)
    _append_section(lines, "Research notes:", checkpoint.research_notes)
    _append_section(lines, "Implementation notes:", checkpoint.implementation_notes)
    _append_section(lines, "Touched files:", checkpoint.touched_files)
    _append_recent_handoffs(lines, view)
    _append_checkpoint_artifacts(lines, checkpoint, options)
    return "\n".join(lines) + "\n"


def _append_section(lines: list[str], heading: str, items: tuple[str, ...]) -> None:
    lines.append(heading)
    if items:
        lines.extend(f"- {item}" for item in items)
    else:
        lines.append("- None recorded")
    lines.append("")


def _append_recent_handoffs(lines: list[str], view: DerivedSessionView) -> None:
    lines.append("Recent handoffs:")
    recent = list(view.handoffs[-3:])
    if not recent:
        lines.append("- None recorded")
        lines.append("")
        return
    for handoff in recent:
        source = get_agent_display_name(handoff.from_agent)
        target = get_agent_display_name(handoff.to_agent)
        lines.append(f"- {handoff.prepared_at}: {source} -> {target} ({handoff.reason})")
    lines.append("")


def _append_checkpoint_artifacts(lines: list[str], checkpoint: CheckpointManifest, options: ResumeRenderOptions) -> None:
    if options.evidence_depth == "minimal":
        return
    lines.append("Latest checkpoint artifacts:")
    if options.evidence_depth == "standard":
        lines.append(f"- Capture mode: {checkpoint.capture_mode}")
        lines.append(f"- Repo state: {checkpoint.repo_state_file}")
        lines.append(f"- Validation: {checkpoint.validation_file}")
        lines.append(f"- Summary: {checkpoint.summary_file}")
        if checkpoint.git_head_file is not None:
            lines.append(f"- Git head: {checkpoint.git_head_file}")
        if checkpoint.workspace_patch_file is not None:
            lines.append(f"- Workspace patch: {checkpoint.workspace_patch_file}")
        if checkpoint.untracked_manifest_file is not None:
            lines.append(f"- Untracked manifest: {checkpoint.untracked_manifest_file}")
        if checkpoint.snapshot_manifest_file is not None:
            lines.append(f"- Snapshot manifest: {checkpoint.snapshot_manifest_file}")
        lines.append("")
        return
    for file_entry in checkpoint.files:
        lines.append(f"- {file_entry.relative_path}")
    lines.append("")


def _manifest_file_for_text(relative_path: str, content: str) -> ManifestFile:
    encoded = content.encode("utf-8")
    return ManifestFile(relative_path=relative_path, sha256=sha256_text(content), size_bytes=len(encoded))


def _id_now(prefix: str) -> str:
    return datetime.now(UTC).strftime(f"{prefix}-%Y%m%dT%H%M%SZ-") + secrets.token_hex(3)


def _require_non_empty_str(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise V2CorruptionError(f"{field_name} must be a non-empty string")
    return value


def _validate_evidence_depth(value: str) -> None:
    if value not in EVIDENCE_DEPTHS:
        allowed = ", ".join(sorted(EVIDENCE_DEPTHS))
        raise SystemExit(f"resume evidence depth must be one of: {allowed}")


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
