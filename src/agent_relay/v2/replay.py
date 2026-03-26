from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from agent_relay.v2.errors import V2CorruptionError
from agent_relay.v2.lifecycle import (
    LifecycleState,
    LifecycleViolation,
    normalize_checkpoint_status_directive,
    plan_checkpoint_command,
    plan_complete_command,
    plan_failover_command,
    plan_launch_finished,
    plan_launch_started,
    plan_repair_command,
    plan_resume_command,
    plan_session_started,
)
from agent_relay.v2.models import (
    CheckpointManifest,
    DerivedHandoffView,
    DerivedSessionView,
    HeadRef,
    HandoffManifest,
    JournalEvent,
    LaunchManifest,
    ObjectManifest,
    ObjectRef,
    SCHEMA_VERSION,
    SessionManifest,
    ValidationState,
)

ObjectLoader = Callable[[ObjectRef], ObjectManifest]


@dataclass
class _ReplayState:
    manifest: SessionManifest
    current_agent: str
    phase: str
    updated_at: str
    task_status: str | None = None
    next_action: str = ""
    decisions: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    research_notes: list[str] = field(default_factory=list)
    implementation_notes: list[str] = field(default_factory=list)
    touched_files: list[str] = field(default_factory=list)
    validation: ValidationState = field(
        default_factory=lambda: ValidationState(status="not_run", summary=""),
    )
    latest_checkpoint_id: str | None = None
    prepared_handoff_id: str | None = None
    latest_launch_id: str | None = None
    last_resume_handoff_id: str | None = None
    handoffs: dict[str, DerivedHandoffView] = field(default_factory=dict)
    checkpoint_ids: list[str] = field(default_factory=list)
    launch_ids: list[str] = field(default_factory=list)
    last_event_id: str = ""
    last_event_hash: str = ""
    last_sequence: int = 0


@dataclass(frozen=True, slots=True)
class ReplayResult:
    view: DerivedSessionView
    head: HeadRef


def replay_session(
    manifest: SessionManifest,
    *,
    manifest_hash: str,
    events: list[JournalEvent],
    load_object: ObjectLoader,
) -> ReplayResult:
    if not events:
        raise V2CorruptionError("journal is empty", session_id=manifest.session_id)

    ordered_events = sorted(events, key=lambda item: item.sequence)
    state = _ReplayState(
        manifest=manifest,
        current_agent=manifest.initial_agent,
        phase="active",
        updated_at=manifest.created_at,
    )

    previous_hash: str | None = None
    expected_sequence = 1
    for event in ordered_events:
        _validate_event_chain(
            manifest=manifest,
            manifest_hash=manifest_hash,
            event=event,
            previous_hash=previous_hash,
            expected_sequence=expected_sequence,
            current_phase=state.phase if state.last_sequence else None,
        )
        _apply_event(state, event, load_object, manifest_hash)
        previous_hash = event.event_hash
        expected_sequence += 1

    head = HeadRef(
        schema_version=SCHEMA_VERSION,
        kind="session_head_ref",
        session_id=manifest.session_id,
        last_event_id=state.last_event_id,
        last_sequence=state.last_sequence,
        last_event_hash=state.last_event_hash,
        updated_at=state.updated_at,
    )
    handoffs = tuple(
        state.handoffs[handoff_id]
        for handoff_id in sorted(
            state.handoffs,
            key=lambda item: (state.handoffs[item].prepared_at, item),
        )
    )
    view = DerivedSessionView(
        schema_version=SCHEMA_VERSION,
        kind="derived_session_view",
        session_id=manifest.session_id,
        storage_model="journal_v2",
        repo_root=manifest.repo_root,
        objective=manifest.objective,
        workstream_kind=manifest.workstream_kind,
        created_at=manifest.created_at,
        updated_at=state.updated_at,
        initial_agent=manifest.initial_agent,
        current_agent=state.current_agent,
        phase=state.phase,
        current_status=state.phase,
        task_status=state.task_status,
        next_action=state.next_action,
        decisions=tuple(state.decisions),
        blockers=tuple(state.blockers),
        research_notes=tuple(state.research_notes),
        implementation_notes=tuple(state.implementation_notes),
        touched_files=tuple(state.touched_files),
        validation=state.validation,
        latest_checkpoint_id=state.latest_checkpoint_id,
        prepared_handoff_id=state.prepared_handoff_id,
        latest_launch_id=state.latest_launch_id,
        last_resume_handoff_id=state.last_resume_handoff_id,
        event_count=state.last_sequence,
        last_event_id=state.last_event_id,
        last_event_hash=state.last_event_hash,
        built_from_sequence=state.last_sequence,
        built_from_event_hash=state.last_event_hash,
        health="healthy",
        handoffs=handoffs,
        checkpoint_ids=tuple(state.checkpoint_ids),
        launch_ids=tuple(state.launch_ids),
        alerts=tuple(),
    )
    return ReplayResult(view=view, head=head)


