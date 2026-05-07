from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from agent_relay.capture_support import CaptureOptions
from agent_relay.checkpoints import (
    SupplementalCaptureInputs,
    _build_checkpoint_draft,
    _capture_workspace,
)
from agent_relay.fs import write_json_atomic, write_text_atomic
from agent_relay.hashing import sha256_path, sha256_text
from agent_relay.layout import (
    LAYOUT_VERSION,
    relay_root,
    session_root,
    sessions_root,
    version_path,
)
from agent_relay.lifecycle import LifecycleState, plan_checkpoint_command, plan_session_started
from agent_relay.locks import acquire_repo_lock
from agent_relay.models import (
    SCHEMA_VERSION,
    CheckpointManifest,
    DerivedSessionView,
    JournalEvent,
    ObjectManifest,
    ObjectRef,
    SessionManifest,
    ValidationState,
)
from agent_relay.replay import replay_session


@dataclass(frozen=True, slots=True)
class StartSessionResult:
    session_id: str
    checkpoint_id: str
    session_path: str
    phase: str


def ensure_repo_layout(repo_root: Path) -> None:
    root = relay_root(repo_root)
    root.mkdir(parents=True, exist_ok=True)
    sessions_root(repo_root).mkdir(parents=True, exist_ok=True)
    path = version_path(repo_root)
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if value != LAYOUT_VERSION:
            raise SystemExit(f"Unsupported .agent-relay layout version: {value}")
        return
    write_text_atomic(path, LAYOUT_VERSION + "\n")


def build_journal_event(
    *,
    session_id: str,
    event_id: str,
    sequence: int,
    event_type: str,
    timestamp: str,
    tx_id: str,
    phase_before: str | None,
    phase_after: str,
    payload: Mapping[str, object],
    object_refs: tuple[ObjectRef, ...],
    prev_event_hash: str | None,
) -> JournalEvent:
    event = JournalEvent(
        schema_version=SCHEMA_VERSION,
        kind="journal_event",
        session_id=session_id,
        event_id=event_id,
        sequence=sequence,
        type=event_type,
        timestamp=timestamp,
        tx_id=tx_id,
        phase_before=phase_before,
        phase_after=phase_after,
        payload=dict(payload),
        object_refs=object_refs,
        prev_event_hash=prev_event_hash,
        event_hash="sha256:" + ("0" * 64),
    )
    return JournalEvent.from_dict({**event.to_dict(), "event_hash": event.expected_event_hash()})


def write_object_manifest_tree(
    session_path: Path,
    manifest: ObjectManifest,
    *,
    file_contents: Mapping[str, str | bytes],
) -> ObjectRef:
    object_kind = _object_kind_from_manifest(manifest)
    target_dir = session_path / "objects" / _object_dirname(object_kind) / manifest.object_id
    target_dir.mkdir(parents=True, exist_ok=False)

    expected_paths = {entry.relative_path for entry in manifest.files}
    provided_paths = set(file_contents)
    if provided_paths != expected_paths:
        raise SystemExit("bootstrapped object files must exactly match manifest.files")

    for relative_path, content in file_contents.items():
        path = target_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            write_text_atomic(path, content)

    for file_entry in manifest.files:
        candidate = target_dir / file_entry.relative_path
        if sha256_path(candidate) != file_entry.sha256:
            raise SystemExit(
                f"bootstrapped object file hash mismatch for {file_entry.relative_path}"
            )
        if candidate.stat().st_size != file_entry.size_bytes:
            raise SystemExit(
                f"bootstrapped object file size mismatch for {file_entry.relative_path}"
            )

    manifest_path = target_dir / "manifest.json"
    write_json_atomic(manifest_path, manifest.to_dict())
    return ObjectRef(
        object_kind=object_kind,
        object_id=manifest.object_id,
        manifest_path=str(manifest_path.relative_to(session_path).as_posix()),
        manifest_sha256=sha256_path(manifest_path),
    )


def initialize_session_from_manifest(
    repo_root: Path,
    *,
    manifest: SessionManifest,
    events: list[JournalEvent],
    object_payloads: list[tuple[ObjectManifest, Mapping[str, str | bytes]]],
    owner: str,
) -> None:
    ensure_repo_layout(repo_root)
    final_path = session_root(repo_root, manifest.session_id)
    if final_path.exists():
        raise SystemExit(f"Session already exists: {manifest.session_id}")

    with acquire_repo_lock(repo_root, owner=owner):
        temp_path = Path(
            tempfile.mkdtemp(
                prefix=f".session-{manifest.session_id}-",
                dir=str(sessions_root(repo_root)),
            )
        )
        try:
            _write_session_tree(
                temp_path, manifest=manifest, events=events, object_payloads=object_payloads
            )
            temp_path.rename(final_path)
            _rebuild_and_validate(repo_root, manifest.session_id)
        except BaseException:
            shutil.rmtree(temp_path, ignore_errors=True)
            if final_path.exists():
                shutil.rmtree(final_path, ignore_errors=True)
            raise


