from __future__ import annotations

import json
from pathlib import Path

from agent_relay.errors import CorruptionError, ValidationError
from agent_relay.fs import write_json_atomic
from agent_relay.hashing import sha256_path
from agent_relay.layout import (
    LAYOUT_VERSION,
    derived_view_path,
    head_ref_path,
    is_session_dir,
    journal_dir,
    relay_root,
    session_manifest_path,
    session_root,
    sessions_root,
    version_path,
)
from agent_relay.models import (
    DerivedSessionView,
    JournalEvent,
    ObjectManifest,
    ObjectRef,
    SessionManifest,
    build_session_manifest_hash,
    object_manifest_from_dict,
)
from agent_relay.replay import replay_session


def is_session(repo_root: Path, session_id: str) -> bool:
    return session_manifest_path(repo_root, session_id).exists()


def list_session_ids(repo_root: Path) -> list[str]:
    root = sessions_root(repo_root)
    if not root.exists():
        return []
    ids: list[str] = []
    for candidate in root.iterdir():
        if candidate.is_dir() and is_session_dir(candidate):
            ids.append(candidate.name)
    return sorted(ids)


def load_session_manifest(repo_root: Path, session_id: str) -> SessionManifest:
    _validate_repo_layout_version(repo_root)
    path = session_manifest_path(repo_root, session_id)
    if not path.exists():
        raise SystemExit(f"Session not found: {session_id}")
    manifest = _load_session_manifest_path(path, session_id=session_id)
    if manifest.session_id != session_id:
        raise CorruptionError(
            "session manifest session_id does not match directory name",
            session_id=session_id,
            path=path,
        )
    return manifest


def load_session_view(repo_root: Path, session_id: str) -> DerivedSessionView:
    manifest = load_session_manifest(repo_root, session_id)
    events = load_journal_events(repo_root, session_id)
    session_path = session_root(repo_root, session_id)

    manifest_hash = build_session_manifest_hash(manifest)
    replay_result = replay_session(
        manifest,
        manifest_hash=manifest_hash,
        events=events,
        load_object=lambda ref: _load_object_from_ref(session_path, ref),
    )
    write_json_atomic(derived_view_path(repo_root, session_id), replay_result.view.to_dict())
    write_json_atomic(head_ref_path(repo_root, session_id), replay_result.head.to_dict())
    return replay_result.view


def load_journal_events(repo_root: Path, session_id: str) -> list[JournalEvent]:
    directory = journal_dir(repo_root, session_id)
    if not directory.exists():
        raise CorruptionError("journal directory is missing", session_id=session_id, path=directory)
    events: list[JournalEvent] = []
    for event_path in sorted(directory.glob("*.json")):
        try:
            raw = json.loads(event_path.read_text(encoding="utf-8"))
            event = JournalEvent.from_dict(raw)
            events.append(event)
        except json.JSONDecodeError as exc:
            raise CorruptionError(
                f"journal file is not valid JSON: {exc}",
                session_id=session_id,
                path=event_path,
            ) from exc
        except ValidationError as exc:
            raise CorruptionError(str(exc), session_id=session_id, path=event_path) from exc
    if not events:
        raise CorruptionError("journal is empty", session_id=session_id, path=directory)
    return events


def load_latest_journal_event(repo_root: Path, session_id: str) -> JournalEvent:
    return load_journal_events(repo_root, session_id)[-1]


def load_referenced_object(
    repo_root: Path, session_id: str, object_kind: str, object_id: str
) -> ObjectManifest:
    events = load_journal_events(repo_root, session_id)
    session_path = session_root(repo_root, session_id)
    for event in reversed(events):
        for ref in event.object_refs:
            if ref.object_kind == object_kind and ref.object_id == object_id:
                return _load_object_from_ref(session_path, ref)
    raise SystemExit(f"{object_kind} object not found: {object_id}")


def _load_session_manifest_path(path: Path, *, session_id: str) -> SessionManifest:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        manifest = SessionManifest.from_dict(raw)
    except json.JSONDecodeError as exc:
        raise CorruptionError(
            f"session manifest is not valid JSON: {exc}",
            session_id=session_id,
            path=path,
        ) from exc
    except ValidationError as exc:
        raise CorruptionError(str(exc), session_id=session_id, path=path) from exc
    return manifest


def _validate_repo_layout_version(repo_root: Path) -> None:
    root = relay_root(repo_root)
    if not root.exists():
        raise SystemExit("No .agent-relay directory found")
    path = version_path(repo_root)
    if not path.exists():
        raise CorruptionError("repo layout is missing VERSION", path=path)
    value = path.read_text(encoding="utf-8").strip()
    if value != LAYOUT_VERSION:
        raise CorruptionError(f"unsupported repo layout version: {value}", path=path)


def _load_object_from_ref(session_root_path: Path, ref: ObjectRef):
    manifest_path = session_root_path / ref.manifest_path
    if not manifest_path.exists():
        raise CorruptionError(
            "referenced object manifest is missing",
            session_id=session_root_path.name,
            path=manifest_path,
        )
    actual_hash = sha256_path(manifest_path)
    if actual_hash != ref.manifest_sha256:
        raise CorruptionError(
            "referenced object manifest hash mismatch",
            session_id=session_root_path.name,
            path=manifest_path,
        )
    try:
        manifest = object_manifest_from_dict(json.loads(manifest_path.read_text(encoding="utf-8")))
    except json.JSONDecodeError as exc:
        raise CorruptionError(
            f"object manifest is not valid JSON: {exc}",
            session_id=session_root_path.name,
            path=manifest_path,
        ) from exc
    except ValidationError as exc:
        raise CorruptionError(
            str(exc), session_id=session_root_path.name, path=manifest_path
        ) from exc

    if manifest.object_id != ref.object_id:
        raise CorruptionError(
            "object ref id does not match manifest object_id",
            session_id=session_root_path.name,
            path=manifest_path,
        )
    if manifest.session_id != session_root_path.name:
        raise CorruptionError(
            "object manifest session_id does not match session directory",
            session_id=session_root_path.name,
            path=manifest_path,
        )

    object_dir = manifest_path.parent
    for file_entry in manifest.files:
        candidate = object_dir / file_entry.relative_path
        if not candidate.exists():
            raise CorruptionError(
                "object manifest references a missing file",
                session_id=session_root_path.name,
                path=candidate,
            )
        if sha256_path(candidate) != file_entry.sha256:
            raise CorruptionError(
                "object file hash mismatch",
                session_id=session_root_path.name,
                path=candidate,
            )
        if candidate.stat().st_size != file_entry.size_bytes:
            raise CorruptionError(
                "object file size mismatch",
                session_id=session_root_path.name,
                path=candidate,
            )
    return manifest