def _validate_event_chain(
    *,
    manifest: SessionManifest,
    manifest_hash: str,
    event: JournalEvent,
    previous_hash: str | None,
    expected_sequence: int,
    current_phase: str | None,
) -> None:
    if event.session_id != manifest.session_id:
        raise V2CorruptionError(
            "journal event session_id does not match session manifest",
            session_id=manifest.session_id,
        )
    if event.sequence != expected_sequence:
        raise V2CorruptionError(
            f"journal sequence gap or duplicate at {event.event_id}",
            session_id=manifest.session_id,
        )
    if event.prev_event_hash != previous_hash:
        raise V2CorruptionError(
            f"journal hash chain broken at {event.event_id}",
            session_id=manifest.session_id,
        )
    if event.expected_event_hash() != event.event_hash:
        raise V2CorruptionError(
            f"journal event hash mismatch at {event.event_id}",
            session_id=manifest.session_id,
        )
    if expected_sequence == 1:
        if event.type != "session.started":
            raise V2CorruptionError(
                "first journal event must be session.started",
                session_id=manifest.session_id,
            )
        recorded_manifest_hash = event.payload.get("session_manifest_sha256")
        if recorded_manifest_hash != manifest_hash:
            raise V2CorruptionError(
                "session manifest hash does not match session.started payload",
                session_id=manifest.session_id,
            )
        if event.phase_before is not None:
            raise V2CorruptionError(
                "first event phase_before must be null",
                session_id=manifest.session_id,
            )
    else:
        if event.phase_before != current_phase:
            raise V2CorruptionError(
                f"phase_before does not match replay state at {event.event_id}",
                session_id=manifest.session_id,
            )


def _apply_event(
    state: _ReplayState,
    event: JournalEvent,
    load_object: ObjectLoader,
    manifest_hash: str,
) -> None:
    if event.type == "session.started":
        _apply_session_started(state, event, manifest_hash)
    elif event.type == "checkpoint.recorded":
        _apply_checkpoint(state, event, load_object)
    elif event.type == "handoff.prepared":
        _apply_handoff(state, event, load_object)
    elif event.type == "launch.started":
        _apply_launch_started(state, event)
    elif event.type == "launch.finished":
        _apply_launch_finished(state, event, load_object)
    elif event.type == "resume.accepted":
        _apply_resume(state, event)
    elif event.type == "session.completed":
        _apply_session_completed(state, event)
    elif event.type == "repair.rebuilt":
        _apply_repair_rebuilt(state, event)
    else:
        raise V2CorruptionError(
            f"unsupported journal event type {event.type}",
            session_id=state.manifest.session_id,
        )

    state.phase = event.phase_after
    state.updated_at = event.timestamp
    state.last_event_id = event.event_id
    state.last_event_hash = event.event_hash
    state.last_sequence = event.sequence


def _apply_session_started(state: _ReplayState, event: JournalEvent, manifest_hash: str) -> None:
    _ensure_no_refs(event)
    if event.payload.get("session_manifest_sha256") != manifest_hash:
        raise V2CorruptionError(
            "session.started payload manifest hash mismatch",
            session_id=state.manifest.session_id,
        )
    transition = _plan_or_corrupt(event, lambda: plan_session_started())
    if event.phase_after != transition.phase_after:
        raise V2CorruptionError(
            "session.started phase_after does not match the lifecycle state machine",
            session_id=state.manifest.session_id,
        )
    state.current_agent = state.manifest.initial_agent


def _require_single_ref(event: JournalEvent, expected_kind: str) -> ObjectRef:
    if len(event.object_refs) != 1:
        raise V2CorruptionError(
            f"{event.type} must reference exactly one {expected_kind} object",
            session_id=event.session_id,
        )
    ref = event.object_refs[0]
    if ref.object_kind != expected_kind:
        raise V2CorruptionError(
            f"{event.type} must reference a {expected_kind} object",
            session_id=event.session_id,
        )
    return ref


def _ensure_no_refs(event: JournalEvent) -> None:
    if event.object_refs:
        raise V2CorruptionError(
            f"{event.type} must not reference object manifests",
            session_id=event.session_id,
        )


