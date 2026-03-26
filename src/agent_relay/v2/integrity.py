from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from agent_relay.v2.errors import V2CorruptionError, V2ValidationError
from agent_relay.v2.layout import (
    journal_dir,
    pending_tx_dir,
    session_manifest_path,
    session_root,
)
from agent_relay.v2.models import JournalEvent, SessionManifest, build_session_manifest_hash
from agent_relay.v2.replay import ReplayResult, replay_session
from agent_relay.v2.storage import _load_object_from_ref, load_session_manifest

HEALTH_STATUSES = {"healthy", "degraded", "corrupt"}


@dataclass(frozen=True, slots=True)
class LastValidEvent:
    event_id: str
    sequence: int
    event_hash: str

    def to_dict(self) -> dict[str, str | int]:
        return {
            "event_id": self.event_id,
            "sequence": self.sequence,
            "event_hash": self.event_hash,
        }


@dataclass(frozen=True, slots=True)
class SessionIntegrityReport:
    session_id: str
    storage_model: str
    repo_root: str
    objective: str
    workstream_kind: str
    created_at: str
    updated_at: str
    initial_agent: str
    current_agent: str
    current_status: str
    task_status: str | None
    next_action: str
    decisions: tuple[str, ...]
    blockers: tuple[str, ...]
    research_notes: tuple[str, ...]
    implementation_notes: tuple[str, ...]
    touched_files: tuple[str, ...]
    validation: dict[str, str]
    latest_checkpoint_id: str | None
    prepared_handoff_id: str | None
    latest_launch_id: str | None
    last_resume_handoff_id: str | None
    handoffs: tuple[dict[str, object], ...]
    checkpoint_ids: tuple[str, ...]
    launch_ids: tuple[str, ...]
    health: str
    error: str | None
    last_valid_event: LastValidEvent | None
    broken_paths: tuple[str, ...]
    suggested_repair: tuple[str, ...]
    alerts: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.health not in HEALTH_STATUSES:
            allowed = ", ".join(sorted(HEALTH_STATUSES))
            raise ValueError(f"health must be one of: {allowed}")

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "storage_model": self.storage_model,
            "repo_root": self.repo_root,
            "objective": self.objective,
            "workstream_kind": self.workstream_kind,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "initial_agent": self.initial_agent,
            "current_agent": self.current_agent,
            "current_status": self.current_status,
            "phase": self.current_status,
            "task_status": self.task_status,
            "next_action": self.next_action,
            "decisions": list(self.decisions),
            "blockers": list(self.blockers),
            "research_notes": list(self.research_notes),
            "implementation_notes": list(self.implementation_notes),
            "touched_files": list(self.touched_files),
            "validation": dict(self.validation),
            "latest_checkpoint_id": self.latest_checkpoint_id,
            "prepared_handoff_id": self.prepared_handoff_id,
            "latest_launch_id": self.latest_launch_id,
            "last_resume_handoff_id": self.last_resume_handoff_id,
            "handoffs": list(self.handoffs),
            "checkpoint_ids": list(self.checkpoint_ids),
            "launch_ids": list(self.launch_ids),
            "health": self.health,
            "error": self.error,
            "last_valid_event": self.last_valid_event.to_dict() if self.last_valid_event is not None else None,
            "broken_paths": list(self.broken_paths),
            "suggested_repair": list(self.suggested_repair),
            "alerts": list(self.alerts),
        }

    def dashboard_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "current_agent": self.current_agent,
            "current_status": self.current_status,
            "objective": self.objective,
            "updated_at": self.updated_at,
            "storage_model": self.storage_model,
            "health": self.health,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class IntegrityScan:
    report: SessionIntegrityReport
    event_paths: tuple[Path, ...]
    parsed_events: tuple[JournalEvent, ...]
    failure_index: int | None
    failure_event_path: Path | None
    pending_tx_paths: tuple[Path, ...]


