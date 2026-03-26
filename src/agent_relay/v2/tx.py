from __future__ import annotations

import json
import secrets
import shutil
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from agent_relay.fs import write_json_atomic, write_text_atomic
from agent_relay.v2.errors import TransactionError
from agent_relay.v2.hashing import sha256_path
from agent_relay.v2.layout import (
    object_dirname,
    pending_tx_dir,
    pending_tx_manifest_path,
    pending_tx_root,
    pending_tx_staging_dir,
    quarantine_dir,
    session_root,
)
from agent_relay.v2.locks import LockHandle, acquire_session_lock, utc_now
from agent_relay.v2.models import (
    JOURNAL_EVENT_TYPES,
    JournalEvent,
    ObjectManifest,
    ObjectRef,
)
from agent_relay.v2.storage import load_journal_events, load_session_manifest, load_session_view

TX_STATES = {"staging", "promoted", "committed"}


def new_tx_id() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"tx-{timestamp}-{secrets.token_hex(4)}"


def _require_str(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TransactionError(f"{field_name} must be a non-empty string")
    return value


def _require_relative_path(value: Any, field_name: str) -> str:
    text = _require_str(value, field_name)
    path = PurePosixPath(text)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        raise TransactionError(f"{field_name} must be a clean relative path")
    return text


@dataclass(frozen=True, slots=True)
class PendingTransactionObject:
    object_kind: str
    object_id: str
    staged_dir: str
    final_dir: str
    manifest_path: str
    manifest_sha256: str

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PendingTransactionObject:
        return cls(
            object_kind=_require_str(data["object_kind"], "pending_object.object_kind"),
            object_id=_require_str(data["object_id"], "pending_object.object_id"),
            staged_dir=_require_relative_path(data["staged_dir"], "pending_object.staged_dir"),
            final_dir=_require_relative_path(data["final_dir"], "pending_object.final_dir"),
            manifest_path=_require_relative_path(data["manifest_path"], "pending_object.manifest_path"),
            manifest_sha256=_require_str(data["manifest_sha256"], "pending_object.manifest_sha256"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "object_kind": self.object_kind,
            "object_id": self.object_id,
            "staged_dir": self.staged_dir,
            "final_dir": self.final_dir,
            "manifest_path": self.manifest_path,
            "manifest_sha256": self.manifest_sha256,
        }


@dataclass(frozen=True, slots=True)
class PendingTransactionManifest:
    schema_version: int
    kind: str
    tx_id: str
    session_id: str
    operation: str
    owner: str
    created_at: str
    updated_at: str
    state: str
    staged_objects: tuple[PendingTransactionObject, ...]
    promoted_object_dirs: tuple[str, ...]
    planned_event_path: str | None
    planned_event_hash: str | None
    planned_event_id: str | None
    planned_event_sequence: int | None

    def __post_init__(self) -> None:
        if self.schema_version != 2:
            raise TransactionError("pending transaction schema_version must be 2")
        if self.kind != "pending_transaction_manifest":
            raise TransactionError("pending transaction kind must be pending_transaction_manifest")
        _require_str(self.tx_id, "pending_tx.tx_id")
        _require_str(self.session_id, "pending_tx.session_id")
        _require_str(self.operation, "pending_tx.operation")
        _require_str(self.owner, "pending_tx.owner")
        _require_str(self.created_at, "pending_tx.created_at")
        _require_str(self.updated_at, "pending_tx.updated_at")
        if self.state not in TX_STATES:
            raise TransactionError("pending_tx.state must be staging, promoted, or committed")
        for item in self.staged_objects:
            if not isinstance(item, PendingTransactionObject):
                raise TransactionError("pending_tx.staged_objects entries must be PendingTransactionObject")
        for item in self.promoted_object_dirs:
            _require_relative_path(item, "pending_tx.promoted_object_dirs[]")
        if self.planned_event_path is not None:
            _require_relative_path(self.planned_event_path, "pending_tx.planned_event_path")
        if self.planned_event_hash is not None:
            _require_str(self.planned_event_hash, "pending_tx.planned_event_hash")
        if self.planned_event_id is not None:
            _require_str(self.planned_event_id, "pending_tx.planned_event_id")
        if self.planned_event_sequence is not None and self.planned_event_sequence < 1:
            raise TransactionError("pending_tx.planned_event_sequence must be >= 1")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PendingTransactionManifest:
        staged = data.get("staged_objects", [])
        promoted = data.get("promoted_object_dirs", [])
        if not isinstance(staged, list):
            raise TransactionError("pending_tx.staged_objects must be a list")
        if not isinstance(promoted, list):
            raise TransactionError("pending_tx.promoted_object_dirs must be a list")
        return cls(
            schema_version=int(data["schema_version"]),
            kind=_require_str(data["kind"], "pending_tx.kind"),
            tx_id=_require_str(data["tx_id"], "pending_tx.tx_id"),
            session_id=_require_str(data["session_id"], "pending_tx.session_id"),
            operation=_require_str(data["operation"], "pending_tx.operation"),
            owner=_require_str(data["owner"], "pending_tx.owner"),
            created_at=_require_str(data["created_at"], "pending_tx.created_at"),
            updated_at=_require_str(data["updated_at"], "pending_tx.updated_at"),
            state=_require_str(data["state"], "pending_tx.state"),
            staged_objects=tuple(PendingTransactionObject.from_dict(item) for item in staged),
            promoted_object_dirs=tuple(_require_relative_path(item, "pending_tx.promoted_object_dirs[]") for item in promoted),
            planned_event_path=data.get("planned_event_path"),
            planned_event_hash=data.get("planned_event_hash"),
            planned_event_id=data.get("planned_event_id"),
            planned_event_sequence=data.get("planned_event_sequence"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "tx_id": self.tx_id,
            "session_id": self.session_id,
            "operation": self.operation,
            "owner": self.owner,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "state": self.state,
            "staged_objects": [item.to_dict() for item in self.staged_objects],
            "promoted_object_dirs": list(self.promoted_object_dirs),
            "planned_event_path": self.planned_event_path,
            "planned_event_hash": self.planned_event_hash,
            "planned_event_id": self.planned_event_id,
            "planned_event_sequence": self.planned_event_sequence,
        }


@dataclass(frozen=True, slots=True)
class JournalCommitRequest:
    event_type: str
    phase_before: str | None
    phase_after: str
    payload: Mapping[str, Any]
    timestamp: str
    object_refs: tuple[ObjectRef, ...] | None = None

    def __post_init__(self) -> None:
        if self.event_type not in JOURNAL_EVENT_TYPES:
            raise TransactionError(f"Unsupported event type: {self.event_type}")
        if not isinstance(self.payload, Mapping):
            raise TransactionError("commit request payload must be an object")
        _require_str(self.timestamp, "commit_request.timestamp")


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    quarantined_transactions: int
    cleaned_committed_transactions: int
    rebuilt_caches: bool


class SessionTransaction:
    def __init__(
        self,
        *,
        repo_root: Path,
        session_id: str,
        operation: str,
        owner: str,
        lock: LockHandle,
        release_lock_on_close: bool = True,
    ) -> None:
        self.repo_root = repo_root
        self.session_id = session_id
        self.operation = operation
        self.owner = owner
        self.lock = lock
        self._release_lock_on_close = release_lock_on_close
        self.tx_id = new_tx_id()
        self.created_at = utc_now()
        self._pending_root = pending_tx_root(repo_root, session_id, self.tx_id)
        self._staging_root = pending_tx_staging_dir(repo_root, session_id, self.tx_id)
        self._manifest = PendingTransactionManifest(
            schema_version=2,
            kind="pending_transaction_manifest",
            tx_id=self.tx_id,
            session_id=session_id,
            operation=operation,
            owner=owner,
            created_at=self.created_at,
            updated_at=self.created_at,
            state="staging",
            staged_objects=tuple(),
            promoted_object_dirs=tuple(),
            planned_event_path=None,
            planned_event_hash=None,
            planned_event_id=None,
            planned_event_sequence=None,
        )
        self._pending_root.mkdir(parents=True, exist_ok=False)
        self._staging_root.mkdir(parents=True, exist_ok=True)
        self._write_manifest()
        self._closed = False
        self._committed = False

    @classmethod
    def begin(
        cls,
        repo_root: Path,
        session_id: str,
        *,
        operation: str,
        owner: str,
        timeout_seconds: float = 5.0,
        poll_interval_seconds: float = 0.05,
    ) -> SessionTransaction:
        lock = acquire_session_lock(
            repo_root,
            session_id,
            owner=owner,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        try:
            load_session_manifest(repo_root, session_id)
            recover_session_transactions(repo_root, session_id)
            return cls(
                repo_root=repo_root,
                session_id=session_id,
                operation=operation,
                owner=owner,
                lock=lock,
            )
        except Exception:
            lock.release()
            raise

    @classmethod
    def begin_with_lock(
        cls,
        repo_root: Path,
        session_id: str,
        *,
        operation: str,
        owner: str,
        lock: LockHandle,
    ) -> SessionTransaction:
        load_session_manifest(repo_root, session_id)
        recover_session_transactions(repo_root, session_id)
        return cls(
            repo_root=repo_root,
            session_id=session_id,
            operation=operation,
            owner=owner,
            lock=lock,
            release_lock_on_close=False,
        )

    def __enter__(self) -> SessionTransaction:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        if self._release_lock_on_close:
            self.lock.release()
        self._closed = True

    def stage_manifest_object(
        self,
        manifest: ObjectManifest,
        *,
        file_contents: Mapping[str, str | bytes],
    ) -> ObjectRef:
        if manifest.session_id != self.session_id:
            raise TransactionError("staged object session_id must match the transaction session")

        object_kind = _object_kind_from_manifest(manifest)
        final_dir_relative = f"objects/{object_dirname(object_kind)}/{manifest.object_id}"
        if any(item.final_dir == final_dir_relative for item in self._manifest.staged_objects):
            raise TransactionError(f"Object already staged in this transaction: {final_dir_relative}")

        staged_dir_relative = f"staging/{final_dir_relative}"
        staged_object_dir = self._pending_root / staged_dir_relative
        staged_object_dir.mkdir(parents=True, exist_ok=False)

        expected_paths = {file_entry.relative_path for file_entry in manifest.files}
        provided_paths = set(file_contents)
        if provided_paths != expected_paths:
            raise TransactionError("staged file set must exactly match manifest.files")

        for relative_path, content in file_contents.items():
            cleaned = _require_relative_path(relative_path, "staged_file.relative_path")
            destination = staged_object_dir / cleaned
            destination.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                destination.write_bytes(content)
            else:
                write_text_atomic(destination, content)

        for file_entry in manifest.files:
            candidate = staged_object_dir / file_entry.relative_path
            if not candidate.exists():
                raise TransactionError(f"staged object is missing expected file {file_entry.relative_path}")
            if sha256_path(candidate) != file_entry.sha256:
                raise TransactionError(f"staged object file hash mismatch for {file_entry.relative_path}")
            if candidate.stat().st_size != file_entry.size_bytes:
                raise TransactionError(f"staged object file size mismatch for {file_entry.relative_path}")

        manifest_path = staged_object_dir / "manifest.json"
        write_json_atomic(manifest_path, manifest.to_dict())
        manifest_relative = f"{final_dir_relative}/manifest.json"
        object_ref = ObjectRef(
            object_kind=object_kind,
            object_id=manifest.object_id,
            manifest_path=manifest_relative,
            manifest_sha256=sha256_path(manifest_path),
        )
        staged_object = PendingTransactionObject(
            object_kind=object_kind,
            object_id=manifest.object_id,
            staged_dir=staged_dir_relative,
            final_dir=final_dir_relative,
            manifest_path=object_ref.manifest_path,
            manifest_sha256=object_ref.manifest_sha256,
        )
        self._manifest = replace(
            self._manifest,
            updated_at=utc_now(),
            staged_objects=(*self._manifest.staged_objects, staged_object),
        )
        self._write_manifest()
        return object_ref

    def commit(self, request: JournalCommitRequest, *, cleanup: bool = True) -> JournalEvent:
        if self._committed:
            raise TransactionError("transaction has already been committed")

        staged_refs = tuple(
            ObjectRef(
                object_kind=item.object_kind,
                object_id=item.object_id,
                manifest_path=item.manifest_path,
                manifest_sha256=item.manifest_sha256,
            )
            for item in self._manifest.staged_objects
        )
        object_refs = request.object_refs if request.object_refs is not None else staged_refs
        if tuple(object_refs) != staged_refs:
            raise TransactionError("commit request object_refs must match the transaction's staged objects exactly")

        events = load_journal_events(self.repo_root, self.session_id)
        last_event = events[-1]
        sequence = last_event.sequence + 1
        event_id = f"ev-{sequence:06d}"
        event = JournalEvent(
            schema_version=2,
            kind="journal_event",
            session_id=self.session_id,
            event_id=event_id,
            sequence=sequence,
            type=request.event_type,
            timestamp=request.timestamp,
            tx_id=self.tx_id,
            phase_before=request.phase_before,
            phase_after=request.phase_after,
            payload=dict(request.payload),
            object_refs=tuple(object_refs),
            prev_event_hash=last_event.event_hash,
            event_hash="sha256:" + ("0" * 64),
        )
        event = JournalEvent.from_dict({**event.to_dict(), "event_hash": event.expected_event_hash()})
        event_path_relative = f"journal/{sequence:06d}-{request.event_type}.json"

        self._manifest = replace(
            self._manifest,
            updated_at=utc_now(),
            planned_event_path=event_path_relative,
            planned_event_hash=event.event_hash,
            planned_event_id=event.event_id,
            planned_event_sequence=event.sequence,
        )
        self._write_manifest()

        self._promote_staged_objects()

        final_event_path = session_root(self.repo_root, self.session_id) / event_path_relative
        write_json_atomic(final_event_path, event.to_dict())

        self._manifest = replace(
            self._manifest,
            updated_at=utc_now(),
            state="committed",
        )
        self._write_manifest()

        load_session_view(self.repo_root, self.session_id)
        self._committed = True

        if cleanup:
            shutil.rmtree(self._pending_root, ignore_errors=True)

        return event

    def _promote_staged_objects(self) -> None:
        if self._manifest.state == "committed":
            raise TransactionError("cannot promote objects for a committed transaction")

        promoted = list(self._manifest.promoted_object_dirs)
        for item in self._manifest.staged_objects:
            if item.final_dir in promoted:
                continue
            source = self._pending_root / item.staged_dir
            target = session_root(self.repo_root, self.session_id) / item.final_dir
            if target.exists():
                raise TransactionError(f"refusing to overwrite immutable object directory {target}")
            target.parent.mkdir(parents=True, exist_ok=True)
            source.rename(target)
            promoted.append(item.final_dir)
            self._manifest = replace(
                self._manifest,
                updated_at=utc_now(),
                state="promoted",
                promoted_object_dirs=tuple(promoted),
            )
            self._write_manifest()

    def _write_manifest(self) -> None:
        write_json_atomic(
            pending_tx_manifest_path(self.repo_root, self.session_id, self.tx_id),
            self._manifest.to_dict(),
        )


def recover_session_transactions(repo_root: Path, session_id: str) -> RecoveryReport:
    pending_root = pending_tx_dir(repo_root, session_id)
    pending_root.mkdir(parents=True, exist_ok=True)
    quarantine_root = quarantine_dir(repo_root, session_id)
    quarantine_root.mkdir(parents=True, exist_ok=True)

    quarantined = 0
    committed_dirs: list[Path] = []
    rebuild_required = False
    for candidate in sorted(pending_root.iterdir()):
        if not candidate.is_dir():
            continue
        manifest_path = candidate / "transaction.json"
        try:
            if not manifest_path.exists():
                raise TransactionError("pending transaction is missing transaction.json")
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest = PendingTransactionManifest.from_dict(raw)
        except (json.JSONDecodeError, TransactionError) as exc:
            _quarantine_path(candidate, quarantine_root / f"{candidate.name}-broken")
            quarantined += 1
            continue

        if manifest.session_id != session_id:
            _quarantine_path(candidate, quarantine_root / f"{candidate.name}-wrong-session")
            quarantined += 1
            continue

        journal_exists = False
        if manifest.planned_event_path:
            journal_path = session_root(repo_root, session_id) / manifest.planned_event_path
            journal_exists = journal_path.exists()

        if journal_exists:
            committed_dirs.append(candidate)
            rebuild_required = True
            continue

        for promoted_dir in manifest.promoted_object_dirs:
            promoted_path = session_root(repo_root, session_id) / promoted_dir
            if promoted_path.exists():
                _quarantine_path(
                    promoted_path,
                    quarantine_root / f"{candidate.name}-{Path(promoted_dir).name}",
                )
        _quarantine_path(candidate, quarantine_root / f"{candidate.name}-abandoned")
        quarantined += 1

    cleaned_committed = 0
    if rebuild_required:
        load_session_view(repo_root, session_id)
        for candidate in committed_dirs:
            shutil.rmtree(candidate, ignore_errors=True)
            cleaned_committed += 1

    return RecoveryReport(
        quarantined_transactions=quarantined,
        cleaned_committed_transactions=cleaned_committed,
        rebuilt_caches=rebuild_required,
    )


def _quarantine_path(source: Path, target: Path) -> None:
    if not source.exists():
        return
    target = _unique_quarantine_target(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))


def _unique_quarantine_target(target: Path) -> Path:
    if not target.exists():
        return target
    suffix = 1
    while True:
        candidate = target.with_name(f"{target.name}-{suffix}")
        if not candidate.exists():
            return candidate
        suffix += 1


def _object_kind_from_manifest(manifest: ObjectManifest) -> str:
    manifest_kind = manifest.kind
    if manifest_kind == "checkpoint_manifest":
        return "checkpoint"
    if manifest_kind == "handoff_manifest":
        return "handoff"
    if manifest_kind == "launch_manifest":
        return "launch"
    raise TransactionError(f"Unsupported manifest kind: {manifest_kind}")