def _apply_checkpoint(state: _ReplayState, event: JournalEvent, load_object: ObjectLoader) -> None:
    ref = _require_single_ref(event, "checkpoint")
    manifest = load_object(ref)
    if not isinstance(manifest, CheckpointManifest):
        raise V2CorruptionError("checkpoint event resolved to the wrong manifest type", session_id=event.session_id)
    if event.payload.get("checkpoint_id") != manifest.object_id:
        raise V2CorruptionError("checkpoint payload does not match checkpoint manifest", session_id=event.session_id)
    command_name = event.payload.get("command_name", "checkpoint")
    status_directive = event.payload.get("status_directive")
    transition = _plan_or_corrupt(
        event,
        lambda: plan_checkpoint_command(
            LifecycleState(phase=state.phase, task_status=state.task_status),
            command_name=command_name,
            status_directive=normalize_checkpoint_status_directive(status_directive),
        ),
    )
    if event.phase_after != transition.phase_after:
        raise V2CorruptionError(
            "checkpoint.recorded phase_after does not match the lifecycle state machine",
            session_id=event.session_id,
        )
    if manifest.phase_hint != transition.phase_after:
        raise V2CorruptionError("checkpoint phase_hint does not match journal phase", session_id=event.session_id)
    if manifest.task_status != transition.task_status_after:
        raise V2CorruptionError(
            "checkpoint task_status does not match the lifecycle state machine",
            session_id=event.session_id,
        )

    state.current_agent = manifest.current_agent
    state.task_status = manifest.task_status
    state.next_action = manifest.next_action
    state.decisions = list(manifest.decisions)
    state.blockers = list(manifest.blockers)
    state.research_notes = list(manifest.research_notes)
    state.implementation_notes = list(manifest.implementation_notes)
    state.touched_files = list(manifest.touched_files)
    state.validation = manifest.validation
    state.latest_checkpoint_id = manifest.object_id
    state.checkpoint_ids.append(manifest.object_id)


def _apply_handoff(state: _ReplayState, event: JournalEvent, load_object: ObjectLoader) -> None:
    ref = _require_single_ref(event, "handoff")
    manifest = load_object(ref)
    if not isinstance(manifest, HandoffManifest):
        raise V2CorruptionError("handoff event resolved to the wrong manifest type", session_id=event.session_id)
    if event.payload.get("handoff_id") != manifest.object_id:
        raise V2CorruptionError("handoff payload does not match handoff manifest", session_id=event.session_id)
    if state.latest_checkpoint_id != manifest.source_checkpoint_id:
        raise V2CorruptionError(
            "handoff does not reference the latest checkpoint",
            session_id=event.session_id,
        )
    if manifest.source_event_hash != state.last_event_hash:
        raise V2CorruptionError(
            "handoff does not anchor to the prior journal head",
            session_id=event.session_id,
        )
    transition = _plan_or_corrupt(
        event,
        lambda: plan_failover_command(LifecycleState(phase=state.phase, task_status=state.task_status)),
    )
    if event.phase_after != transition.phase_after:
        raise V2CorruptionError(
            "handoff.prepared phase_after does not match the lifecycle state machine",
            session_id=event.session_id,
        )
    handoff = DerivedHandoffView(
        handoff_id=manifest.object_id,
        from_agent=manifest.from_agent,
        to_agent=manifest.to_agent,
        reason=manifest.reason,
        prepared_at=manifest.created_at,
        checkpoint_id=manifest.source_checkpoint_id,
        launch_status="ready",
        latest_launch_id=None,
    )
    state.handoffs[manifest.object_id] = handoff
    state.prepared_handoff_id = manifest.object_id


def _apply_launch_started(state: _ReplayState, event: JournalEvent) -> None:
    _ensure_no_refs(event)
    handoff_id = event.payload.get("handoff_id")
    launch_id = event.payload.get("launch_id")
    if not isinstance(handoff_id, str) or not isinstance(launch_id, str):
        raise V2CorruptionError("launch.started payload must include handoff_id and launch_id", session_id=event.session_id)
    if handoff_id not in state.handoffs:
        raise V2CorruptionError("launch.started references an unknown handoff", session_id=event.session_id)
    if state.prepared_handoff_id != handoff_id:
        raise V2CorruptionError("launch.started must reference the current prepared handoff", session_id=event.session_id)
    transition = _plan_or_corrupt(
        event,
        lambda: plan_launch_started(LifecycleState(phase=state.phase, task_status=state.task_status)),
    )
    if event.phase_after != transition.phase_after:
        raise V2CorruptionError(
            "launch.started phase_after does not match the lifecycle state machine",
            session_id=event.session_id,
        )
    handoff = state.handoffs[handoff_id]
    state.handoffs[handoff_id] = DerivedHandoffView(
        handoff_id=handoff.handoff_id,
        from_agent=handoff.from_agent,
        to_agent=handoff.to_agent,
        reason=handoff.reason,
        prepared_at=handoff.prepared_at,
        checkpoint_id=handoff.checkpoint_id,
        launch_status="launching",
        latest_launch_id=launch_id,
    )
    state.latest_launch_id = launch_id


