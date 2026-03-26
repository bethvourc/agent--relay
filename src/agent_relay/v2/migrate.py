from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from agent_relay.fs import write_json_atomic
from agent_relay.models import (
    CheckpointRecord,
    HandoffRecord,
    ModelValidationError,
    SessionState,
    ValidationState as LegacyValidationState,
)
from agent_relay.resume import ResumeRenderOptions, render_resume_packet
from agent_relay.storage import checkpoint_path as legacy_checkpoint_path
from agent_relay.storage import checkpoints_dir as legacy_checkpoints_dir
from agent_relay.storage import session_root as legacy_session_root
from agent_relay.v2.bootstrap import (
    _preview_object_ref,
    _rebuild_and_validate,
    _write_session_tree,
    build_journal_event,
    ensure_v2_repo_layout,
)
from agent_relay.v2.hashing import sha256_bytes, sha256_text
from agent_relay.v2.integrity import inspect_session_integrity
from agent_relay.v2.layout import legacy_migration_records_dir, legacy_v1_dir, session_root
from agent_relay.v2.lifecycle import (
    LifecycleState,
    LifecycleViolation,
    normalize_checkpoint_status_directive,
    plan_checkpoint_command,
    plan_complete_command,
    plan_failover_command,
    plan_launch_finished,
    plan_launch_started,
    plan_resume_command,
    plan_session_started,
)
from agent_relay.v2.locks import acquire_repo_lock, utc_now
from agent_relay.v2.models import (
    CheckpointManifest,
    HandoffManifest,
    LaunchManifest,
    LegacyImportMetadata,
    ManifestFile,
    SCHEMA_VERSION,
    SessionManifest,
    ValidationState,
)
from agent_relay.v2.storage import is_v2_session


@dataclass(frozen=True, slots=True)
class MigrateSessionResult:
    session_id: str
    health: str
    legacy_archive_path: str
    imported_checkpoints: int
    imported_handoffs: int
    imported_launches: int
    alerts: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "command": "migrate",
            "session_id": self.session_id,
            "health": self.health,
            "legacy_archive_path": self.legacy_archive_path,
            "imported_checkpoints": self.imported_checkpoints,
            "imported_handoffs": self.imported_handoffs,
            "imported_launches": self.imported_launches,
            "alerts": list(self.alerts),
        }


@dataclass(slots=True)
class _ImportState:
    phase: str
    task_status: str | None
    current_agent: str
    next_action: str
    last_event_hash: str
    last_sequence: int


@dataclass(frozen=True, slots=True)
class _LegacyContext:
    state: SessionState | None
    raw_state: Mapping[str, Any] | None
    session_path: Path
    objective: str
    workstream_kind: str
    initial_agent: str
    current_status: str
    next_action: str
    validation: ValidationState
    latest_checkpoint_id: str | None
    handoffs: tuple[HandoffRecord, ...]
    created_at: str
    alerts: tuple[str, ...]
    broken_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ImportPlan:
    manifest: SessionManifest
    events: list[Any]
    object_payloads: list[tuple[Any, Mapping[str, str | bytes]]]
    alerts: tuple[str, ...]
    broken_paths: tuple[str, ...]
    checkpoint_count: int
    handoff_count: int
    launch_count: int


