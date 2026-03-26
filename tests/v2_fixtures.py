from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_relay.v2.hashing import sha256_path, sha256_text
from agent_relay.v2.models import (
    CheckpointManifest,
    HandoffManifest,
    JournalEvent,
    LaunchManifest,
    ManifestFile,
    ObjectRef,
    SCHEMA_VERSION,
    SessionManifest,
    ValidationState,
    build_session_manifest_hash,
)


ZERO_DIGEST = "sha256:" + ("0" * 64)


def build_sample_v2_session(repo_root: Path, *, session_id: str = "20260325-180000-abcd12") -> dict[str, str]:
    relay_root = repo_root / ".agent-relay"
    (relay_root / "sessions" / session_id / "journal").mkdir(parents=True, exist_ok=True)
    (relay_root / "sessions" / session_id / "refs").mkdir(parents=True, exist_ok=True)
    (relay_root / "sessions" / session_id / "derived").mkdir(parents=True, exist_ok=True)
    (relay_root / "sessions" / session_id / "recovery" / "pending-tx").mkdir(parents=True, exist_ok=True)
    (relay_root / "sessions" / session_id / "recovery" / "quarantine").mkdir(parents=True, exist_ok=True)
    (relay_root / "VERSION").write_text("2\n", encoding="utf-8")

    manifest = SessionManifest(
        schema_version=SCHEMA_VERSION,
        kind="session_manifest",
        session_id=session_id,
        repo_root=str(repo_root),
        objective="Migrate the relay session core",
        workstream_kind="mixed",
        initial_agent="claude",
        created_at="2026-03-25T18:00:00Z",
    )
    manifest_path = relay_root / "sessions" / session_id / "session.json"
    _write_json(manifest_path, manifest.to_dict())
    manifest_hash = build_session_manifest_hash(manifest)

    checkpoint_one_id = "cp-20260325T180500Z-111111"
    checkpoint_one_dir = relay_root / "sessions" / session_id / "objects" / "checkpoints" / checkpoint_one_id
    checkpoint_one_summary = checkpoint_one_dir / "summary.md"
    checkpoint_one_artifact = checkpoint_one_dir / "artifacts" / "repo-state.json"
    checkpoint_one_summary.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_one_artifact.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_one_summary.write_text("# Checkpoint One\n", encoding="utf-8")
    checkpoint_one_artifact.write_text("{\"branch\":\"main\"}\n", encoding="utf-8")
    checkpoint_one = CheckpointManifest(
        schema_version=SCHEMA_VERSION,
        kind="checkpoint_manifest",
        object_id=checkpoint_one_id,
        session_id=session_id,
        created_at="2026-03-25T18:05:00Z",
        current_agent="claude",
        phase_hint="active",
        task_status="working",
        next_action="Prepare the first Codex handoff",
        decisions=("Keep journal state canonical",),
        blockers=(),
        research_notes=("Mapped the v2 object model",),
        implementation_notes=("Started the replay engine",),
        touched_files=("src/agent_relay/v2/models.py",),
        validation=ValidationState(status="partial", summary="Replay path still needs verification"),
        summary_file="summary.md",
        files=(
            _file_entry(checkpoint_one_summary),
            _file_entry(checkpoint_one_artifact),
        ),
    )
    checkpoint_one_manifest_path = checkpoint_one_dir / "manifest.json"
    _write_json(checkpoint_one_manifest_path, checkpoint_one.to_dict())

    handoff_id = "ho-20260325T180600Z-222222"
    handoff_dir = relay_root / "sessions" / session_id / "objects" / "handoffs" / handoff_id
    packet_path = handoff_dir / "packet.md"
    packet_path.parent.mkdir(parents=True, exist_ok=True)
    packet_path.write_text("# Codex Resume Packet\n\nContinue from cp-1.\n", encoding="utf-8")
    handoff = HandoffManifest(
        schema_version=SCHEMA_VERSION,
        kind="handoff_manifest",
        object_id=handoff_id,
        session_id=session_id,
        created_at="2026-03-25T18:06:00Z",
        from_agent="claude",
        to_agent="codex",
        reason="Move implementation to Codex",
        source_checkpoint_id=checkpoint_one_id,
        source_event_hash=ZERO_DIGEST,
        launch_profile="Codex",
        launch_cwd=str(repo_root),
        launch_command=f"cd {repo_root} && codex --resume packet.md",
        launch_template="cd {repo_root} && codex --resume {resume_path}",
        launch_template_source="default",
        launch_instructions="Run Codex with the packet path",
        packet_file="packet.md",
        files=(_file_entry(packet_path),),
    )
    handoff_manifest_path = handoff_dir / "manifest.json"
    _write_json(handoff_manifest_path, handoff.to_dict())

    launch_id = "la-20260325T180700Z-333333"
    launch_dir = relay_root / "sessions" / session_id / "objects" / "launches" / launch_id
    stdout_path = launch_dir / "stdout.log"
    stderr_path = launch_dir / "stderr.log"
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_path.write_text("Codex launched successfully\n", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")
    launch = LaunchManifest(
        schema_version=SCHEMA_VERSION,
        kind="launch_manifest",
        object_id=launch_id,
        session_id=session_id,
        created_at="2026-03-25T18:07:30Z",
        handoff_id=handoff_id,
        target_agent="codex",
        started_at="2026-03-25T18:07:00Z",
        finished_at="2026-03-25T18:07:30Z",
        status="succeeded",
        exit_code=0,
        dispatched_command=f"cd {repo_root} && codex --resume packet.md",
        stdout_file="stdout.log",
        stderr_file="stderr.log",
        files=(
            _file_entry(stdout_path),
            _file_entry(stderr_path),
        ),
    )
    launch_manifest_path = launch_dir / "manifest.json"
    _write_json(launch_manifest_path, launch.to_dict())

    checkpoint_two_id = "cp-20260325T180900Z-444444"
    checkpoint_two_dir = relay_root / "sessions" / session_id / "objects" / "checkpoints" / checkpoint_two_id
    checkpoint_two_summary = checkpoint_two_dir / "summary.md"
    checkpoint_two_summary.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_two_summary.write_text("# Checkpoint Two\n", encoding="utf-8")
    checkpoint_two = CheckpointManifest(
        schema_version=SCHEMA_VERSION,
        kind="checkpoint_manifest",
        object_id=checkpoint_two_id,
        session_id=session_id,
        created_at="2026-03-25T18:09:00Z",
        current_agent="codex",
        phase_hint="active",
        task_status="working",
        next_action="Validate the rebuilt inspect path",
        decisions=("Ownership transfers only on resume",),
        blockers=(),
        research_notes=("Confirmed the journal replay order",),
        implementation_notes=("Codex resumed from the immutable packet",),
        touched_files=("src/agent_relay/v2/replay.py",),
        validation=ValidationState(status="passed", summary="Replay and cache rebuild verified"),
        summary_file="summary.md",
        files=(_file_entry(checkpoint_two_summary),),
    )
    checkpoint_two_manifest_path = checkpoint_two_dir / "manifest.json"
    _write_json(checkpoint_two_manifest_path, checkpoint_two.to_dict())

    session_started = _build_event(
        session_id=session_id,
        event_id="ev-000001",
        sequence=1,
        event_type="session.started",
        timestamp="2026-03-25T18:00:00Z",
        tx_id="tx-0001",
        phase_before=None,
        phase_after="active",
        payload={"session_manifest_sha256": manifest_hash},
        object_refs=(),
        prev_event_hash=None,
    )
    checkpoint_one_event = _build_event(
        session_id=session_id,
        event_id="ev-000002",
        sequence=2,
        event_type="checkpoint.recorded",
        timestamp="2026-03-25T18:05:00Z",
        tx_id="tx-0002",
        phase_before="active",
        phase_after="active",
        payload={"checkpoint_id": checkpoint_one_id},
        object_refs=(
            ObjectRef(
                object_kind="checkpoint",
                object_id=checkpoint_one_id,
                manifest_path=_relative_to_session(session_id, checkpoint_one_manifest_path),
                manifest_sha256=sha256_path(checkpoint_one_manifest_path),
            ),
        ),
        prev_event_hash=session_started.event_hash,
    )
    handoff_event = _build_event(
        session_id=session_id,
        event_id="ev-000003",
        sequence=3,
        event_type="handoff.prepared",
        timestamp="2026-03-25T18:06:00Z",
        tx_id="tx-0003",
        phase_before="active",
        phase_after="ready_for_handoff",
        payload={"handoff_id": handoff_id},
        object_refs=(
            ObjectRef(
                object_kind="handoff",
                object_id=handoff_id,
                manifest_path=_relative_to_session(session_id, handoff_manifest_path),
                manifest_sha256=sha256_path(handoff_manifest_path),
            ),
        ),
        prev_event_hash=checkpoint_one_event.event_hash,
    )
    launch_started = _build_event(
        session_id=session_id,
        event_id="ev-000004",
        sequence=4,
        event_type="launch.started",
        timestamp="2026-03-25T18:07:00Z",
        tx_id="tx-0004",
        phase_before="ready_for_handoff",
        phase_after="launching",
        payload={"handoff_id": handoff_id, "launch_id": launch_id},
        object_refs=(),
        prev_event_hash=handoff_event.event_hash,
    )
    launch_finished = _build_event(
        session_id=session_id,
        event_id="ev-000005",
        sequence=5,
        event_type="launch.finished",
        timestamp="2026-03-25T18:07:30Z",
        tx_id="tx-0005",
        phase_before="launching",
        phase_after="awaiting_resume",
        payload={"handoff_id": handoff_id, "launch_id": launch_id},
        object_refs=(
            ObjectRef(
                object_kind="launch",
                object_id=launch_id,
                manifest_path=_relative_to_session(session_id, launch_manifest_path),
                manifest_sha256=sha256_path(launch_manifest_path),
            ),
        ),
        prev_event_hash=launch_started.event_hash,
    )
    resume_event = _build_event(
        session_id=session_id,
        event_id="ev-000006",
        sequence=6,
        event_type="resume.accepted",
        timestamp="2026-03-25T18:08:00Z",
        tx_id="tx-0006",
        phase_before="awaiting_resume",
        phase_after="active",
        payload={"handoff_id": handoff_id},
        object_refs=(),
        prev_event_hash=launch_finished.event_hash,
    )
    checkpoint_two_event = _build_event(
        session_id=session_id,
        event_id="ev-000007",
        sequence=7,
        event_type="checkpoint.recorded",
        timestamp="2026-03-25T18:09:00Z",
        tx_id="tx-0007",
        phase_before="active",
        phase_after="active",
        payload={"checkpoint_id": checkpoint_two_id},
        object_refs=(
            ObjectRef(
                object_kind="checkpoint",
                object_id=checkpoint_two_id,
                manifest_path=_relative_to_session(session_id, checkpoint_two_manifest_path),
                manifest_sha256=sha256_path(checkpoint_two_manifest_path),
            ),
        ),
        prev_event_hash=resume_event.event_hash,
    )

    journal_dir = relay_root / "sessions" / session_id / "journal"
    for event in (
        session_started,
        checkpoint_one_event,
        handoff_event,
        launch_started,
        launch_finished,
        resume_event,
        checkpoint_two_event,
    ):
        _write_json(journal_dir / f"{event.sequence:06d}-{event.type}.json", event.to_dict())

    return {
        "session_id": session_id,
        "checkpoint_one_id": checkpoint_one_id,
        "checkpoint_two_id": checkpoint_two_id,
        "handoff_id": handoff_id,
        "launch_id": launch_id,
        "session_manifest_path": str(manifest_path),
        "handoff_manifest_path": str(handoff_manifest_path),
        "launch_manifest_path": str(launch_manifest_path),
    }


def build_checkpoint_object(
    *,
    session_id: str,
    object_id: str,
    created_at: str,
    current_agent: str,
    next_action: str,
    phase_hint: str = "active",
    task_status: str = "working",
    validation_status: str = "passed",
    validation_summary: str = "Verified in transaction test",
) -> tuple[CheckpointManifest, dict[str, str]]:
    summary_text = f"# {object_id}\n\n{next_action}\n"
    summary_bytes = summary_text.encode("utf-8")
    manifest = CheckpointManifest(
        schema_version=SCHEMA_VERSION,
        kind="checkpoint_manifest",
        object_id=object_id,
        session_id=session_id,
        created_at=created_at,
        current_agent=current_agent,
        phase_hint=phase_hint,
        task_status=task_status,
        next_action=next_action,
        decisions=("Committed through the tx engine",),
        blockers=(),
        research_notes=("Prepared in a staged object dir",),
        implementation_notes=("Promoted only before journal visibility",),
        touched_files=("src/agent_relay/v2/tx.py",),
        validation=ValidationState(status=validation_status, summary=validation_summary),
        summary_file="summary.md",
        files=(
            ManifestFile(
                relative_path="summary.md",
                sha256=sha256_text(summary_text),
                size_bytes=len(summary_bytes),
            ),
        ),
    )
    return manifest, {"summary.md": summary_text}


def _build_event(
    *,
    session_id: str,
    event_id: str,
    sequence: int,
    event_type: str,
    timestamp: str,
    tx_id: str,
    phase_before: str | None,
    phase_after: str,
    payload: dict,
    object_refs: tuple[ObjectRef, ...],
    prev_event_hash: str | None,
) -> JournalEvent:
    provisional = JournalEvent(
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
        payload=payload,
        object_refs=object_refs,
        prev_event_hash=prev_event_hash,
        event_hash=ZERO_DIGEST,
    )
    return JournalEvent(
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
        payload=payload,
        object_refs=object_refs,
        prev_event_hash=prev_event_hash,
        event_hash=provisional.expected_event_hash(),
    )


def _file_entry(path: Path) -> ManifestFile:
    return ManifestFile(
        relative_path=str(path.relative_to(path.parent.parent if path.parent.name == "artifacts" else path.parent).as_posix()),
        sha256=sha256_path(path),
        size_bytes=path.stat().st_size,
    )


def _relative_to_session(session_id: str, path: Path) -> str:
    session_root = path.parents[3]
    return str(path.relative_to(session_root).as_posix())


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
