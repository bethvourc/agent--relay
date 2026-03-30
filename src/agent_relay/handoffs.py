from __future__ import annotations

import json
import secrets
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_relay.agents import get_agent_adapter, get_agent_display_name
from agent_relay.resume_options import EVIDENCE_DEPTHS, ResumeRenderOptions
from agent_relay.errors import CorruptionError
from agent_relay.hashing import sha256_path, sha256_text
from agent_relay.integrity import require_session_mutable
from agent_relay.layout import object_dir, turns_dir, workspace_log_path
from agent_relay.lifecycle import (
    LifecycleState,
    LifecycleViolation,
    plan_failover_command,
    plan_launch_finished,
    plan_launch_started,
    plan_resume_command,
)
from agent_relay.locks import acquire_session_lock
from agent_relay.models import (
    CheckpointManifest,
    DerivedHandoffView,
    DerivedSessionView,
    HandoffManifest,
    LaunchManifest,
    ManifestFile,
    SCHEMA_VERSION,
)
from agent_relay.storage import (
    load_latest_journal_event,
    load_referenced_object,
    load_session_view,
)
from agent_relay.resumable_state import load_resumable_state_text
from agent_relay.tx import JournalCommitRequest, SessionTransaction
from agent_relay.workspace_log import WorkspaceLog


@dataclass(frozen=True, slots=True)
class HandoffCommandResult:
    handoff_id: str
    to_agent: str
    resume_path: str
    launch_command: str
    launch_instructions: str
    packet_aware: bool
    execute_policy: str
    warning: str | None = None


@dataclass(frozen=True, slots=True)
class LaunchPreviewResult:
    handoff_id: str
    to_agent: str
    resume_path: str
    launch_command: str
    launch_instructions: str
    packet_aware: bool
    execute_policy: str
    warning: str | None = None


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
    packet_aware: bool
    execute_policy: str
    warning: str | None = None


@dataclass(frozen=True, slots=True)
class LoadedHandoff:
    view: DerivedSessionView
    derived_handoff: DerivedHandoffView
    manifest: HandoffManifest
    packet_path: Path
    packet_sha_path: Path
    launch_spec_path: Path
    launch_spec: StoredLaunchSpec


@dataclass(frozen=True, slots=True)
class RelayArtifact:
    relative_path: str
    content: str


@dataclass(frozen=True, slots=True)
class RelayTurnSummary:
    turn_number: int
    summary: str
    prompt_path: str
    output_path: str
    stderr_path: str | None = None
    excerpt: str | None = None


@dataclass(frozen=True, slots=True)
class RelayResumableState:
    source_label: str
    relative_path: str
    summary: str
    next_step: str | None = None
    remaining_work: tuple[str, ...] = ()
    verification: tuple[str, ...] = ()
    intended_edits: tuple[str, ...] = ()
    raw_text: str | None = None


