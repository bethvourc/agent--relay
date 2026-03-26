from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from agent_relay.fs import write_json_atomic
from agent_relay.v2.integrity import IntegrityScan, SessionIntegrityReport, inspect_session_integrity
from agent_relay.v2.layout import (
    derived_view_path,
    head_ref_path,
    repair_reports_dir,
    quarantine_dir,
    session_root,
)
from agent_relay.v2.locks import acquire_session_lock, utc_now
from agent_relay.v2.storage import load_session_view
from agent_relay.v2.tx import JournalCommitRequest, SessionTransaction, recover_session_transactions

REPAIR_ACTIONS = {"rebuild_view", "rollback_pending", "promote_last_good"}


@dataclass(frozen=True, slots=True)
class RepairCommandResult:
    session_id: str
    action: str
    health_before: str
    health_after: str
    repair_log_path: str
    repair_event_id: str | None
    current_status: str
    last_valid_event: dict[str, str | int] | None
    broken_paths: tuple[str, ...]
    suggested_repair: tuple[str, ...]
    quarantined_paths: tuple[str, ...]
    cleaned_pending_transactions: int

    def to_dict(self) -> dict[str, object]:
        return {
            "command": "repair",
            "session_id": self.session_id,
            "action": self.action,
            "health_before": self.health_before,
            "health_after": self.health_after,
            "repair_log_path": self.repair_log_path,
            "repair_event_id": self.repair_event_id,
            "current_status": self.current_status,
            "last_valid_event": self.last_valid_event,
            "broken_paths": list(self.broken_paths),
            "suggested_repair": list(self.suggested_repair),
            "quarantined_paths": list(self.quarantined_paths),
            "cleaned_pending_transactions": self.cleaned_pending_transactions,
        }


def repair_session(
    repo_root: Path,
    session_id: str,
    *,
    action: str,
    owner: str,
) -> RepairCommandResult:
    if action not in REPAIR_ACTIONS:
        allowed = ", ".join(sorted(REPAIR_ACTIONS))
        raise SystemExit(f"repair action must be one of: {allowed}")

    with acquire_session_lock(repo_root, session_id, owner=owner) as lock:
        before_scan = inspect_session_integrity(repo_root, session_id)
        quarantined_paths: list[str] = []
        cleaned_pending_transactions = 0

        if action == "rebuild_view":
            if before_scan.report.health != "healthy":
                suggestions = ", ".join(before_scan.report.suggested_repair) or "repair the canonical state first"
                raise SystemExit(
                    f"repair --rebuild-view requires a healthy canonical session; run: {suggestions}"
                )
            load_session_view(repo_root, session_id)
        elif action == "rollback_pending":
            if not before_scan.pending_tx_paths:
                raise SystemExit("repair --rollback-pending found no pending transaction residue")
            recovery = recover_session_transactions(repo_root, session_id)
            cleaned_pending_transactions = (
                recovery.cleaned_committed_transactions + recovery.quarantined_transactions
            )
        else:
            promoted = _promote_last_good(repo_root, session_id, before_scan)
            quarantined_paths.extend(promoted)

        after_scan = inspect_session_integrity(repo_root, session_id)
        repair_event_id: str | None = None
        if after_scan.report.health == "healthy":
            repair_event_id = _append_repair_event(
                repo_root,
                session_id,
                action=action,
                owner=owner,
                report=after_scan.report,
                cleaned_pending_transactions=cleaned_pending_transactions,
                quarantined_paths=tuple(quarantined_paths),
                lock=lock,
            )
            after_scan = inspect_session_integrity(repo_root, session_id)

        repair_log_path = _write_repair_receipt(
            repo_root,
            session_id,
            action=action,
            owner=owner,
            before_report=before_scan.report,
            after_report=after_scan.report,
            repair_event_id=repair_event_id,
            cleaned_pending_transactions=cleaned_pending_transactions,
            quarantined_paths=tuple(quarantined_paths),
        )

        return RepairCommandResult(
            session_id=session_id,
            action=action,
            health_before=before_scan.report.health,
            health_after=after_scan.report.health,
            repair_log_path=str(repair_log_path),
            repair_event_id=repair_event_id,
            current_status=after_scan.report.current_status,
            last_valid_event=(
                after_scan.report.last_valid_event.to_dict()
                if after_scan.report.last_valid_event is not None
                else None
            ),
            broken_paths=after_scan.report.broken_paths,
            suggested_repair=after_scan.report.suggested_repair,
            quarantined_paths=tuple(quarantined_paths),
            cleaned_pending_transactions=cleaned_pending_transactions,
        )