@dataclass(frozen=True, slots=True)
class _MigrationSwapRecord:
    session_id: str
    staging_root: str
    backup_root: str
    final_path: str
    recorded_at: str
    state: str

    def to_dict(self) -> dict[str, str]:
        return {
            "schema_version": "2",
            "kind": "legacy_migration_swap",
            "session_id": self.session_id,
            "staging_root": self.staging_root,
            "backup_root": self.backup_root,
            "final_path": self.final_path,
            "recorded_at": self.recorded_at,
            "state": self.state,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> _MigrationSwapRecord:
        return cls(
            session_id=str(payload["session_id"]),
            staging_root=str(payload["staging_root"]),
            backup_root=str(payload["backup_root"]),
            final_path=str(payload["final_path"]),
            recorded_at=str(payload["recorded_at"]),
            state=str(payload["state"]),
        )


def is_legacy_v1_session(repo_root: Path, session_id: str) -> bool:
    path = legacy_session_root(repo_root, session_id)
    return path.exists() and not is_v2_session(repo_root, session_id)


def migrate_legacy_session(
    repo_root: Path,
    session_id: str,
    *,
    owner: str,
) -> MigrateSessionResult:
    if is_v2_session(repo_root, session_id):
        raise SystemExit(f"Session already uses v2 storage: {session_id}")
    legacy_root = legacy_session_root(repo_root, session_id)
    if not legacy_root.exists():
        raise SystemExit(f"Session not found: {session_id}")

    ensure_v2_repo_layout(repo_root)
    with acquire_repo_lock(repo_root, owner=owner):
        _recover_legacy_migrations_locked(repo_root)
        plan = _build_import_plan(repo_root, session_id)
        final_path = session_root(repo_root, session_id)
        if final_path.exists() and final_path != legacy_root:
            raise SystemExit(f"Session already exists: {session_id}")

        staging_root = Path(
            tempfile.mkdtemp(
                prefix=f".migrate-{session_id}-",
                dir=str(final_path.parent),
            )
        )
        backup_root = legacy_root.parent / f".legacy-{session_id}-{utc_now().replace(':', '').replace('-', '')}"
        record_path = _migration_record_path(repo_root, session_id)
        try:
            _write_session_tree(
                staging_root,
                manifest=plan.manifest,
                events=plan.events,
                object_payloads=plan.object_payloads,
            )
            _write_migration_record(
                record_path,
                _MigrationSwapRecord(
                    session_id=session_id,
                    staging_root=str(staging_root),
                    backup_root=str(backup_root),
                    final_path=str(final_path),
                    recorded_at=utc_now(),
                    state="staged",
                ),
            )
            shutil.copytree(legacy_root, staging_root / "legacy-v1")
            legacy_root.rename(backup_root)
            _write_migration_record(
                record_path,
                _MigrationSwapRecord(
                    session_id=session_id,
                    staging_root=str(staging_root),
                    backup_root=str(backup_root),
                    final_path=str(final_path),
                    recorded_at=utc_now(),
                    state="backup_swapped",
                ),
            )
            staging_root.rename(final_path)
            _rebuild_and_validate(repo_root, session_id)
            shutil.rmtree(backup_root, ignore_errors=True)
            record_path.unlink(missing_ok=True)
        except BaseException:
            if backup_root.exists():
                if final_path.exists():
                    shutil.rmtree(final_path, ignore_errors=True)
                backup_root.rename(legacy_root)
            shutil.rmtree(staging_root, ignore_errors=True)
            record_path.unlink(missing_ok=True)
            raise

    report = inspect_session_integrity(repo_root, session_id).report
    return MigrateSessionResult(
        session_id=session_id,
        health=report.health,
        legacy_archive_path=str(legacy_v1_dir(repo_root, session_id)),
        imported_checkpoints=plan.checkpoint_count,
        imported_handoffs=plan.handoff_count,
        imported_launches=plan.launch_count,
        alerts=plan.alerts,
    )


def recover_legacy_migrations(repo_root: Path, *, owner: str) -> None:
    records_dir = legacy_migration_records_dir(repo_root)
    if not records_dir.exists():
        return
    with acquire_repo_lock(repo_root, owner=f"{owner}:recover"):
        _recover_legacy_migrations_locked(repo_root)


def _recover_legacy_migrations_locked(repo_root: Path) -> None:
    records_dir = legacy_migration_records_dir(repo_root)
    if not records_dir.exists():
        return
    for path in sorted(records_dir.glob("*.json")):
        try:
            record = _MigrationSwapRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            path.unlink(missing_ok=True)
            continue
        staging_root = Path(record.staging_root)
        backup_root = Path(record.backup_root)
        final_path = Path(record.final_path)
        if backup_root.exists() and not final_path.exists():
            backup_root.rename(final_path)
        elif backup_root.exists() and final_path.exists():
            shutil.rmtree(backup_root, ignore_errors=True)
        if staging_root.exists():
            shutil.rmtree(staging_root, ignore_errors=True)
        path.unlink(missing_ok=True)


def _build_import_plan(repo_root: Path, session_id: str) -> _ImportPlan:
    context = _load_legacy_context(repo_root, session_id)
    started_at = context.created_at or utc_now()
    manifest = SessionManifest(
        schema_version=SCHEMA_VERSION,
        kind="session_manifest",
        session_id=session_id,
        repo_root=str(repo_root),
        objective=context.objective,
        workstream_kind=context.workstream_kind,
        initial_agent=context.initial_agent,
        created_at=started_at,
        legacy_import=LegacyImportMetadata(
            source_schema_version=1,
            imported_at=utc_now(),
            raw_archive_dir="legacy-v1",
            import_health="degraded" if context.alerts else "healthy",
            alerts=context.alerts,
            broken_paths=context.broken_paths,
        ),
    )
    started = build_journal_event(
        session_id=session_id,
        event_id="ev-000001",
        sequence=1,
        event_type="session.started",
        timestamp=started_at,
        tx_id=f"tx-migrate-{session_id}-0001",
        phase_before=plan_session_started().phase_before,
        phase_after=plan_session_started().phase_after,
        payload={"session_manifest_sha256": _manifest_hash(manifest)},
        object_refs=tuple(),
        prev_event_hash=None,
    )
    import_state = _ImportState(
        phase="active",
        task_status=None,
        current_agent=context.initial_agent,
        next_action=context.next_action,
        last_event_hash=started.event_hash,
        last_sequence=1,
    )
    events: list[Any] = [started]
    object_payloads: list[tuple[Any, Mapping[str, str | bytes]]] = []
    alerts = list(context.alerts)
    broken_paths = list(context.broken_paths)

    checkpoints, checkpoint_raw = _load_legacy_checkpoints(repo_root, session_id, alerts, broken_paths)
    handoffs_by_checkpoint = _handoffs_by_checkpoint(context.handoffs)
    checkpoint_count = 0
    handoff_count = 0
    launch_count = 0

    if context.latest_checkpoint_id and context.latest_checkpoint_id not in checkpoint_raw:
        alerts.append(
            f"legacy latest_checkpoint_id {context.latest_checkpoint_id} could not be imported; raw archive preserved under legacy-v1/"
        )
        broken_paths.append(str(legacy_checkpoint_path(repo_root, session_id, context.latest_checkpoint_id)))

    for checkpoint in checkpoints:
        checkpoint_alerts_before = len(alerts)
        command_name, status_directive = _checkpoint_import_mode(
            checkpoint.status,
            has_handoffs=checkpoint.checkpoint_id in handoffs_by_checkpoint,
            alerts=alerts,
        )
        transition = _plan_checkpoint_transition(
            import_state,
            command_name=command_name,
            status_directive=status_directive,
            alerts=alerts,
        )
        manifest_object, file_contents = _build_checkpoint_manifest(
            session_id=session_id,
            checkpoint=checkpoint,
            raw_checkpoint=checkpoint_raw[checkpoint.checkpoint_id],
            current_agent=import_state.current_agent,
            phase_after=transition.phase_after,
            task_status=transition.task_status_after or "working",
            repo_root=repo_root,
        )
        object_payloads.append((manifest_object, file_contents))
        ref = _preview_object_ref(manifest_object)
        event = build_journal_event(
            session_id=session_id,
            event_id=f"ev-{import_state.last_sequence + 1:06d}",
            sequence=import_state.last_sequence + 1,
            event_type="checkpoint.recorded",
            timestamp=checkpoint.created_at,
            tx_id=f"tx-migrate-{session_id}-{import_state.last_sequence + 1:04d}",
            phase_before=transition.phase_before,
            phase_after=transition.phase_after,
            payload={
                "checkpoint_id": checkpoint.checkpoint_id,
                "command_name": command_name,
                "capture_mode": "snapshot",
                "status_directive": normalize_checkpoint_status_directive(status_directive),
            },
            object_refs=(ref,),
            prev_event_hash=import_state.last_event_hash,
        )
        events.append(event)
        import_state = _ImportState(
            phase=transition.phase_after,
            task_status=transition.task_status_after,
            current_agent=import_state.current_agent,
            next_action=checkpoint.next_action,
            last_event_hash=event.event_hash,
            last_sequence=event.sequence,
        )
        checkpoint_count += 1

        for index, handoff in enumerate(handoffs_by_checkpoint.get(checkpoint.checkpoint_id, tuple()), start=1):
            handoff_manifest, handoff_files = _build_handoff_manifest(
                repo_root=repo_root,
                session_id=session_id,
                handoff=handoff,
                checkpoint=checkpoint,
                state=context.state,
                checkpoint_count=checkpoint_count,
                handoff_index=handoff_count + 1,
                source_event_hash=import_state.last_event_hash,
                alerts=alerts,
                broken_paths=broken_paths,
            )
            transition_failover = _plan_failover(import_state)
            object_payloads.append((handoff_manifest, handoff_files))
            handoff_event = build_journal_event(
                session_id=session_id,
                event_id=f"ev-{import_state.last_sequence + 1:06d}",
                sequence=import_state.last_sequence + 1,
                event_type="handoff.prepared",
                timestamp=handoff.prepared_at,
                tx_id=f"tx-migrate-{session_id}-{import_state.last_sequence + 1:04d}",
                phase_before=transition_failover.phase_before,
                phase_after=transition_failover.phase_after,
                payload={"handoff_id": handoff_manifest.object_id, "to_agent": handoff.to_agent},
                object_refs=(_preview_object_ref(handoff_manifest),),
                prev_event_hash=import_state.last_event_hash,
            )
            events.append(handoff_event)
            import_state = _ImportState(
                phase=transition_failover.phase_after,
                task_status=transition_failover.task_status_after,
                current_agent=import_state.current_agent,
                next_action=checkpoint.next_action,
                last_event_hash=handoff_event.event_hash,
                last_sequence=handoff_event.sequence,
            )
            handoff_count += 1

            if handoff.launch_status == "ready":
                continue

            mapped_launch_status = _map_legacy_launch_status(handoff.launch_status, alerts)
            transition_launch = _plan_launch_started(import_state)
            launch_id = f"la-import-{handoff_count:04d}"
            launch_started = build_journal_event(
                session_id=session_id,
                event_id=f"ev-{import_state.last_sequence + 1:06d}",
                sequence=import_state.last_sequence + 1,
                event_type="launch.started",
                timestamp=handoff.launched_at or handoff.prepared_at,
                tx_id=f"tx-migrate-{session_id}-{import_state.last_sequence + 1:04d}",
                phase_before=transition_launch.phase_before,
                phase_after=transition_launch.phase_after,
                payload={"handoff_id": handoff_manifest.object_id, "launch_id": launch_id},
                object_refs=tuple(),
                prev_event_hash=import_state.last_event_hash,
            )
            events.append(launch_started)
            import_state = _ImportState(
                phase=transition_launch.phase_after,
                task_status=transition_launch.task_status_after,
                current_agent=import_state.current_agent,
                next_action=checkpoint.next_action,
                last_event_hash=launch_started.event_hash,
                last_sequence=launch_started.sequence,
            )

            launch_manifest, launch_files = _build_launch_manifest(
                session_id=session_id,
                handoff=handoff,
                handoff_id=handoff_manifest.object_id,
                launch_id=launch_id,
                status=mapped_launch_status,
            )
            transition_finished = _plan_launch_finished(import_state, mapped_launch_status)
            object_payloads.append((launch_manifest, launch_files))
            launch_finished = build_journal_event(
                session_id=session_id,
                event_id=f"ev-{import_state.last_sequence + 1:06d}",
                sequence=import_state.last_sequence + 1,
                event_type="launch.finished",
                timestamp=launch_manifest.created_at,
                tx_id=f"tx-migrate-{session_id}-{import_state.last_sequence + 1:04d}",
                phase_before=transition_finished.phase_before,
                phase_after=transition_finished.phase_after,
                payload={"handoff_id": handoff_manifest.object_id, "launch_id": launch_id},
                object_refs=(_preview_object_ref(launch_manifest),),
                prev_event_hash=import_state.last_event_hash,
            )
            events.append(launch_finished)
            import_state = _ImportState(
                phase=transition_finished.phase_after,
                task_status=transition_finished.task_status_after,
                current_agent=import_state.current_agent,
                next_action=checkpoint.next_action,
                last_event_hash=launch_finished.event_hash,
                last_sequence=launch_finished.sequence,
            )
            launch_count += 1

            if mapped_launch_status == "succeeded":
                transition_resume = _plan_resume(import_state)
                resume_event = build_journal_event(
                    session_id=session_id,
                    event_id=f"ev-{import_state.last_sequence + 1:06d}",
                    sequence=import_state.last_sequence + 1,
                    event_type="resume.accepted",
                    timestamp=launch_manifest.created_at,
                    tx_id=f"tx-migrate-{session_id}-{import_state.last_sequence + 1:04d}",
                    phase_before=transition_resume.phase_before,
                    phase_after=transition_resume.phase_after,
                    payload={
                        "handoff_id": handoff_manifest.object_id,
                        "accepted_by_agent": handoff.to_agent,
                    },
                    object_refs=tuple(),
                    prev_event_hash=import_state.last_event_hash,
                )
                events.append(resume_event)
                import_state = _ImportState(
                    phase=transition_resume.phase_after,
                    task_status=transition_resume.task_status_after,
                    current_agent=handoff.to_agent,
                    next_action=checkpoint.next_action,
                    last_event_hash=resume_event.event_hash,
                    last_sequence=resume_event.sequence,
                )

        if len(alerts) != checkpoint_alerts_before:
            import_state.next_action = checkpoint.next_action

    if context.current_status == "completed":
        try:
            complete_transition = plan_complete_command(
                LifecycleState(phase=import_state.phase, task_status=import_state.task_status)
            )
            completed_event = build_journal_event(
                session_id=session_id,
                event_id=f"ev-{import_state.last_sequence + 1:06d}",
                sequence=import_state.last_sequence + 1,
                event_type="session.completed",
                timestamp=context.state.updated_at if context.state is not None else utc_now(),
                tx_id=f"tx-migrate-{session_id}-{import_state.last_sequence + 1:04d}",
                phase_before=complete_transition.phase_before,
                phase_after=complete_transition.phase_after,
                payload={"completed_by_agent": import_state.current_agent},
                object_refs=tuple(),
                prev_event_hash=import_state.last_event_hash,
            )
            events.append(completed_event)
            import_state = _ImportState(
                phase=complete_transition.phase_after,
                task_status=complete_transition.task_status_after,
                current_agent=import_state.current_agent,
                next_action=import_state.next_action,
                last_event_hash=completed_event.event_hash,
                last_sequence=completed_event.sequence,
            )
        except LifecycleViolation:
            alerts.append("legacy completed session could not be imported as a terminal v2 phase; preserved raw archive for review")

    if context.state is not None and import_state.current_agent != context.state.current_agent:
        alerts.append(
            f"legacy current_agent {context.state.current_agent} did not match imported agent {import_state.current_agent}"
        )
    if context.current_status in {"launching", "launch_failed"}:
        alerts.append(
            f"legacy status {context.current_status} was normalized to v2 relay semantics during migration"
        )
    manifest = SessionManifest(
        schema_version=SCHEMA_VERSION,
        kind="session_manifest",
        session_id=session_id,
        repo_root=str(repo_root),
        objective=context.objective,
        workstream_kind=context.workstream_kind,
        initial_agent=context.initial_agent,
        created_at=started_at,
        legacy_import=LegacyImportMetadata(
            source_schema_version=1,
            imported_at=utc_now(),
            raw_archive_dir="legacy-v1",
            import_health="degraded" if alerts else "healthy",
            alerts=tuple(alerts),
            broken_paths=tuple(dict.fromkeys(broken_paths)),
        ),
    )
    if events:
        events[0] = build_journal_event(
            session_id=session_id,
            event_id="ev-000001",
            sequence=1,
            event_type="session.started",
            timestamp=started_at,
            tx_id=f"tx-migrate-{session_id}-0001",
            phase_before=plan_session_started().phase_before,
            phase_after=plan_session_started().phase_after,
            payload={"session_manifest_sha256": _manifest_hash(manifest)},
            object_refs=tuple(),
            prev_event_hash=None,
        )
        _rechain_events(events)
        _refresh_handoff_anchors(events, object_payloads)

    return _ImportPlan(
        manifest=manifest,
        events=events,
        object_payloads=object_payloads,
        alerts=tuple(alerts),
        broken_paths=tuple(dict.fromkeys(broken_paths)),
        checkpoint_count=checkpoint_count,
        handoff_count=handoff_count,
        launch_count=launch_count,
    )


def _load_legacy_context(repo_root: Path, session_id: str) -> _LegacyContext:
    session_path = legacy_session_root(repo_root, session_id)
    state_path = session_path / "state.json"
    alerts: list[str] = []
    broken_paths: list[str] = []
    raw_state: Mapping[str, Any] | None = None
    state: SessionState | None = None

    if state_path.exists():
        try:
            raw = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(raw, Mapping):
                raw_state = raw
            else:
                alerts.append("legacy state.json was not an object; importing with conservative defaults")
                broken_paths.append(str(state_path))
        except json.JSONDecodeError as exc:
            alerts.append(f"legacy state.json is invalid JSON: {exc}")
            broken_paths.append(str(state_path))
        else:
            try:
                state = SessionState.from_dict(raw_state)
            except ModelValidationError as exc:
                alerts.append(f"legacy state.json failed validation: {exc}")
                broken_paths.append(str(state_path))
    else:
        alerts.append("legacy state.json is missing")
        broken_paths.append(str(state_path))

    handoffs = tuple(state.handoffs) if state is not None else _parse_raw_handoffs(raw_state, alerts, broken_paths, state_path)
    objective = _mapping_str(raw_state, "objective") or (state.objective if state is not None else "")
    if not objective:
        objective = "Legacy v1 session import requires review"
        alerts.append("legacy objective was missing; imported a placeholder objective")
    workstream_kind = _mapping_choice(raw_state, "workstream_kind", {"research", "implementation", "mixed"}) or (
        state.workstream_kind if state is not None else "mixed"
    )
    current_agent = _mapping_choice(raw_state, "current_agent", {"claude", "codex"}) or (
        state.current_agent if state is not None else None
    )
    if current_agent is None and handoffs:
        current_agent = handoffs[0].from_agent
    if current_agent is None:
        current_agent = "claude"
        alerts.append("legacy current_agent was missing; defaulted to claude for migration")
    created_at = _mapping_str(raw_state, "created_at") or (state.created_at if state is not None else utc_now())
    current_status = _mapping_str(raw_state, "current_status") or (state.current_status if state is not None else "active")
    next_action = _mapping_str(raw_state, "next_action") or (state.next_action if state is not None else "")
    validation = _parse_validation(raw_state, state)
    latest_checkpoint_id = _mapping_str(raw_state, "latest_checkpoint_id") or (state.latest_checkpoint_id if state is not None else None)

    return _LegacyContext(
        state=state,
        raw_state=raw_state,
        session_path=session_path,
        objective=objective,
        workstream_kind=workstream_kind,
        initial_agent=handoffs[0].from_agent if handoffs else current_agent,
        current_status=current_status,
        next_action=next_action,
        validation=validation,
        latest_checkpoint_id=latest_checkpoint_id,
        handoffs=handoffs,
        created_at=created_at,
        alerts=tuple(alerts),
        broken_paths=tuple(dict.fromkeys(broken_paths)),
    )


def _load_legacy_checkpoints(
    repo_root: Path,
    session_id: str,
    alerts: list[str],
    broken_paths: list[str],
) -> tuple[list[CheckpointRecord], dict[str, Mapping[str, Any]]]:
    loaded: list[CheckpointRecord] = []
    raw_by_id: dict[str, Mapping[str, Any]] = {}
    directory = legacy_checkpoints_dir(repo_root, session_id)
    if not directory.exists():
        alerts.append("legacy checkpoints directory is missing")
        broken_paths.append(str(directory))
        return loaded, raw_by_id
    for checkpoint_file in sorted(directory.glob("*.json")):
        try:
            raw = json.loads(checkpoint_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            alerts.append(f"legacy checkpoint {checkpoint_file.name} is invalid JSON: {exc}")
            broken_paths.append(str(checkpoint_file))
            continue
        try:
            checkpoint = CheckpointRecord.from_dict(raw)
        except ModelValidationError as exc:
            alerts.append(f"legacy checkpoint {checkpoint_file.name} failed validation: {exc}")
            broken_paths.append(str(checkpoint_file))
            continue
        loaded.append(checkpoint)
        raw_by_id[checkpoint.checkpoint_id] = raw
    loaded.sort(key=lambda item: (item.created_at, item.checkpoint_id))
    return loaded, raw_by_id


def _handoffs_by_checkpoint(handoffs: tuple[HandoffRecord, ...]) -> dict[str, tuple[HandoffRecord, ...]]:
    grouped: dict[str, list[HandoffRecord]] = {}
    for handoff in handoffs:
        grouped.setdefault(handoff.checkpoint_id, []).append(handoff)
    return {
        checkpoint_id: tuple(sorted(items, key=lambda item: (item.prepared_at, item.to_agent, item.reason)))
        for checkpoint_id, items in grouped.items()
    }


def _checkpoint_import_mode(
    status: str,
    *,
    has_handoffs: bool,
    alerts: list[str],
) -> tuple[str, str | None]:
    if has_handoffs or status == "handoff_prepared":
        return "prepare", None
    if status == "active":
        return "checkpoint", "active"
    if status == "paused":
        return "checkpoint", "paused"
    if status == "blocked":
        return "checkpoint", "blocked"
    if status == "completed":
        return "checkpoint", "done"
    if status in {"launch_failed", "launching"}:
        alerts.append(f"legacy checkpoint status {status} was normalized to ready_for_handoff during migration")
        return "prepare", None
    alerts.append(f"legacy checkpoint status {status} was normalized to active during migration")
    return "checkpoint", "active"


def _plan_checkpoint_transition(import_state: _ImportState, *, command_name: str, status_directive: str | None, alerts: list[str]):
    try:
        return plan_checkpoint_command(
            LifecycleState(phase=import_state.phase, task_status=import_state.task_status),
            command_name=command_name,
            status_directive=status_directive,
        )
    except LifecycleViolation as exc:
        alerts.append(f"checkpoint import was normalized from phase {import_state.phase}: {exc}")
        fallback_command = "checkpoint"
        fallback_directive = "active"
        return plan_checkpoint_command(
            LifecycleState(phase="active", task_status=import_state.task_status),
            command_name=fallback_command,
            status_directive=fallback_directive,
        )


def _build_checkpoint_manifest(
    *,
    session_id: str,
    checkpoint: CheckpointRecord,
    raw_checkpoint: Mapping[str, Any],
    current_agent: str,
    phase_after: str,
    task_status: str,
    repo_root: Path,
) -> tuple[CheckpointManifest, Mapping[str, str | bytes]]:
    legacy_payload = json.dumps(raw_checkpoint, indent=2, sort_keys=True) + "\n"
    repo_state = {
        "schema_version": 2,
        "kind": "legacy_v1_checkpoint_import",
        "session_id": session_id,
        "checkpoint_id": checkpoint.checkpoint_id,
        "captured_at": checkpoint.created_at,
        "source_schema_version": 1,
        "capture_mode": "snapshot",
        "repo_root": str(repo_root),
        "current_agent": current_agent,
        "phase": phase_after,
        "task_status": task_status,
        "next_action": checkpoint.next_action,
    }
    validation = {
        "schema_version": 2,
        "kind": "checkpoint_validation",
        "session_id": session_id,
        "checkpoint_id": checkpoint.checkpoint_id,
        "captured_at": checkpoint.created_at,
        "status": checkpoint.validation.status,
        "summary": checkpoint.validation.summary,
    }
    summary = _render_imported_checkpoint_summary(checkpoint, current_agent=current_agent, phase_after=phase_after, task_status=task_status)
    snapshot_manifest = {
        "schema_version": 2,
        "kind": "legacy_v1_snapshot_import",
        "session_id": session_id,
        "checkpoint_id": checkpoint.checkpoint_id,
        "source_schema_version": 1,
        "source_files": [
            f"legacy-v1/checkpoints/{checkpoint.checkpoint_id}.json",
        ],
    }
    file_contents: dict[str, str | bytes] = {
        "repo-state.json": _json_text(repo_state),
        "validation.json": _json_text(validation),
        "summary.md": summary,
        "snapshot-manifest.json": _json_text(snapshot_manifest),
        "legacy-checkpoint.json": legacy_payload,
    }
    manifest_files = _manifest_files_from_contents(file_contents)
    manifest = CheckpointManifest(
        schema_version=SCHEMA_VERSION,
        kind="checkpoint_manifest",
        object_id=checkpoint.checkpoint_id,
        session_id=session_id,
        created_at=checkpoint.created_at,
        current_agent=current_agent,
        phase_hint=phase_after,
        task_status=task_status,
        capture_mode="snapshot",
        next_action=checkpoint.next_action,
        decisions=tuple(checkpoint.decisions),
        blockers=tuple(checkpoint.blockers),
        research_notes=tuple(checkpoint.research_notes),
        implementation_notes=tuple(checkpoint.implementation_notes),
        touched_files=tuple(checkpoint.touched_files),
        validation=ValidationState(status=checkpoint.validation.status, summary=checkpoint.validation.summary),
        repo_state_file="repo-state.json",
        validation_file="validation.json",
        summary_file="summary.md",
        git_head_file=None,
        workspace_patch_file=None,
        untracked_manifest_file=None,
        snapshot_manifest_file="snapshot-manifest.json",
        files=manifest_files,
    )
    return manifest, file_contents


def _build_handoff_manifest(
    *,
    repo_root: Path,
    session_id: str,
    handoff: HandoffRecord,
    checkpoint: CheckpointRecord,
    state: SessionState | None,
    checkpoint_count: int,
    handoff_index: int,
    source_event_hash: str,
    alerts: list[str],
    broken_paths: list[str],
) -> tuple[HandoffManifest, Mapping[str, str | bytes]]:
    handoff_id = f"ho-import-{handoff_index:04d}"
    packet_text = _load_or_render_legacy_packet(repo_root, session_id, handoff, checkpoint, state, alerts, broken_paths)
    packet_sha = sha256_text(packet_text)
    packet_sha_text = packet_sha + "\n"
    launch_spec_text = _json_text(
        {
            "profile": handoff.launch_profile,
            "cwd": handoff.launch_cwd,
            "command": handoff.launch_command,
            "template": handoff.launch_template,
            "template_source": handoff.launch_template_source,
            "instructions": handoff.launch_instructions,
            "packet_aware": handoff.launch_packet_aware,
            "execute_policy": handoff.launch_execute_policy,
            "warning": handoff.launch_warning,
        }
    )
    legacy_handoff = _json_text(handoff.to_dict())
    file_contents: dict[str, str | bytes] = {
        "packet.md": packet_text,
        "packet.sha256": packet_sha_text,
        "launch-spec.json": launch_spec_text,
        "legacy-handoff.json": legacy_handoff,
    }
    manifest = HandoffManifest(
        schema_version=SCHEMA_VERSION,
        kind="handoff_manifest",
        object_id=handoff_id,
        session_id=session_id,
        created_at=handoff.prepared_at,
        from_agent=handoff.from_agent,
        to_agent=handoff.to_agent,
        reason=handoff.reason,
        source_checkpoint_id=checkpoint.checkpoint_id,
        source_event_hash=source_event_hash,
        launch_profile=handoff.launch_profile,
        launch_cwd=handoff.launch_cwd,
        launch_command=handoff.launch_command,
        launch_template=handoff.launch_template,
        launch_template_source=handoff.launch_template_source,
        launch_instructions=handoff.launch_instructions,
        launch_packet_aware=handoff.launch_packet_aware,
        launch_execute_policy=handoff.launch_execute_policy,
        launch_warning=handoff.launch_warning,
        packet_file="packet.md",
        packet_sha256_file="packet.sha256",
        launch_spec_file="launch-spec.json",
        files=_manifest_files_from_contents(file_contents),
    )
    return manifest, file_contents


def _build_launch_manifest(
    *,
    session_id: str,
    handoff: HandoffRecord,
    handoff_id: str,
    launch_id: str,
    status: str,
) -> tuple[LaunchManifest, Mapping[str, str | bytes]]:
    created_at = handoff.finished_at or handoff.launched_at or handoff.prepared_at
    metadata = _json_text(
        {
            "schema_version": 2,
            "kind": "legacy_v1_launch_import",
            "session_id": session_id,
            "handoff_id": handoff_id,
            "status": status,
            "legacy_launch_status": handoff.launch_status,
            "exit_code": handoff.exit_code,
        }
    )
    file_contents: dict[str, str | bytes] = {"legacy-launch.json": metadata}
    manifest = LaunchManifest(
        schema_version=SCHEMA_VERSION,
        kind="launch_manifest",
        object_id=launch_id,
        session_id=session_id,
        created_at=created_at,
        handoff_id=handoff_id,
        target_agent=handoff.to_agent,
        started_at=handoff.launched_at or handoff.prepared_at,
        finished_at=created_at,
        status=status,
        exit_code=handoff.exit_code if handoff.exit_code is not None else (0 if status == "succeeded" else 1),
        dispatched_command=handoff.launch_command,
        stdout_file=None,
        stderr_file=None,
        files=_manifest_files_from_contents(file_contents),
    )
    return manifest, file_contents


def _load_or_render_legacy_packet(
    repo_root: Path,
    session_id: str,
    handoff: HandoffRecord,
    checkpoint: CheckpointRecord,
    state: SessionState | None,
    alerts: list[str],
    broken_paths: list[str],
) -> str:
    packet_path = Path(handoff.resume_packet_path)
    if packet_path.exists():
        return packet_path.read_text(encoding="utf-8")
    broken_paths.append(str(packet_path))
    alerts.append(f"legacy resume packet was missing for handoff to {handoff.to_agent}; generated a replacement packet")
    if state is not None:
        return render_resume_packet(
            state,
            checkpoint,
            handoff.to_agent,
            handoff_reason=handoff.reason,
            prepared_at=handoff.prepared_at,
            options=ResumeRenderOptions(evidence_depth="full"),
        )
    return (
        f"# Imported Resume Packet\n\n"
        f"Legacy v1 handoff packet for {handoff.to_agent} was missing.\n"
        f"Session: {session_id}\n"
        f"Checkpoint: {checkpoint.checkpoint_id}\n"
        f"Reason: {handoff.reason}\n"
    )


def _plan_failover(import_state: _ImportState):
    try:
        return plan_failover_command(LifecycleState(phase=import_state.phase, task_status=import_state.task_status))
    except LifecycleViolation as exc:
        raise SystemExit(f"legacy handoff import violated the v2 lifecycle: {exc}") from exc


def _plan_launch_started(import_state: _ImportState):
    try:
        return plan_launch_started(LifecycleState(phase=import_state.phase, task_status=import_state.task_status))
    except LifecycleViolation as exc:
        raise SystemExit(f"legacy launch import violated the v2 lifecycle: {exc}") from exc


def _plan_launch_finished(import_state: _ImportState, launch_status: str):
    try:
        return plan_launch_finished(
            LifecycleState(phase=import_state.phase, task_status=import_state.task_status),
            launch_status=launch_status,
        )
    except LifecycleViolation as exc:
        raise SystemExit(f"legacy launch receipt import violated the v2 lifecycle: {exc}") from exc


def _plan_resume(import_state: _ImportState):
    try:
        return plan_resume_command(LifecycleState(phase=import_state.phase, task_status=import_state.task_status))
    except LifecycleViolation as exc:
        raise SystemExit(f"legacy resume import violated the v2 lifecycle: {exc}") from exc


def _map_legacy_launch_status(status: str, alerts: list[str]) -> str:
    if status == "succeeded":
        return "succeeded"
    if status == "failed":
        return "failed"
    if status == "launching":
        alerts.append("legacy in-flight launch was imported as interrupted")
        return "interrupted"
    if status == "ready":
        raise SystemExit("internal error: ready handoff must not be imported as a launch receipt")
    alerts.append(f"legacy launch status {status} was normalized to failed")
    return "failed"


def _parse_raw_handoffs(
    raw_state: Mapping[str, Any] | None,
    alerts: list[str],
    broken_paths: list[str],
    state_path: Path,
) -> tuple[HandoffRecord, ...]:
    if raw_state is None:
        return tuple()
    raw_handoffs = raw_state.get("handoffs", [])
    if not isinstance(raw_handoffs, list):
        alerts.append("legacy handoffs list was malformed; preserved raw archive only")
        broken_paths.append(str(state_path))
        return tuple()
    parsed: list[HandoffRecord] = []
    for index, item in enumerate(raw_handoffs):
        try:
            parsed.append(HandoffRecord.from_dict(item))
        except ModelValidationError as exc:
            alerts.append(f"legacy handoff #{index + 1} failed validation: {exc}")
            broken_paths.append(str(state_path))
    return tuple(parsed)


def _parse_validation(raw_state: Mapping[str, Any] | None, state: SessionState | None) -> ValidationState:
    if state is not None:
        return ValidationState(status=state.validation.status, summary=state.validation.summary)
    if raw_state is None:
        return ValidationState(status="not_run", summary="")
    raw_validation = raw_state.get("validation")
    if isinstance(raw_validation, Mapping):
        try:
            legacy = LegacyValidationState.from_dict(raw_validation)
            return ValidationState(status=legacy.status, summary=legacy.summary)
        except ModelValidationError:
            pass
    return ValidationState(status="not_run", summary="")


def _mapping_str(mapping: Mapping[str, Any] | None, key: str) -> str | None:
    if mapping is None:
        return None
    value = mapping.get(key)
    return value if isinstance(value, str) and value.strip() else None


def _mapping_choice(mapping: Mapping[str, Any] | None, key: str, choices: set[str]) -> str | None:
    value = _mapping_str(mapping, key)
    return value if value in choices else None


def _migration_record_path(repo_root: Path, session_id: str) -> Path:
    records_dir = legacy_migration_records_dir(repo_root)
    records_dir.mkdir(parents=True, exist_ok=True)
    return records_dir / f"{session_id}.json"


def _write_migration_record(path: Path, record: _MigrationSwapRecord) -> None:
    write_json_atomic(path, record.to_dict())


def _manifest_hash(manifest: SessionManifest) -> str:
    from agent_relay.v2.models import build_session_manifest_hash

    return build_session_manifest_hash(manifest)


def _manifest_files_from_contents(file_contents: Mapping[str, str | bytes]) -> tuple[ManifestFile, ...]:
    files: list[ManifestFile] = []
    for relative_path, content in sorted(file_contents.items()):
        data = content if isinstance(content, bytes) else content.encode("utf-8")
        files.append(
            ManifestFile(
                relative_path=relative_path,
                sha256=sha256_bytes(data),
                size_bytes=len(data),
            )
        )
    return tuple(files)


def _render_imported_checkpoint_summary(
    checkpoint: CheckpointRecord,
    *,
    current_agent: str,
    phase_after: str,
    task_status: str,
) -> str:
    lines = [
        "# Imported Legacy Checkpoint",
        "",
        f"Checkpoint ID: {checkpoint.checkpoint_id}",
        f"Imported from schema: 1",
        f"Current agent: {current_agent}",
        f"Phase: {phase_after}",
        f"Task status: {task_status}",
        f"Next action: {checkpoint.next_action or 'Not recorded'}",
        f"Validation: {checkpoint.validation.status} - {checkpoint.validation.summary or 'None recorded'}",
        "",
        "Decisions:",
    ]
    lines.extend(_bullet_lines(checkpoint.decisions))
    lines.extend(["", "Blockers:"])
    lines.extend(_bullet_lines(checkpoint.blockers))
    lines.extend(["", "Touched files:"])
    lines.extend(_bullet_lines(checkpoint.touched_files))
    lines.append("")
    return "\n".join(lines)


def _bullet_lines(items: list[str]) -> list[str]:
    if not items:
        return ["- None recorded"]
    return [f"- {item}" for item in items]


def _json_text(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _rechain_events(events: list[Any]) -> None:
    previous_hash: str | None = None
    for index, event in enumerate(events, start=1):
        events[index - 1] = build_journal_event(
            session_id=event.session_id,
            event_id=f"ev-{index:06d}",
            sequence=index,
            event_type=event.type,
            timestamp=event.timestamp,
            tx_id=event.tx_id,
            phase_before=event.phase_before,
            phase_after=event.phase_after,
            payload=event.payload,
            object_refs=event.object_refs,
            prev_event_hash=previous_hash,
        )
        previous_hash = events[index - 1].event_hash


def _refresh_handoff_anchors(
    events: list[Any],
    object_payloads: list[tuple[Any, Mapping[str, str | bytes]]],
) -> None:
    payload_by_handoff: dict[str, tuple[HandoffManifest, Mapping[str, str | bytes]]] = {}
    payload_indexes: dict[str, int] = {}
    for index, (manifest, files) in enumerate(object_payloads):
        if isinstance(manifest, HandoffManifest):
            payload_by_handoff[manifest.object_id] = (manifest, files)
            payload_indexes[manifest.object_id] = index

    changed = True
    while changed:
        changed = False
        _rechain_events(events)
        for event_index, event in enumerate(events):
            if event.type != "handoff.prepared":
                continue
            handoff_id = event.payload.get("handoff_id")
            if not isinstance(handoff_id, str) or handoff_id not in payload_by_handoff or event_index == 0:
                continue
            previous_hash = events[event_index - 1].event_hash
            manifest, files = payload_by_handoff[handoff_id]
            if manifest.source_event_hash == previous_hash:
                continue
            updated_manifest = HandoffManifest(
                schema_version=manifest.schema_version,
                kind=manifest.kind,
                object_id=manifest.object_id,
                session_id=manifest.session_id,
                created_at=manifest.created_at,
                from_agent=manifest.from_agent,
                to_agent=manifest.to_agent,
                reason=manifest.reason,
                source_checkpoint_id=manifest.source_checkpoint_id,
                source_event_hash=previous_hash,
                launch_profile=manifest.launch_profile,
                launch_cwd=manifest.launch_cwd,
                launch_command=manifest.launch_command,
                launch_template=manifest.launch_template,
                launch_template_source=manifest.launch_template_source,
                launch_instructions=manifest.launch_instructions,
                launch_packet_aware=manifest.launch_packet_aware,
                launch_execute_policy=manifest.launch_execute_policy,
                launch_warning=manifest.launch_warning,
                packet_file=manifest.packet_file,
                packet_sha256_file=manifest.packet_sha256_file,
                launch_spec_file=manifest.launch_spec_file,
                files=manifest.files,
            )
            object_payloads[payload_indexes[handoff_id]] = (updated_manifest, files)
            payload_by_handoff[handoff_id] = (updated_manifest, files)
            events[event_index] = build_journal_event(
                session_id=event.session_id,
                event_id=event.event_id,
                sequence=event.sequence,
                event_type=event.type,
                timestamp=event.timestamp,
                tx_id=event.tx_id,
                phase_before=event.phase_before,
                phase_after=event.phase_after,
                payload=event.payload,
                object_refs=(_preview_object_ref(updated_manifest),),
                prev_event_hash=event.prev_event_hash,
            )
            changed = True