def start_session(
    repo_root: Path,
    *,
    session_id: str,
    objective: str,
    workstream_kind: str,
    initial_agent: str,
    next_action: str,
    snapshot_mode: str | None,
    owner: str,
) -> StartSessionResult:
    ensure_repo_layout(repo_root)
    created_at = _utc_now()
    manifest = SessionManifest(
        schema_version=SCHEMA_VERSION,
        kind="session_manifest",
        session_id=session_id,
        repo_root=str(repo_root),
        objective=objective,
        workstream_kind=workstream_kind,
        initial_agent=initial_agent,
        created_at=created_at,
    )
    started_transition = plan_session_started()
    session_started = build_journal_event(
        session_id=session_id,
        event_id="ev-000001",
        sequence=1,
        event_type="session.started",
        timestamp=created_at,
        tx_id=f"tx-start-{session_id}",
        phase_before=started_transition.phase_before,
        phase_after=started_transition.phase_after,
        payload={"session_manifest_sha256": _manifest_hash(manifest)},
        object_refs=tuple(),
        prev_event_hash=None,
    )
    initial_view = DerivedSessionView(
        schema_version=SCHEMA_VERSION,
        kind="derived_session_view",
        session_id=session_id,
        storage_model="journal_v2",
        repo_root=str(repo_root),
        objective=objective,
        workstream_kind=workstream_kind,
        created_at=created_at,
        updated_at=created_at,
        initial_agent=initial_agent,
        current_agent=initial_agent,
        phase="active",
        current_status="active",
        task_status=None,
        next_action=next_action,
        decisions=tuple(),
        blockers=tuple(),
        research_notes=tuple(),
        implementation_notes=tuple(),
        touched_files=tuple(),
        validation=ValidationState(status="not_run", summary=""),
        latest_checkpoint_id=None,
        prepared_handoff_id=None,
        latest_launch_id=None,
        last_resume_handoff_id=None,
        event_count=1,
        last_event_id=session_started.event_id,
        last_event_hash=session_started.event_hash,
        built_from_sequence=1,
        built_from_event_hash=session_started.event_hash,
        health="healthy",
        handoffs=tuple(),
        checkpoint_ids=tuple(),
        launch_ids=tuple(),
        alerts=tuple(),
    )
    transition = plan_checkpoint_command(
        LifecycleState(phase="active", task_status=None),
        command_name="checkpoint",
    )
    options = CaptureOptions(
        status=None,
        snapshot_mode=snapshot_mode,
        next_action=next_action,
        decisions=[],
        blockers=[],
        touched_files=[],
        research_notes=[],
        implementation_notes=[],
        validation_status=None,
        validation_summary=None,
        research_note_file=None,
        implementation_note_file=None,
        validation_summary_file=None,
        capture_git_changes=False,
    )
    draft = _build_checkpoint_draft(
        initial_view,
        options=options,
        command_name="checkpoint",
        transition=transition,
        supplemental=SupplementalCaptureInputs(),
    )
    capture = _capture_workspace(
        repo_root,
        view=initial_view,
        draft=draft,
        command_name="start",
        snapshot_mode=snapshot_mode,
        supplemental=SupplementalCaptureInputs(),
    )
    checkpoint_manifest = CheckpointManifest(
        schema_version=SCHEMA_VERSION,
        kind="checkpoint_manifest",
        object_id=draft.checkpoint_id,
        session_id=session_id,
        created_at=draft.created_at,
        current_agent=draft.current_agent,
        phase_hint=draft.phase_after,
        task_status=draft.task_status,
        capture_mode=capture.capture_mode,
        next_action=draft.next_action,
        decisions=draft.decisions,
        blockers=draft.blockers,
        research_notes=draft.research_notes,
        implementation_notes=draft.implementation_notes,
        touched_files=draft.touched_files,
        validation=draft.validation,
        repo_state_file=capture.repo_state_file,
        validation_file=capture.validation_file,
        summary_file=capture.summary_file,
        git_head_file=capture.git_head_file,
        workspace_patch_file=capture.workspace_patch_file,
        untracked_manifest_file=capture.untracked_manifest_file,
        snapshot_manifest_file=capture.snapshot_manifest_file,
        files=capture.files,
    )
    checkpoint_ref = _preview_object_ref(checkpoint_manifest)
    checkpoint_event = build_journal_event(
        session_id=session_id,
        event_id="ev-000002",
        sequence=2,
        event_type="checkpoint.recorded",
        timestamp=draft.created_at,
        tx_id=f"tx-start-{draft.checkpoint_id}",
        phase_before=transition.phase_before,
        phase_after=transition.phase_after,
        payload={
            "checkpoint_id": draft.checkpoint_id,
            "command_name": "checkpoint",
            "capture_mode": capture.capture_mode,
            "status_directive": transition.status_directive,
        },
        object_refs=(checkpoint_ref,),
        prev_event_hash=session_started.event_hash,
    )
    initialize_session_from_manifest(
        repo_root,
        manifest=manifest,
        events=[session_started, checkpoint_event],
        object_payloads=[(checkpoint_manifest, capture.file_contents)],
        owner=owner,
    )
    return StartSessionResult(
        session_id=session_id,
        checkpoint_id=draft.checkpoint_id,
        session_path=str(session_root(repo_root, session_id) / "session.json"),
        phase=transition.phase_after,
    )