def _apply_launch_finished(state: _ReplayState, event: JournalEvent, load_object: ObjectLoader) -> None:
    ref = _require_single_ref(event, "launch")
    manifest = load_object(ref)
    if not isinstance(manifest, LaunchManifest):
        raise V2CorruptionError("launch event resolved to the wrong manifest type", session_id=event.session_id)
    if event.payload.get("handoff_id") != manifest.handoff_id or event.payload.get("launch_id") != manifest.object_id:
        raise V2CorruptionError("launch payload does not match launch manifest", session_id=event.session_id)
    if manifest.handoff_id not in state.handoffs:
        raise V2CorruptionError("launch receipt references an unknown handoff", session_id=event.session_id)
    handoff = state.handoffs[manifest.handoff_id]
    if handoff.latest_launch_id != manifest.object_id:
        raise V2CorruptionError("launch receipt does not match the current launch attempt", session_id=event.session_id)
    transition = _plan_or_corrupt(
        event,
        lambda: plan_launch_finished(
            LifecycleState(phase=state.phase, task_status=state.task_status),
            launch_status=manifest.status,
        ),
    )
    if event.phase_after != transition.phase_after:
        raise V2CorruptionError(
            "launch.finished phase_after does not match the lifecycle state machine",
            session_id=event.session_id,
        )
    state.handoffs[manifest.handoff_id] = DerivedHandoffView(
        handoff_id=handoff.handoff_id,
        from_agent=handoff.from_agent,
        to_agent=handoff.to_agent,
        reason=handoff.reason,
        prepared_at=handoff.prepared_at,
        checkpoint_id=handoff.checkpoint_id,
        launch_status=manifest.status,
        latest_launch_id=manifest.object_id,
    )
    state.latest_launch_id = manifest.object_id
    state.launch_ids.append(manifest.object_id)


def _apply_resume(state: _ReplayState, event: JournalEvent) -> None:
    _ensure_no_refs(event)
    handoff_id = event.payload.get("handoff_id")
    if not isinstance(handoff_id, str):
        raise V2CorruptionError("resume.accepted payload must include handoff_id", session_id=event.session_id)
    if handoff_id not in state.handoffs:
        raise V2CorruptionError("resume.accepted references an unknown handoff", session_id=event.session_id)
    if state.prepared_handoff_id != handoff_id:
        raise V2CorruptionError("resume.accepted must reference the current prepared handoff", session_id=event.session_id)
    accepted_by_agent = event.payload.get("accepted_by_agent")
    if accepted_by_agent is not None and not isinstance(accepted_by_agent, str):
        raise V2CorruptionError("resume.accepted accepted_by_agent must be a string", session_id=event.session_id)
    transition = _plan_or_corrupt(
        event,
        lambda: plan_resume_command(LifecycleState(phase=state.phase, task_status=state.task_status)),
    )
    if event.phase_after != transition.phase_after:
        raise V2CorruptionError(
            "resume.accepted phase_after does not match the lifecycle state machine",
            session_id=event.session_id,
        )
    handoff = state.handoffs[handoff_id]
    if accepted_by_agent is not None and accepted_by_agent != handoff.to_agent:
        raise V2CorruptionError("resume.accepted accepted_by_agent does not match handoff target", session_id=event.session_id)
    state.current_agent = handoff.to_agent
    state.last_resume_handoff_id = handoff_id
    if state.prepared_handoff_id == handoff_id:
        state.prepared_handoff_id = None


def _apply_session_completed(state: _ReplayState, event: JournalEvent) -> None:
    _ensure_no_refs(event)
    transition = _plan_or_corrupt(
        event,
        lambda: plan_complete_command(LifecycleState(phase=state.phase, task_status=state.task_status)),
    )
    if event.phase_after != transition.phase_after:
        raise V2CorruptionError(
            "session.completed phase_after does not match the lifecycle state machine",
            session_id=event.session_id,
        )
    completed_by = event.payload.get("completed_by_agent")
    if completed_by is not None:
        if not isinstance(completed_by, str):
            raise V2CorruptionError("session.completed completed_by_agent must be a string", session_id=event.session_id)
        state.current_agent = completed_by


def _apply_repair_rebuilt(state: _ReplayState, event: JournalEvent) -> None:
    _ensure_no_refs(event)
    transition = _plan_or_corrupt(
        event,
        lambda: plan_repair_command(LifecycleState(phase=state.phase, task_status=state.task_status)),
    )
    if event.phase_after != transition.phase_after:
        raise V2CorruptionError(
            "repair.rebuilt phase_after does not match the lifecycle state machine",
            session_id=event.session_id,
        )


def _plan_or_corrupt(event: JournalEvent, planner):
    try:
        return planner()
    except LifecycleViolation as exc:
        raise V2CorruptionError(str(exc), session_id=event.session_id) from exc