def _append_repair_event(
    repo_root: Path,
    session_id: str,
    *,
    action: str,
    owner: str,
    report: SessionIntegrityReport,
    cleaned_pending_transactions: int,
    quarantined_paths: tuple[str, ...],
    lock,
) -> str:
    timestamp = utc_now()
    with SessionTransaction.begin_with_lock(
        repo_root,
        session_id,
        operation=f"repair:{action}",
        owner=owner,
        lock=lock,
    ) as tx:
        event = tx.commit(
            JournalCommitRequest(
                event_type="repair.rebuilt",
                phase_before=report.current_status,
                phase_after=report.current_status,
                payload={
                    "action": action,
                    "requested_by": owner,
                    "cleaned_pending_transactions": cleaned_pending_transactions,
                    "quarantined_paths": list(quarantined_paths),
                },
                timestamp=timestamp,
                object_refs=tuple(),
            )
        )
    return event.event_id


def _write_repair_receipt(
    repo_root: Path,
    session_id: str,
    *,
    action: str,
    owner: str,
    before_report: SessionIntegrityReport,
    after_report: SessionIntegrityReport,
    repair_event_id: str | None,
    cleaned_pending_transactions: int,
    quarantined_paths: tuple[str, ...],
) -> Path:
    timestamp = utc_now().replace(":", "").replace("-", "")
    report_dir = repair_reports_dir(repo_root, session_id)
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"{timestamp}-{action}.json"
    write_json_atomic(
        path,
        {
            "schema_version": 2,
            "kind": "repair_receipt",
            "session_id": session_id,
            "action": action,
            "requested_by": owner,
            "recorded_at": utc_now(),
            "health_before": before_report.health,
            "health_after": after_report.health,
            "repair_event_id": repair_event_id,
            "cleaned_pending_transactions": cleaned_pending_transactions,
            "quarantined_paths": list(quarantined_paths),
            "before_last_valid_event": (
                before_report.last_valid_event.to_dict()
                if before_report.last_valid_event is not None
                else None
            ),
            "after_last_valid_event": (
                after_report.last_valid_event.to_dict()
                if after_report.last_valid_event is not None
                else None
            ),
            "broken_paths": list(after_report.broken_paths),
            "suggested_repair": list(after_report.suggested_repair),
            "error_before": before_report.error,
            "error_after": after_report.error,
        },
    )
    return path


def _promote_last_good(repo_root: Path, session_id: str, scan: IntegrityScan) -> list[str]:
    if scan.report.health != "degraded":
        raise SystemExit("repair --promote-last-good requires a degraded session with a recoverable last valid event")
    if scan.failure_index is None or scan.report.last_valid_event is None:
        raise SystemExit("repair --promote-last-good could not identify a recoverable journal tail")

    quarantined: list[str] = []
    quarantine_root = quarantine_dir(repo_root, session_id)
    quarantine_root.mkdir(parents=True, exist_ok=True)
    session_path = session_root(repo_root, session_id)

    tail_events = scan.event_paths[scan.failure_index :]
    for event_path in tail_events:
        target = quarantine_root / f"journal-tail-{event_path.name}"
        quarantined.append(str(_move_to_quarantine(event_path, target)))

    for event in scan.parsed_events[scan.failure_index :]:
        for ref in event.object_refs:
            object_path = session_path / ref.manifest_path
            object_dir = object_path.parent
            if not object_dir.exists():
                continue
            target = quarantine_root / f"{event.event_id}-{object_dir.name}"
            moved = _move_to_quarantine(object_dir, target)
            path_text = str(moved)
            if path_text not in quarantined:
                quarantined.append(path_text)

    derived_view_path(repo_root, session_id).unlink(missing_ok=True)
    head_ref_path(repo_root, session_id).unlink(missing_ok=True)
    return quarantined


def _move_to_quarantine(source: Path, target: Path) -> Path:
    actual_target = _unique_quarantine_target(target)
    actual_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(actual_target))
    return actual_target


def _unique_quarantine_target(target: Path) -> Path:
    if not target.exists():
        return target
    suffix = 1
    while True:
        candidate = target.with_name(f"{target.name}-{suffix}")
        if not candidate.exists():
            return candidate
        suffix += 1