def _write_session_tree(
    session_path: Path,
    *,
    manifest: SessionManifest,
    events: list[JournalEvent],
    object_payloads: list[tuple[ObjectManifest, Mapping[str, str | bytes]]],
) -> None:
    (session_path / "journal").mkdir(parents=True, exist_ok=True)
    (session_path / "refs").mkdir(parents=True, exist_ok=True)
    (session_path / "derived").mkdir(parents=True, exist_ok=True)
    (session_path / "recovery" / "pending-tx").mkdir(parents=True, exist_ok=True)
    (session_path / "recovery" / "quarantine").mkdir(parents=True, exist_ok=True)
    write_json_atomic(session_path / "session.json", manifest.to_dict())
    staged_refs: dict[tuple[str, str], ObjectRef] = {}
    for manifest_object, contents in object_payloads:
        ref = write_object_manifest_tree(session_path, manifest_object, file_contents=contents)
        staged_refs[(ref.object_kind, ref.object_id)] = ref

    previous_hash: str | None = None
    for event in events:
        resolved_refs = tuple(
            staged_refs.get((ref.object_kind, ref.object_id), ref) for ref in event.object_refs
        )
        event = build_journal_event(
            session_id=event.session_id,
            event_id=event.event_id,
            sequence=event.sequence,
            event_type=event.type,
            timestamp=event.timestamp,
            tx_id=event.tx_id,
            phase_before=event.phase_before,
            phase_after=event.phase_after,
            payload=event.payload,
            object_refs=resolved_refs,
            prev_event_hash=previous_hash,
        )
        previous_hash = event.event_hash
        write_json_atomic(
            session_path / "journal" / f"{event.sequence:06d}-{event.type}.json", event.to_dict()
        )


def _rebuild_and_validate(repo_root: Path, session_id: str) -> None:
    manifest = _load_manifest(repo_root, session_id)
    events = _load_events(repo_root, session_id)
    replay_session(
        manifest,
        manifest_hash=_manifest_hash(manifest),
        events=events,
        load_object=lambda ref: _load_object(repo_root, session_id, ref),
    )
    from agent_relay.storage import load_session_view

    load_session_view(repo_root, session_id)


def _manifest_hash(manifest: SessionManifest) -> str:
    from agent_relay.models import build_session_manifest_hash

    return build_session_manifest_hash(manifest)


def _load_manifest(repo_root: Path, session_id: str) -> SessionManifest:
    from agent_relay.storage import load_session_manifest

    return load_session_manifest(repo_root, session_id)


def _load_events(repo_root: Path, session_id: str) -> list[JournalEvent]:
    from agent_relay.storage import load_journal_events

    return load_journal_events(repo_root, session_id)


def _load_object(repo_root: Path, session_id: str, ref: ObjectRef):
    from agent_relay.storage import _load_object_from_ref

    return _load_object_from_ref(session_root(repo_root, session_id), ref)


def _utc_now() -> str:
    from agent_relay.locks import utc_now

    return utc_now()


def _object_kind_from_manifest(manifest: ObjectManifest) -> str:
    if manifest.kind == "checkpoint_manifest":
        return "checkpoint"
    if manifest.kind == "handoff_manifest":
        return "handoff"
    if manifest.kind == "launch_manifest":
        return "launch"
    raise SystemExit(f"Unsupported object manifest kind: {manifest.kind}")


def _object_dirname(object_kind: str) -> str:
    if object_kind == "checkpoint":
        return "checkpoints"
    if object_kind == "handoff":
        return "handoffs"
    if object_kind == "launch":
        return "launches"
    raise SystemExit(f"Unsupported object kind: {object_kind}")


def _preview_object_ref(manifest: ObjectManifest) -> ObjectRef:
    object_kind = _object_kind_from_manifest(manifest)
    relative = Path("objects") / _object_dirname(object_kind) / manifest.object_id / "manifest.json"
    return ObjectRef(
        object_kind=object_kind,
        object_id=manifest.object_id,
        manifest_path=relative.as_posix(),
        manifest_sha256=sha256_text(
            json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n"
        ),
    )