def inspect_session_integrity(repo_root: Path, session_id: str) -> IntegrityScan:
    manifest_path = session_manifest_path(repo_root, session_id)
    pending_paths = _pending_transaction_paths(repo_root, session_id)

    try:
        manifest = load_session_manifest(repo_root, session_id)
    except SystemExit:
        raise
    except V2CorruptionError as exc:
        report = SessionIntegrityReport(
            session_id=session_id,
            storage_model="journal_v2",
            repo_root="",
            objective=str(exc),
            workstream_kind="mixed",
            created_at="",
            updated_at="",
            initial_agent="claude",
            current_agent="?",
            current_status="corrupt",
            task_status=None,
            next_action="",
            decisions=tuple(),
            blockers=tuple(),
            research_notes=tuple(),
            implementation_notes=tuple(),
            touched_files=tuple(),
            validation={"status": "not_run", "summary": ""},
            latest_checkpoint_id=None,
            prepared_handoff_id=None,
            latest_launch_id=None,
            last_resume_handoff_id=None,
            handoffs=tuple(),
            checkpoint_ids=tuple(),
            launch_ids=tuple(),
            health="corrupt",
            error=str(exc),
            last_valid_event=None,
            broken_paths=_unique_paths([exc.path]),
            suggested_repair=(
                "restore session.json from a known-good backup before mutating this session",
            ),
            alerts=(str(exc),),
        )
        return IntegrityScan(
            report=report,
            event_paths=tuple(),
            parsed_events=tuple(),
            failure_index=0,
            failure_event_path=exc.path,
            pending_tx_paths=pending_paths,
        )

    journal_path = journal_dir(repo_root, session_id)
    if not journal_path.exists():
        message = f"journal directory is missing (session={session_id}, path={journal_path})"
        report = _report_from_manifest(
            manifest,
            health="corrupt",
            error=message,
            last_valid_event=None,
            broken_paths=(str(journal_path),),
            suggested_repair=("restore the journal directory from backup before mutating this session",),
            alerts=(message,),
        )
        return IntegrityScan(
            report=report,
            event_paths=tuple(),
            parsed_events=tuple(),
            failure_index=0,
            failure_event_path=journal_path,
            pending_tx_paths=pending_paths,
        )

    event_paths = tuple(sorted(journal_path.glob("*.json")))
    if not event_paths:
        message = f"journal is empty (session={session_id}, path={journal_path})"
        report = _report_from_manifest(
            manifest,
            health="corrupt",
            error=message,
            last_valid_event=None,
            broken_paths=(str(journal_path),),
            suggested_repair=("restore the journal directory from backup before mutating this session",),
            alerts=(message,),
        )
        return IntegrityScan(
            report=report,
            event_paths=tuple(),
            parsed_events=tuple(),
            failure_index=0,
            failure_event_path=journal_path,
            pending_tx_paths=pending_paths,
        )

    manifest_hash = build_session_manifest_hash(manifest)
    session_path = session_root(repo_root, session_id)
    parsed_events: list[JournalEvent] = []
    last_good: ReplayResult | None = None
    failure_message: str | None = None
    failure_path: Path | None = None
    failure_index: int | None = None
    failure_event_path: Path | None = None

    for index, event_path in enumerate(event_paths):
        try:
            raw = json.loads(event_path.read_text(encoding="utf-8"))
            event = JournalEvent.from_dict(raw)
        except json.JSONDecodeError as exc:
            failure_message = f"journal file is not valid JSON: {exc}"
            failure_path = event_path
            failure_index = index
            failure_event_path = event_path
            break
        except V2ValidationError as exc:
            failure_message = str(exc)
            failure_path = event_path
            failure_index = index
            failure_event_path = event_path
            break

        parsed_events.append(event)
        try:
            last_good = replay_session(
                manifest,
                manifest_hash=manifest_hash,
                events=parsed_events,
                load_object=lambda ref: _load_object_from_ref(session_path, ref),
            )
        except V2CorruptionError as exc:
            failure_message = str(exc)
            failure_path = exc.path or event_path
            failure_index = index
            failure_event_path = event_path
            break

    if failure_message is None and not pending_paths and last_good is not None:
        report = _healthy_report_from_replay(manifest, last_good)
        return IntegrityScan(
            report=report,
            event_paths=event_paths,
            parsed_events=tuple(parsed_events),
            failure_index=None,
            failure_event_path=None,
            pending_tx_paths=tuple(),
        )

    alerts: list[str] = []
    if failure_message is not None:
        alerts.append(failure_message)
    if pending_paths:
        plural = "directory" if len(pending_paths) == 1 else "directories"
        alerts.append(f"pending transaction residue detected in {len(pending_paths)} {plural}")

    health = "degraded" if last_good is not None else "corrupt"
    broken_paths = _unique_paths([failure_path, *pending_paths])
    suggested_repair = _suggested_repair_commands(
        session_id,
        health=health,
        has_failure=failure_message is not None,
        has_last_good=last_good is not None,
        has_pending=bool(pending_paths),
    )
    error = "; ".join(alerts) if alerts else None
    if last_good is not None:
        report = _report_from_replay(
            last_good,
            health=health,
            error=error,
            broken_paths=broken_paths,
            suggested_repair=suggested_repair,
            alerts=tuple(alerts),
        )
    else:
        report = _report_from_manifest(
            manifest,
            health="corrupt",
            error=error or "session corruption detected",
            last_valid_event=None,
            broken_paths=broken_paths,
            suggested_repair=suggested_repair,
            alerts=tuple(alerts),
        )

    return IntegrityScan(
        report=report,
        event_paths=event_paths,
        parsed_events=tuple(parsed_events),
        failure_index=failure_index,
        failure_event_path=failure_event_path,
        pending_tx_paths=pending_paths,
    )