@dataclass(frozen=True, slots=True)
class RelayConversationContext:
    artifacts: tuple[RelayArtifact, ...]
    turn_summaries: tuple[RelayTurnSummary, ...]
    resumable_states: tuple[RelayResumableState, ...] = ()
    workspace_log_path: str | None = None
    workspace_log_excerpt: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SupplementalCheckpointContext:
    artifacts: tuple[RelayArtifact, ...]
    planning_snapshot: str | None = None
    planning_snapshot_path: str | None = None
    proposed_edits: str | None = None
    proposed_edits_path: str | None = None
    provider_source_agent: str | None = None
    provider_hook_name: str | None = None
    provider_resumable_state: str | None = None
    provider_resumable_state_path: str | None = None
    provider_transcript: str | None = None
    provider_transcript_path: str | None = None
    provider_session_metadata: str | None = None
    provider_session_metadata_path: str | None = None
    provider_warnings: tuple[str, ...] = ()
    provider_warnings_path: str | None = None


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
    require_session_mutable(repo_root, session_id, command_name="failover")
    recover_interrupted_launches(repo_root, session_id, owner=f"{owner}:recover")
    view = load_session_view(repo_root, session_id)
    try:
        transition = plan_failover_command(LifecycleState(phase=view.phase, task_status=view.task_status))
    except LifecycleViolation as exc:
        raise SystemExit(str(exc)) from exc
    if not view.latest_checkpoint_id:
        raise SystemExit("Session has no checkpoint to hand off")

    checkpoint = _load_checkpoint_manifest(repo_root, session_id, view.latest_checkpoint_id)
    prepared_at = utc_now()
    handoff_id = handoff_id_now()
    packet_path = object_dir(repo_root, session_id, "handoff", handoff_id) / "packet.md"
    adapter = get_agent_adapter(to_agent)
    launch_spec = adapter.render_launch_spec(repo_root, packet_path)
    resume_options = ResumeRenderOptions(evidence_depth=evidence_depth)
    supplemental_context = _collect_checkpoint_supplemental_context(
        repo_root,
        session_id,
        checkpoint,
    )
    relay_context = _collect_relay_conversation_context(
        repo_root,
        session_id,
        options=resume_options,
    )
    packet_text = _render_resume_packet(
        view,
        checkpoint,
        target_agent=to_agent,
        handoff_reason=reason,
        prepared_at=prepared_at,
        options=resume_options,
        repo_root=repo_root,
        session_id=session_id,
        supplemental_context=supplemental_context,
        relay_context=relay_context,
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
            "packet_aware": launch_spec.packet_aware,
            "execute_policy": launch_spec.execute_policy,
            "warning": launch_spec.warning,
        },
        indent=2,
        sort_keys=True,
    ) + "\n"

    relay_artifact_files = tuple(
        _manifest_file_for_text(artifact.relative_path, artifact.content)
        for artifact in relay_context.artifacts
    )
    supplemental_artifact_files = tuple(
        _manifest_file_for_text(artifact.relative_path, artifact.content)
        for artifact in supplemental_context.artifacts
    )
    files = (
        _manifest_file_for_text("packet.md", packet_text),
        _manifest_file_for_text("packet.sha256", packet_sha_text),
        _manifest_file_for_text("launch-spec.json", launch_spec_text),
    ) + relay_artifact_files + supplemental_artifact_files
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
        launch_packet_aware=launch_spec.packet_aware,
        launch_execute_policy=launch_spec.execute_policy,
        launch_warning=launch_spec.warning,
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
        file_contents = {
            "packet.md": packet_text,
            "packet.sha256": packet_sha_text,
            "launch-spec.json": launch_spec_text,
        }
        file_contents.update({
            artifact.relative_path: artifact.content
            for artifact in relay_context.artifacts
        })
        file_contents.update({
            artifact.relative_path: artifact.content
            for artifact in supplemental_context.artifacts
        })
        tx.stage_manifest_object(
            manifest,
            file_contents=file_contents,
        )
        tx.commit(
            JournalCommitRequest(
                event_type="handoff.prepared",
                phase_before=transition.phase_before,
                phase_after=transition.phase_after,
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
        packet_aware=launch_spec.packet_aware,
        execute_policy=launch_spec.execute_policy,
        warning=launch_spec.warning,
    )


def preview_launch_for_command(
    repo_root: Path,
    session_id: str,
    *,
    handoff_id: str | None,
    owner: str,
) -> LaunchPreviewResult:
    require_session_mutable(repo_root, session_id, command_name="launch")
    recover_interrupted_launches(repo_root, session_id, owner=f"{owner}:recover")
    loaded = _load_prepared_handoff(repo_root, session_id, handoff_id=handoff_id, command_name="launch")
    try:
        plan_launch_started(LifecycleState(phase=loaded.view.phase, task_status=loaded.view.task_status))
    except LifecycleViolation as exc:
        raise SystemExit(str(exc)) from exc
    return LaunchPreviewResult(
        handoff_id=loaded.manifest.object_id,
        to_agent=loaded.manifest.to_agent,
        resume_path=str(loaded.packet_path),
        launch_command=loaded.launch_spec.command,
        launch_instructions=loaded.launch_spec.instructions,
        packet_aware=loaded.launch_spec.packet_aware,
        execute_policy=loaded.launch_spec.execute_policy,
        warning=loaded.launch_spec.warning,
    )


def execute_launch_for_command(
    repo_root: Path,
    session_id: str,
    *,
    handoff_id: str | None,
    owner: str,
) -> LaunchExecutionResult:
    require_session_mutable(repo_root, session_id, command_name="launch")
    with acquire_session_lock(repo_root, session_id, owner=owner) as lock:
        recovered = _recover_interrupted_launch_locked(repo_root, session_id, owner=owner, lock=lock)
        loaded = _load_prepared_handoff(repo_root, session_id, handoff_id=handoff_id, command_name="launch")
        _require_launch_spec_safe(loaded.launch_spec)
        try:
            started_transition = plan_launch_started(
                LifecycleState(phase=loaded.view.phase, task_status=loaded.view.task_status)
            )
        except LifecycleViolation as exc:
            raise SystemExit(str(exc)) from exc

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
                    phase_before=started_transition.phase_before,
                    phase_after=started_transition.phase_after,
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
        finished_transition = plan_launch_finished(
            LifecycleState(phase="launching", task_status=loaded.view.task_status),
            launch_status=status,
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
                    phase_before=finished_transition.phase_before,
                    phase_after=finished_transition.phase_after,
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
    require_session_mutable(repo_root, session_id, command_name="resume")
    recover_interrupted_launches(repo_root, session_id, owner=f"{owner}:recover")
    loaded = _load_prepared_handoff(repo_root, session_id, handoff_id=handoff_id, command_name="resume")
    try:
        transition = plan_resume_command(LifecycleState(phase=loaded.view.phase, task_status=loaded.view.task_status))
    except LifecycleViolation as exc:
        raise SystemExit(str(exc)) from exc
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
                phase_before=transition.phase_before,
                phase_after=transition.phase_after,
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
        phase=transition.phase_after,
    )


def recover_interrupted_launches(repo_root: Path, session_id: str, *, owner: str) -> str | None:
    require_session_mutable(repo_root, session_id, command_name="launch recovery")
    with acquire_session_lock(repo_root, session_id, owner=owner) as lock:
        return _recover_interrupted_launch_locked(repo_root, session_id, owner=owner, lock=lock)


def _recover_interrupted_launch_locked(repo_root: Path, session_id: str, *, owner: str, lock) -> str | None:
    latest_event = load_latest_journal_event(repo_root, session_id)
    if latest_event.type != "launch.started":
        return None
    handoff_id = latest_event.payload.get("handoff_id")
    launch_id = latest_event.payload.get("launch_id")
    if not isinstance(handoff_id, str) or not isinstance(launch_id, str):
        raise CorruptionError("launch.started payload must include handoff_id and launch_id", session_id=session_id)

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
        finished_transition = plan_launch_finished(
            LifecycleState(phase="launching", task_status=load_session_view(repo_root, session_id).task_status),
            launch_status="interrupted",
        )
        tx.commit(
            JournalCommitRequest(
                event_type="launch.finished",
                phase_before=finished_transition.phase_before,
                phase_after=finished_transition.phase_after,
                payload={"handoff_id": handoff_id, "launch_id": launch_id},
                timestamp=finished_at,
            )
        )
    return launch_id


def _load_checkpoint_manifest(repo_root: Path, session_id: str, checkpoint_id: str) -> CheckpointManifest:
    manifest = load_referenced_object(repo_root, session_id, "checkpoint", checkpoint_id)
    if not isinstance(manifest, CheckpointManifest):
        raise CorruptionError("checkpoint id resolved to the wrong manifest type", session_id=session_id)
    return manifest


def _load_handoff_manifest(repo_root: Path, session_id: str, handoff_id: str) -> HandoffManifest:
    manifest = load_referenced_object(repo_root, session_id, "handoff", handoff_id)
    if not isinstance(manifest, HandoffManifest):
        raise CorruptionError("handoff id resolved to the wrong manifest type", session_id=session_id)
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
        raise CorruptionError("handoff packet sha file does not match packet.md", session_id=session_id, path=packet_sha_path)
    launch_spec = _load_launch_spec_file(
        launch_spec_path,
        expected=StoredLaunchSpec(
            profile=manifest.launch_profile,
            cwd=manifest.launch_cwd,
            command=manifest.launch_command,
            template=manifest.launch_template,
            template_source=manifest.launch_template_source,
            instructions=manifest.launch_instructions,
            packet_aware=manifest.launch_packet_aware,
            execute_policy=manifest.launch_execute_policy,
            warning=manifest.launch_warning,
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
        raise CorruptionError(f"launch-spec.json is not valid JSON: {exc}", session_id=session_id, path=path) from exc
    required = {"profile", "cwd", "command", "template", "template_source", "instructions"}
    optional = {"packet_aware", "execute_policy", "warning"}
    if not isinstance(data, dict) or not required.issubset(data) or not set(data).issubset(required | optional):
        raise CorruptionError("launch-spec.json has an unexpected shape", session_id=session_id, path=path)
    packet_aware = data.get("packet_aware")
    if packet_aware is None:
        packet_aware = expected.packet_aware
    execute_policy = data.get("execute_policy")
    if execute_policy is None:
        execute_policy = expected.execute_policy
    warning = data.get("warning", expected.warning)
    spec = StoredLaunchSpec(
        profile=_require_non_empty_str(data["profile"], "launch_spec.profile"),
        cwd=_require_non_empty_str(data["cwd"], "launch_spec.cwd"),
        command=_require_non_empty_str(data["command"], "launch_spec.command"),
        template=_require_non_empty_str(data["template"], "launch_spec.template"),
        template_source=_require_non_empty_str(data["template_source"], "launch_spec.template_source"),
        instructions=_require_non_empty_str(data["instructions"], "launch_spec.instructions"),
        packet_aware=_require_bool(packet_aware, "launch_spec.packet_aware"),
        execute_policy=_require_execute_policy(execute_policy, "launch_spec.execute_policy"),
        warning=_optional_warning(warning, "launch_spec.warning"),
    )
    if spec != expected:
        raise CorruptionError("launch-spec.json does not match the handoff manifest", session_id=session_id, path=path)
    return spec


def _run_launch_command(command: str, *, cwd: str) -> subprocess.CompletedProcess[str]:
    import sys

    if sys.stdin.isatty():
        # Interactive CLIs (claude, codex) need a real terminal.
        # Inherit stdio so the child process can detect the TTY.
        result = subprocess.run(
            command,
            cwd=cwd,
            shell=True,
            check=False,
        )
        return subprocess.CompletedProcess(
            args=result.args,
            returncode=result.returncode,
            stdout="",
            stderr="",
        )
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
    repo_root: Path,
    session_id: str,
    supplemental_context: SupplementalCheckpointContext,
    relay_context: RelayConversationContext,
) -> str:
    if get_agent_adapter(target_agent).resume_packet_target == "claude":
        title = "# Claude Code Resume Packet"
    else:
        title = "# Codex Resume Packet"

    has_code_changes = bool(checkpoint.touched_files)

    lines = [
        title,
        "",
        "Resume this Agent Relay session from the state captured below.",
        "",
    ]

    # Task briefing — the most important context for the target agent
    if handoff_reason:
        lines.append("## Task")
        lines.append("")
        lines.append(handoff_reason)
        lines.append("")

    if not has_code_changes:
        lines.append(
            "Note: No code changes were made in the previous session. "
            "The prior agent may have been planning, researching, or discussing the approach. "
            "Review the task above and continue from where they left off."
        )
        lines.append("")

    lines.extend([
        "## Session snapshot",
        "",
        f"- Objective: {view.objective}",
        f"- Repository root: {view.repo_root}",
        f"- Current phase: {view.phase}",
        f"- Source agent: {get_agent_display_name(view.current_agent)}",
        f"- Prepared at: {prepared_at}",
        "",
        "## Latest checkpoint",
        "",
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
    ])
    _append_section(lines, "Decisions:", checkpoint.decisions)
    _append_section(lines, "Blockers:", checkpoint.blockers)
    _append_section(lines, "Research notes:", checkpoint.research_notes)
    _append_section(lines, "Implementation notes:", checkpoint.implementation_notes)
    _append_section(lines, "Touched files:", checkpoint.touched_files)
    _append_resumable_state_context(lines, relay_context, supplemental_context, options)
    _append_supplemental_checkpoint_context(lines, supplemental_context, options)
    _append_provider_export_context(lines, supplemental_context, options)
    _append_relay_conversation(lines, relay_context, options)
    _append_recent_handoffs(lines, view)
    _append_checkpoint_artifacts(lines, checkpoint, options, repo_root=repo_root, session_id=session_id)
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


def _append_relay_conversation(
    lines: list[str],
    context: RelayConversationContext,
    options: ResumeRenderOptions,
) -> None:
    if not context.turn_summaries and not context.workspace_log_excerpt:
        return

    lines.append("## Prior Relay Conversation")
    lines.append("")
    lines.append("Agent Relay bundled recent relay-owned conversation artifacts with this handoff.")
    lines.append("")

    if context.turn_summaries:
        lines.append("Recent turns:")
        for turn in context.turn_summaries:
            lines.append(f"- Turn {turn.turn_number}: {turn.summary}")
            artifact_paths = [turn.output_path, turn.prompt_path]
            if turn.stderr_path is not None:
                artifact_paths.append(turn.stderr_path)
            lines.append(f"- Turn {turn.turn_number} artifacts: {', '.join(artifact_paths)}")
            if options.evidence_depth == "full" and turn.excerpt:
                lines.append(f"Turn {turn.turn_number} excerpt:")
                lines.append("```text")
                lines.append(turn.excerpt)
                lines.append("```")
                lines.append("")
        if options.evidence_depth != "full":
            lines.append("")

    if context.workspace_log_excerpt:
        lines.append("Workspace activity excerpt:")
        for item in context.workspace_log_excerpt:
            lines.append(f"- {item}")
        if context.workspace_log_path is not None:
            lines.append(f"- Full workspace log: {context.workspace_log_path}")
        lines.append("")


def _append_resumable_state_context(
    lines: list[str],
    relay_context: RelayConversationContext,
    supplemental_context: SupplementalCheckpointContext,
    options: ResumeRenderOptions,
) -> None:
    if options.evidence_depth == "minimal":
        return
    if supplemental_context.provider_resumable_state is None and not relay_context.resumable_states:
        return

    lines.append("## Resumable State")
    lines.append("")
    lines.append("Structured relay-owned state is preferred over weaker transcript-only context when available.")
    lines.append("")

    if supplemental_context.provider_resumable_state is not None:
        lines.append("Provider-exported resumable state:")
        if supplemental_context.provider_resumable_state_path is not None:
            lines.append(f"- Artifact: {supplemental_context.provider_resumable_state_path}")
        _append_resumable_state_details(
            lines,
            supplemental_context.provider_resumable_state,
            max_chars=3_000 if options.evidence_depth == "standard" else 10_000,
        )

    if relay_context.resumable_states:
        lines.append("Recent relay turn states:")
        for state in relay_context.resumable_states:
            lines.append(f"- {state.source_label}: {state.summary}")
            lines.append(f"- Artifact: {state.relative_path}")
            if state.next_step:
                lines.append(f"- Next step: {state.next_step}")
            if state.remaining_work:
                lines.append(f"- Remaining work: {', '.join(state.remaining_work)}")
            if state.intended_edits:
                lines.append(f"- Intended edits: {', '.join(state.intended_edits)}")
            if state.verification:
                lines.append(f"- Verification: {', '.join(state.verification)}")
            if options.evidence_depth == "full" and state.raw_text is not None:
                lines.append("```json")
                lines.append(_excerpt_relay_text(state.raw_text, max_chars=10_000) or "")
                lines.append("```")
            lines.append("")


def _append_supplemental_checkpoint_context(
    lines: list[str],
    context: SupplementalCheckpointContext,
    options: ResumeRenderOptions,
) -> None:
    if options.evidence_depth == "minimal":
        return
    if context.planning_snapshot is None and context.proposed_edits is None:
        return

    planning_limit = 2_000 if options.evidence_depth == "standard" else 8_000
    proposed_limit = 3_000 if options.evidence_depth == "standard" else 10_000

    lines.append("## Explicit Handoff Inputs")
    lines.append("")

    if context.planning_snapshot is not None:
        lines.append("Planning snapshot:")
        if context.planning_snapshot_path is not None:
            lines.append(f"- Artifact: {context.planning_snapshot_path}")
        lines.append("- This snapshot may describe planning that never became a working-tree diff.")
        lines.append("```text")
        lines.append(_excerpt_relay_text(context.planning_snapshot, max_chars=planning_limit) or "")
        lines.append("```")
        lines.append("")

    if context.proposed_edits is not None:
        lines.append("Captured proposed edits:")
        if context.proposed_edits_path is not None:
            lines.append(f"- Artifact: {context.proposed_edits_path}")
        lines.append("- These edits were captured outside the working tree and may not be applied yet.")
        fence = "diff" if _looks_like_patch(context.proposed_edits) else "text"
        lines.append(f"```{fence}")
        lines.append(_excerpt_relay_text(context.proposed_edits, max_chars=proposed_limit) or "")
        lines.append("```")
        lines.append("")


def _append_provider_export_context(
    lines: list[str],
    context: SupplementalCheckpointContext,
    options: ResumeRenderOptions,
) -> None:
    if options.evidence_depth == "minimal":
        return
    if (
        context.provider_resumable_state is None
        and context.provider_transcript is None
        and context.provider_session_metadata is None
        and not context.provider_warnings
    ):
        return

    transcript_limit = 2_500 if options.evidence_depth == "standard" else 10_000
    metadata_limit = 2_500 if options.evidence_depth == "standard" else 10_000
    source_label = _safe_agent_display_name(context.provider_source_agent or "provider")

    lines.append("## Provider Session Export")
    lines.append("")
    lines.append(f"- Source agent: {source_label}")
    if context.provider_hook_name:
        lines.append(f"- Capture hook: {context.provider_hook_name}")
    lines.append("")

    if context.provider_warnings:
        lines.append("Warnings:")
        for warning in context.provider_warnings:
            lines.append(f"- {warning}")
        if context.provider_warnings_path is not None:
            lines.append(f"- Artifact: {context.provider_warnings_path}")
        lines.append("")

    if context.provider_resumable_state is not None:
        lines.append("Raw provider resumable state artifact:")
        if context.provider_resumable_state_path is not None:
            lines.append(f"- Artifact: {context.provider_resumable_state_path}")
        lines.append("```json")
        lines.append(_excerpt_relay_text(context.provider_resumable_state, max_chars=transcript_limit) or "")
        lines.append("```")
        lines.append("")

    if context.provider_transcript is not None:
        lines.append("Exported transcript:")
        if context.provider_transcript_path is not None:
            lines.append(f"- Artifact: {context.provider_transcript_path}")
        lines.append("```text")
        lines.append(_excerpt_relay_text(context.provider_transcript, max_chars=transcript_limit) or "")
        lines.append("```")
        lines.append("")

    if context.provider_session_metadata is not None:
        lines.append("Exported session metadata:")
        if context.provider_session_metadata_path is not None:
            lines.append(f"- Artifact: {context.provider_session_metadata_path}")
        fence = "json" if _looks_like_json(context.provider_session_metadata) else "text"
        lines.append(f"```{fence}")
        lines.append(_excerpt_relay_text(context.provider_session_metadata, max_chars=metadata_limit) or "")
        lines.append("```")
        lines.append("")


def _collect_relay_conversation_context(
    repo_root: Path,
    session_id: str,
    *,
    options: ResumeRenderOptions,
) -> RelayConversationContext:
    if options.evidence_depth == "minimal":
        return RelayConversationContext(artifacts=(), turn_summaries=())

    if options.evidence_depth == "full":
        max_turns = 6
        max_workspace_entries = 8
    else:
        max_turns = 3
        max_workspace_entries = 4

    artifacts: list[RelayArtifact] = []
    turn_summaries: list[RelayTurnSummary] = []
    resumable_states: list[RelayResumableState] = []

    turns_root = turns_dir(repo_root, session_id)
    if turns_root.exists():
        turn_dirs = sorted(path for path in turns_root.glob("turn-*") if path.is_dir())
        for turn_path in turn_dirs[-max_turns:]:
            turn_number = _parse_turn_number(turn_path.name)
            prompt_file = turn_path / "prompt.md"
            output_file = turn_path / "output.jsonl"
            stderr_file = turn_path / "stderr.log"

            prompt_relative = f"relay/turns/{turn_path.name}/prompt.md"
            output_relative = f"relay/turns/{turn_path.name}/output.jsonl"
            state_relative = f"relay/turns/{turn_path.name}/state.json"
            stderr_relative = (
                f"relay/turns/{turn_path.name}/stderr.log"
                if stderr_file.exists()
                else None
            )

            if prompt_file.exists():
                artifacts.append(RelayArtifact(
                    relative_path=prompt_relative,
                    content=prompt_file.read_text(encoding="utf-8", errors="replace"),
                ))
            if output_file.exists():
                raw_output = output_file.read_text(encoding="utf-8", errors="replace")
                artifacts.append(RelayArtifact(
                    relative_path=output_relative,
                    content=raw_output,
                ))
                normalized_output = _normalize_relay_output(raw_output)
            else:
                raw_output = ""
                normalized_output = ""
            stderr_text = ""
            if stderr_file.exists():
                stderr_text = stderr_file.read_text(encoding="utf-8", errors="replace")
                if stderr_relative is not None:
                    artifacts.append(RelayArtifact(
                        relative_path=stderr_relative,
                        content=stderr_text,
                    ))

            state_file = turn_path / "state.json"
            if state_file.exists():
                state_text = state_file.read_text(encoding="utf-8", errors="replace")
                artifacts.append(RelayArtifact(
                    relative_path=state_relative,
                    content=state_text,
                ))
                state_summary = _build_resumable_state_summary(
                    state_text,
                    source_label=f"Turn {turn_number} — {_safe_agent_display_name(_resumable_state_agent(state_text) or 'agent')}",
                    relative_path=state_relative,
                )
                if state_summary is not None:
                    resumable_states.append(state_summary)

            summary_source = normalized_output or stderr_text or raw_output
            turn_summaries.append(
                RelayTurnSummary(
                    turn_number=turn_number,
                    summary=_summarize_relay_text(summary_source),
                    prompt_path=prompt_relative,
                    output_path=output_relative,
                    stderr_path=stderr_relative,
                    excerpt=_excerpt_relay_text(normalized_output or stderr_text or raw_output),
                )
            )

    workspace_relative_path: str | None = None
    workspace_excerpt: tuple[str, ...] = ()
    log_path = workspace_log_path(repo_root, session_id)
    if log_path.exists():
        workspace_relative_path = "relay/workspace-log.md"
        artifacts.append(RelayArtifact(
            relative_path=workspace_relative_path,
            content=log_path.read_text(encoding="utf-8", errors="replace"),
        ))
        workspace_log = WorkspaceLog(log_path)
        entries = workspace_log.read_all()
        if entries:
            workspace_excerpt = tuple(
                f"[{entry.timestamp}] {_safe_agent_display_name(entry.agent_key)} ({entry.entry_type}): "
                f"{_summarize_relay_text(entry.summary, max_len=120)}"
                for entry in entries[-max_workspace_entries:]
            )
        else:
            workspace_excerpt = tuple(
                _tail_non_empty_lines(
                    log_path.read_text(encoding="utf-8", errors="replace"),
                    max_lines=max_workspace_entries,
                )
            )

    return RelayConversationContext(
        artifacts=tuple(artifacts),
        turn_summaries=tuple(turn_summaries),
        resumable_states=tuple(resumable_states),
        workspace_log_path=workspace_relative_path,
        workspace_log_excerpt=workspace_excerpt,
    )


def _collect_checkpoint_supplemental_context(
    repo_root: Path,
    session_id: str,
    checkpoint: CheckpointManifest,
) -> SupplementalCheckpointContext:
    checkpoint_dir = object_dir(repo_root, session_id, "checkpoint", checkpoint.object_id)
    manifest_path = checkpoint_dir / "captures" / "manifest.json"
    if not manifest_path.exists():
        return SupplementalCheckpointContext(artifacts=())

    try:
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return SupplementalCheckpointContext(artifacts=())
    if not isinstance(manifest_data, dict):
        return SupplementalCheckpointContext(artifacts=())

    artifacts: list[RelayArtifact] = []
    planning_snapshot = _load_checkpoint_capture_text(
        checkpoint_dir,
        manifest_data.get("planning_snapshot_file"),
    )
    proposed_edits = _load_checkpoint_capture_text(
        checkpoint_dir,
        manifest_data.get("proposed_edits_file"),
    )
    provider_resumable_state = _load_checkpoint_capture_text(
        checkpoint_dir,
        manifest_data.get("provider_resumable_state_file"),
    )
    provider_transcript = _load_checkpoint_capture_text(
        checkpoint_dir,
        manifest_data.get("provider_transcript_file"),
    )
    provider_session_metadata = _load_checkpoint_capture_text(
        checkpoint_dir,
        manifest_data.get("provider_session_metadata_file"),
    )
    provider_warnings_text = _load_checkpoint_capture_text(
        checkpoint_dir,
        manifest_data.get("provider_warnings_file"),
    )
    provider_warnings = _parse_warning_bullets(provider_warnings_text)

    planning_snapshot_path = None
    proposed_edits_path = None
    provider_resumable_state_path = None
    provider_transcript_path = None
    provider_session_metadata_path = None
    provider_warnings_path = None
    if planning_snapshot is not None:
        planning_snapshot_path = "relay/inputs/planning-snapshot.md"
        artifacts.append(RelayArtifact(
            relative_path=planning_snapshot_path,
            content=_ensure_trailing_newline(planning_snapshot),
        ))
    if proposed_edits is not None:
        proposed_ext = ".diff" if _looks_like_patch(proposed_edits) else ".md"
        proposed_edits_path = f"relay/inputs/proposed-edits{proposed_ext}"
        artifacts.append(RelayArtifact(
            relative_path=proposed_edits_path,
            content=_ensure_trailing_newline(proposed_edits),
        ))
    if provider_resumable_state is not None:
        provider_source_agent = _capture_manifest_text(manifest_data.get("provider_source_agent"))
        provider_suffix = (provider_source_agent or "provider").replace("/", "-")
        provider_resumable_state_path = f"relay/provider/{provider_suffix}-resumable-state.json"
        artifacts.append(RelayArtifact(
            relative_path=provider_resumable_state_path,
            content=_ensure_trailing_newline(provider_resumable_state),
        ))
    if provider_transcript is not None:
        provider_source_agent = _capture_manifest_text(manifest_data.get("provider_source_agent"))
        provider_suffix = (provider_source_agent or "provider").replace("/", "-")
        provider_transcript_path = f"relay/provider/{provider_suffix}-transcript.md"
        artifacts.append(RelayArtifact(
            relative_path=provider_transcript_path,
            content=_ensure_trailing_newline(provider_transcript),
        ))
    if provider_session_metadata is not None:
        provider_source_agent = _capture_manifest_text(manifest_data.get("provider_source_agent"))
        provider_suffix = (provider_source_agent or "provider").replace("/", "-")
        metadata_ext = ".json" if _looks_like_json(provider_session_metadata) else ".md"
        provider_session_metadata_path = f"relay/provider/{provider_suffix}-session-metadata{metadata_ext}"
        artifacts.append(RelayArtifact(
            relative_path=provider_session_metadata_path,
            content=_ensure_trailing_newline(provider_session_metadata),
        ))
    if provider_warnings:
        provider_source_agent = _capture_manifest_text(manifest_data.get("provider_source_agent"))
        provider_suffix = (provider_source_agent or "provider").replace("/", "-")
        provider_warnings_path = f"relay/provider/{provider_suffix}-warnings.md"
        artifacts.append(RelayArtifact(
            relative_path=provider_warnings_path,
            content=_ensure_trailing_newline("\n".join(f"- {warning}" for warning in provider_warnings)),
        ))

    return SupplementalCheckpointContext(
        artifacts=tuple(artifacts),
        planning_snapshot=planning_snapshot,
        planning_snapshot_path=planning_snapshot_path,
        proposed_edits=proposed_edits,
        proposed_edits_path=proposed_edits_path,
        provider_source_agent=_capture_manifest_text(manifest_data.get("provider_source_agent")),
        provider_hook_name=_capture_manifest_text(manifest_data.get("provider_hook_name")),
        provider_resumable_state=provider_resumable_state,
        provider_resumable_state_path=provider_resumable_state_path,
        provider_transcript=provider_transcript,
        provider_transcript_path=provider_transcript_path,
        provider_session_metadata=provider_session_metadata,
        provider_session_metadata_path=provider_session_metadata_path,
        provider_warnings=provider_warnings,
        provider_warnings_path=provider_warnings_path,
    )


def _load_checkpoint_capture_text(checkpoint_dir: Path, relative_path: object) -> str | None:
    if not isinstance(relative_path, str) or not relative_path.strip():
        return None
    candidate = checkpoint_dir / relative_path
    if not candidate.exists() or not candidate.is_file():
        return None
    return candidate.read_text(encoding="utf-8", errors="replace").strip()


def _capture_manifest_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped if stripped else None


def _append_resumable_state_details(lines: list[str], raw_text: str, *, max_chars: int) -> None:
    state = load_resumable_state_text(raw_text)
    if state is None:
        lines.append("```text")
        lines.append(_excerpt_relay_text(raw_text, max_chars=max_chars) or "")
        lines.append("```")
        lines.append("")
        return
    summary = _capture_manifest_text(state.get("summary"))
    next_step = _capture_manifest_text(state.get("next_step"))
    remaining_work = _parse_string_list(state.get("remaining_work"))
    intended_edits = _parse_string_list(state.get("intended_edits"))
    verification = _parse_string_list(state.get("verification"))
    if summary:
        lines.append(f"- Summary: {summary}")
    if next_step:
        lines.append(f"- Next step: {next_step}")
    if remaining_work:
        lines.append(f"- Remaining work: {', '.join(remaining_work)}")
    if intended_edits:
        lines.append(f"- Intended edits: {', '.join(intended_edits)}")
    if verification:
        lines.append(f"- Verification: {', '.join(verification)}")
    lines.append("```json")
    lines.append(_excerpt_relay_text(raw_text, max_chars=max_chars) or "")
    lines.append("```")
    lines.append("")


def _build_resumable_state_summary(
    raw_text: str,
    *,
    source_label: str,
    relative_path: str,
) -> RelayResumableState | None:
    state = load_resumable_state_text(raw_text)
    if state is None:
        return None
    return RelayResumableState(
        source_label=source_label,
        relative_path=relative_path,
        summary=_capture_manifest_text(state.get("summary")) or "No summary recorded",
        next_step=_capture_manifest_text(state.get("next_step")),
        remaining_work=_parse_string_list(state.get("remaining_work")),
        verification=_parse_string_list(state.get("verification")),
        intended_edits=_parse_string_list(state.get("intended_edits")),
        raw_text=raw_text,
    )


def _parse_string_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    items: list[str] = []
    for item in value:
        if isinstance(item, str):
            stripped = item.strip()
            if stripped:
                items.append(stripped)
    return tuple(items)


def _resumable_state_agent(raw_text: str) -> str | None:
    state = load_resumable_state_text(raw_text)
    if state is None:
        return None
    value = state.get("agent_key")
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped if stripped else None


def _parse_turn_number(dirname: str) -> int:
    suffix = dirname.removeprefix("turn-")
    try:
        return int(suffix)
    except ValueError:
        return 0


def _normalize_relay_output(raw_output: str) -> str:
    texts: list[str] = []
    for raw_line in raw_output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue

        item = event.get("item")
        if event.get("type") == "item.completed" and isinstance(item, dict) and item.get("type") == "agent_message":
            text = item.get("text", "")
            if isinstance(text, str) and text.strip():
                texts.append(text.strip())

        message = event.get("message") if isinstance(event.get("message"), dict) else event
        if isinstance(message, dict) and message.get("role") == "assistant":
            for block in message.get("content", []):
                if isinstance(block, dict) and block.get("type") in {"text", "output_text"}:
                    text = block.get("text", "")
                    if isinstance(text, str) and text.strip():
                        texts.append(text.strip())

    if texts:
        return _strip_relay_control_lines("\n".join(texts))
    return _strip_relay_control_lines(raw_output.strip())


def _strip_relay_control_lines(text: str) -> str:
    kept_lines = [
        line
        for line in text.splitlines()
        if not line.strip().startswith("RELAY_STATUS:")
    ]
    return "\n".join(kept_lines).replace("CONVERSATION_COMPLETE", "").strip()


def _summarize_relay_text(text: str, *, max_len: int = 160) -> str:
    normalized = " ".join(part.strip() for part in text.splitlines() if part.strip())
    if not normalized:
        return "(no assistant output captured)"
    if len(normalized) <= max_len:
        return normalized
    return normalized[: max_len - 3] + "..."


def _excerpt_relay_text(text: str, *, max_chars: int = 1000) -> str | None:
    stripped = text.strip()
    if not stripped:
        return None
    if len(stripped) <= max_chars:
        return stripped
    return stripped[:max_chars] + "\n... (truncated)"


def _tail_non_empty_lines(text: str, *, max_lines: int) -> list[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-max_lines:]


def _parse_warning_bullets(text: str | None) -> tuple[str, ...]:
    if not text:
        return ()
    warnings: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("- "):
            line = line[2:].strip()
        if line:
            warnings.append(line)
    return tuple(warnings)


def _ensure_trailing_newline(text: str) -> str:
    return text if text.endswith("\n") else text + "\n"


def _looks_like_patch(text: str) -> bool:
    stripped = text.lstrip()
    if stripped.startswith("diff --git") or stripped.startswith("--- "):
        return True
    return "\n+++ " in text or "\n@@ " in text


def _looks_like_json(text: str) -> bool:
    stripped = text.lstrip()
    return stripped.startswith("{") or stripped.startswith("[")


def _safe_agent_display_name(agent_key: str) -> str:
    try:
        return get_agent_display_name(agent_key)
    except SystemExit:
        return agent_key


def _append_checkpoint_artifacts(
    lines: list[str],
    checkpoint: CheckpointManifest,
    options: ResumeRenderOptions,
    *,
    repo_root: Path,
    session_id: str,
) -> None:
    if options.evidence_depth == "minimal":
        return

    checkpoint_dir = object_dir(repo_root, session_id, "checkpoint", checkpoint.object_id)

    lines.append("Latest checkpoint artifacts:")
    lines.append(f"- Capture mode: {checkpoint.capture_mode}")
    lines.append("")

    # Include git HEAD info
    if checkpoint.git_head_file is not None:
        git_head_path = checkpoint_dir / checkpoint.git_head_file
        if git_head_path.exists():
            lines.append("Git HEAD:")
            lines.append("```")
            lines.append(git_head_path.read_text(errors="replace").strip())
            lines.append("```")
            lines.append("")

    # Include the actual workspace diff — this is the key content the target agent needs
    if checkpoint.workspace_patch_file is not None:
        patch_path = checkpoint_dir / checkpoint.workspace_patch_file
        if patch_path.exists():
            try:
                patch_text = patch_path.read_text(errors="replace").strip()
            except Exception:
                patch_text = ""
            if patch_text:
                lines.append("Workspace changes (git diff):")
                lines.append("```diff")
                # Cap at 50k chars to avoid overwhelming the target agent
                if len(patch_text) > 50_000:
                    lines.append(patch_text[:50_000])
                    lines.append(f"\n... (truncated, full patch is {len(patch_text)} chars)")
                else:
                    lines.append(patch_text)
                lines.append("```")
                lines.append("")
            else:
                lines.append("Workspace changes: None (clean working tree)")
                lines.append("")

    # Include untracked files list
    if checkpoint.untracked_manifest_file is not None:
        manifest_path = checkpoint_dir / checkpoint.untracked_manifest_file
        if manifest_path.exists():
            try:
                manifest_data = json.loads(manifest_path.read_text())
                untracked_files = manifest_data.get("files", [])
            except Exception:
                untracked_files = []
            if untracked_files:
                lines.append("Untracked files:")
                for entry in untracked_files:
                    rel_path = entry.get("relative_path", entry.get("path", "?"))
                    lines.append(f"- {rel_path}")
                lines.append("")

    if options.evidence_depth == "full":
        lines.append("All checkpoint files:")
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
        raise CorruptionError(f"{field_name} must be a non-empty string")
    return value


def _require_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise CorruptionError(f"{field_name} must be a boolean")
    return value


def _require_execute_policy(value: Any, field_name: str) -> str:
    text = _require_non_empty_str(value, field_name)
    if text not in {"allow", "refuse"}:
        raise CorruptionError(f"{field_name} must be one of: allow, refuse")
    return text


def _optional_warning(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CorruptionError(f"{field_name} must be a string when provided")
    return value


def _require_launch_spec_safe(launch_spec: StoredLaunchSpec) -> None:
    if launch_spec.execute_policy == "allow":
        return
    warning = launch_spec.warning or "Launch template does not pass the resume packet."
    raise SystemExit(warning)


def _validate_evidence_depth(value: str) -> None:
    if value not in EVIDENCE_DEPTHS:
        allowed = ", ".join(sorted(EVIDENCE_DEPTHS))
        raise SystemExit(f"resume evidence depth must be one of: {allowed}")


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