def require_session_mutable(repo_root: Path, session_id: str, *, command_name: str) -> SessionIntegrityReport:
    scan = inspect_session_integrity(repo_root, session_id)
    report = scan.report
    if report.health == "healthy":
        return report
    suggestions = ", ".join(report.suggested_repair) if report.suggested_repair else "repair the session first"
    raise SystemExit(
        f"{command_name} is blocked while session health is {report.health}; "
        f"last valid event: {report.last_valid_event.event_id if report.last_valid_event else 'none'}; "
        f"run: {suggestions}"
    )


def _report_from_replay(
    replay_result: ReplayResult,
    *,
    health: str,
    error: str | None,
    broken_paths: tuple[str, ...],
    suggested_repair: tuple[str, ...],
    alerts: tuple[str, ...],
) -> SessionIntegrityReport:
    view = replay_result.view
    return SessionIntegrityReport(
        session_id=view.session_id,
        storage_model=view.storage_model,
        repo_root=view.repo_root,
        objective=view.objective,
        workstream_kind=view.workstream_kind,
        created_at=view.created_at,
        updated_at=view.updated_at,
        initial_agent=view.initial_agent,
        current_agent=view.current_agent,
        current_status=view.current_status,
        task_status=view.task_status,
        next_action=view.next_action,
        decisions=view.decisions,
        blockers=view.blockers,
        research_notes=view.research_notes,
        implementation_notes=view.implementation_notes,
        touched_files=view.touched_files,
        validation=view.validation.to_dict(),
        latest_checkpoint_id=view.latest_checkpoint_id,
        prepared_handoff_id=view.prepared_handoff_id,
        latest_launch_id=view.latest_launch_id,
        last_resume_handoff_id=view.last_resume_handoff_id,
        handoffs=tuple(item.to_dict() for item in view.handoffs),
        checkpoint_ids=view.checkpoint_ids,
        launch_ids=view.launch_ids,
        health=health,
        error=error,
        last_valid_event=LastValidEvent(
            event_id=replay_result.head.last_event_id,
            sequence=replay_result.head.last_sequence,
            event_hash=replay_result.head.last_event_hash,
        ),
        broken_paths=broken_paths,
        suggested_repair=suggested_repair,
        alerts=alerts,
    )


def _healthy_report_from_replay(
    manifest: SessionManifest,
    replay_result: ReplayResult,
) -> SessionIntegrityReport:
    return _report_from_replay(
        replay_result,
        health="healthy",
        error=None,
        broken_paths=tuple(),
        suggested_repair=tuple(),
        alerts=tuple(),
    )


def _report_from_manifest(
    manifest: SessionManifest,
    *,
    health: str,
    error: str,
    last_valid_event: LastValidEvent | None,
    broken_paths: tuple[str, ...],
    suggested_repair: tuple[str, ...],
    alerts: tuple[str, ...],
) -> SessionIntegrityReport:
    return SessionIntegrityReport(
        session_id=manifest.session_id,
        storage_model=manifest.storage_model,
        repo_root=manifest.repo_root,
        objective=manifest.objective,
        workstream_kind=manifest.workstream_kind,
        created_at=manifest.created_at,
        updated_at=manifest.created_at,
        initial_agent=manifest.initial_agent,
        current_agent=manifest.initial_agent,
        current_status="corrupt",
        task_status=None,
        next_action="",
        decisions=tuple(),
        blockers=tuple(),
        research_notes=tuple(),
        implementation_notes=tuple(),
        touched_files=tuple(),
        validation={"status": "not_run", "summary": ""},
        latest_checkpoint_id=None,
        prepared_handoff_id=None,
        latest_launch_id=None,
        last_resume_handoff_id=None,
        handoffs=tuple(),
        checkpoint_ids=tuple(),
        launch_ids=tuple(),
        health=health,
        error=error,
        last_valid_event=last_valid_event,
        broken_paths=broken_paths,
        suggested_repair=suggested_repair,
        alerts=alerts,
    )


def _pending_transaction_paths(repo_root: Path, session_id: str) -> tuple[Path, ...]:
    directory = pending_tx_dir(repo_root, session_id)
    if not directory.exists():
        return tuple()
    return tuple(path for path in sorted(directory.iterdir()) if path.is_dir())


def _suggested_repair_commands(
    session_id: str,
    *,
    health: str,
    has_failure: bool,
    has_last_good: bool,
    has_pending: bool,
) -> tuple[str, ...]:
    suggestions: list[str] = []
    if has_pending:
        suggestions.append(f"agent-relay repair {session_id} --rollback-pending")
    if has_failure and health == "degraded" and has_last_good:
        suggestions.append(f"agent-relay repair {session_id} --promote-last-good")
    if has_failure and health == "corrupt" and not has_last_good:
        suggestions.append("restore the session manifest/journal from backup before mutating this session")
    return tuple(suggestions)


def _unique_paths(values: list[Path | None]) -> tuple[str, ...]:
    seen: set[str] = set()
    paths: list[str] = []
    for value in values:
        if value is None:
            continue
        text = str(value)
        if text in seen:
            continue
        seen.add(text)
        paths.append(text)
    return tuple(paths)
