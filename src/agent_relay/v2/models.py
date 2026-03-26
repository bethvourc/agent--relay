from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Mapping

from agent_relay.agents import AGENT_NAMES, LAUNCH_EXECUTE_POLICIES, launch_template_uses_resume_packet
from agent_relay.v2.errors import V2ValidationError
from agent_relay.v2.hashing import canonical_json, sha256_text

SCHEMA_VERSION = 2
VALIDATION_STATUSES = {"not_run", "passed", "failed", "partial"}
WORKSTREAM_KINDS = {"research", "implementation", "mixed"}
SESSION_PHASES = {
    "active",
    "paused",
    "ready_for_handoff",
    "launching",
    "awaiting_resume",
    "completed",
}
TASK_STATUSES = {"working", "blocked", "done"}
JOURNAL_EVENT_TYPES = {
    "session.started",
    "checkpoint.recorded",
    "handoff.prepared",
    "launch.started",
    "launch.finished",
    "resume.accepted",
    "session.completed",
    "repair.rebuilt",
}
OBJECT_KINDS = {"checkpoint", "handoff", "launch"}
LAUNCH_RESULT_STATUSES = {"succeeded", "failed", "interrupted"}
WORKSPACE_CAPTURE_MODES = {"git", "snapshot"}


def _expect_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise V2ValidationError(f"{field_name} must be an object")
    return value


def _require_str(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise V2ValidationError(f"{field_name} must be a non-empty string")
    return value


def _require_int(value: Any, field_name: str) -> int:
    if not isinstance(value, int):
        raise V2ValidationError(f"{field_name} must be an integer")
    return value


def _require_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise V2ValidationError(f"{field_name} must be a boolean")
    return value


def _require_choice(value: Any, field_name: str, choices: set[str]) -> str:
    text = _require_str(value, field_name)
    if text not in choices:
        allowed = ", ".join(sorted(choices))
        raise V2ValidationError(f"{field_name} must be one of: {allowed}")
    return text


def _optional_choice(value: Any, field_name: str, choices: set[str]) -> str | None:
    if value is None:
        return None
    return _require_choice(value, field_name, choices)


def _optional_str(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise V2ValidationError(f"{field_name} must be a string when provided")
    return value


def _require_string_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise V2ValidationError(f"{field_name} must be a list of strings")
    items: list[str] = []
    for index, item in enumerate(value):
        items.append(_require_str(item, f"{field_name}[{index}]"))
    return tuple(items)


def _require_sha256(value: Any, field_name: str) -> str:
    text = _require_str(value, field_name)
    if not text.startswith("sha256:") or len(text) != 71:
        raise V2ValidationError(f"{field_name} must be a sha256:<64-hex> digest")
    return text


def _require_relative_path(value: Any, field_name: str) -> str:
    text = _require_str(value, field_name)
    path = PurePosixPath(text)
    if path.is_absolute():
        raise V2ValidationError(f"{field_name} must be relative")
    if any(part == ".." for part in path.parts):
        raise V2ValidationError(f"{field_name} must not traverse parent directories")
    if text.startswith("./") or text == ".":
        raise V2ValidationError(f"{field_name} must be a clean relative path")
    return text


@dataclass(frozen=True, slots=True)
class ValidationState:
    status: str
    summary: str

    def __post_init__(self) -> None:
        _require_choice(self.status, "validation.status", VALIDATION_STATUSES)
        if not isinstance(self.summary, str):
            raise V2ValidationError("validation.summary must be a string")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ValidationState:
        mapping = _expect_mapping(data, "validation")
        return cls(
            status=mapping["status"],
            summary=mapping["summary"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "summary": self.summary}


@dataclass(frozen=True, slots=True)
class ManifestFile:
    relative_path: str
    sha256: str
    size_bytes: int

    def __post_init__(self) -> None:
        _require_relative_path(self.relative_path, "file.relative_path")
        _require_sha256(self.sha256, "file.sha256")
        if not isinstance(self.size_bytes, int) or self.size_bytes < 0:
            raise V2ValidationError("file.size_bytes must be an integer >= 0")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ManifestFile:
        mapping = _expect_mapping(data, "file")
        return cls(
            relative_path=mapping["relative_path"],
            sha256=mapping["sha256"],
            size_bytes=_require_int(mapping["size_bytes"], "file.size_bytes"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True, slots=True)
class ObjectRef:
    object_kind: str
    object_id: str
    manifest_path: str
    manifest_sha256: str

    def __post_init__(self) -> None:
        _require_choice(self.object_kind, "object_ref.object_kind", OBJECT_KINDS)
        _require_str(self.object_id, "object_ref.object_id")
        _require_relative_path(self.manifest_path, "object_ref.manifest_path")
        _require_sha256(self.manifest_sha256, "object_ref.manifest_sha256")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ObjectRef:
        mapping = _expect_mapping(data, "object_ref")
        return cls(
            object_kind=mapping["object_kind"],
            object_id=mapping["object_id"],
            manifest_path=mapping["manifest_path"],
            manifest_sha256=mapping["manifest_sha256"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "object_kind": self.object_kind,
            "object_id": self.object_id,
            "manifest_path": self.manifest_path,
            "manifest_sha256": self.manifest_sha256,
        }


@dataclass(frozen=True, slots=True)
class SessionManifest:
    schema_version: int
    kind: str
    session_id: str
    repo_root: str
    objective: str
    workstream_kind: str
    initial_agent: str
    created_at: str
    storage_model: str = "journal_v2"

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise V2ValidationError(f"session_manifest.schema_version must be {SCHEMA_VERSION}")
        if self.kind != "session_manifest":
            raise V2ValidationError("session_manifest.kind must be session_manifest")
        _require_str(self.session_id, "session_manifest.session_id")
        _require_str(self.repo_root, "session_manifest.repo_root")
        _require_str(self.objective, "session_manifest.objective")
        _require_choice(self.workstream_kind, "session_manifest.workstream_kind", WORKSTREAM_KINDS)
        _require_choice(self.initial_agent, "session_manifest.initial_agent", set(AGENT_NAMES))
        _require_str(self.created_at, "session_manifest.created_at")
        if self.storage_model != "journal_v2":
            raise V2ValidationError("session_manifest.storage_model must be journal_v2")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SessionManifest:
        mapping = _expect_mapping(data, "session_manifest")
        return cls(
            schema_version=_require_int(mapping["schema_version"], "session_manifest.schema_version"),
            kind=mapping["kind"],
            session_id=mapping["session_id"],
            repo_root=mapping["repo_root"],
            objective=mapping["objective"],
            workstream_kind=mapping["workstream_kind"],
            initial_agent=mapping["initial_agent"],
            created_at=mapping["created_at"],
            storage_model=mapping.get("storage_model", "journal_v2"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "session_id": self.session_id,
            "repo_root": self.repo_root,
            "objective": self.objective,
            "workstream_kind": self.workstream_kind,
            "initial_agent": self.initial_agent,
            "created_at": self.created_at,
            "storage_model": self.storage_model,
        }


@dataclass(frozen=True, slots=True)
class JournalEvent:
    schema_version: int
    kind: str
    session_id: str
    event_id: str
    sequence: int
    type: str
    timestamp: str
    tx_id: str
    phase_before: str | None
    phase_after: str
    payload: Mapping[str, Any]
    object_refs: tuple[ObjectRef, ...]
    prev_event_hash: str | None
    event_hash: str

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise V2ValidationError(f"journal_event.schema_version must be {SCHEMA_VERSION}")
        if self.kind != "journal_event":
            raise V2ValidationError("journal_event.kind must be journal_event")
        _require_str(self.session_id, "journal_event.session_id")
        _require_str(self.event_id, "journal_event.event_id")
        if self.sequence < 1:
            raise V2ValidationError("journal_event.sequence must be >= 1")
        _require_choice(self.type, "journal_event.type", JOURNAL_EVENT_TYPES)
        _require_str(self.timestamp, "journal_event.timestamp")
        _require_str(self.tx_id, "journal_event.tx_id")
        _optional_choice(self.phase_before, "journal_event.phase_before", SESSION_PHASES)
        _require_choice(self.phase_after, "journal_event.phase_after", SESSION_PHASES)
        _expect_mapping(self.payload, "journal_event.payload")
        for index, ref in enumerate(self.object_refs):
            if not isinstance(ref, ObjectRef):
                raise V2ValidationError(f"journal_event.object_refs[{index}] must be an ObjectRef")
        if self.prev_event_hash is not None:
            _require_sha256(self.prev_event_hash, "journal_event.prev_event_hash")
        _require_sha256(self.event_hash, "journal_event.event_hash")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> JournalEvent:
        mapping = _expect_mapping(data, "journal_event")
        payload = _expect_mapping(mapping["payload"], "journal_event.payload")
        refs = mapping.get("object_refs", [])
        if not isinstance(refs, list):
            raise V2ValidationError("journal_event.object_refs must be a list")
        return cls(
            schema_version=_require_int(mapping["schema_version"], "journal_event.schema_version"),
            kind=mapping["kind"],
            session_id=mapping["session_id"],
            event_id=mapping["event_id"],
            sequence=_require_int(mapping["sequence"], "journal_event.sequence"),
            type=mapping["type"],
            timestamp=mapping["timestamp"],
            tx_id=mapping["tx_id"],
            phase_before=mapping.get("phase_before"),
            phase_after=mapping["phase_after"],
            payload=payload,
            object_refs=tuple(ObjectRef.from_dict(item) for item in refs),
            prev_event_hash=mapping.get("prev_event_hash"),
            event_hash=mapping["event_hash"],
        )

    def hash_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "session_id": self.session_id,
            "event_id": self.event_id,
            "sequence": self.sequence,
            "type": self.type,
            "timestamp": self.timestamp,
            "tx_id": self.tx_id,
            "phase_before": self.phase_before,
            "phase_after": self.phase_after,
            "payload": dict(self.payload),
            "object_refs": [ref.to_dict() for ref in self.object_refs],
            "prev_event_hash": self.prev_event_hash,
        }

    def expected_event_hash(self) -> str:
        return sha256_text(canonical_json(self.hash_payload()))

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.hash_payload(),
            "event_hash": self.event_hash,
        }


def _validate_manifest_files(files: tuple[ManifestFile, ...], field_name: str) -> None:
    seen: set[str] = set()
    for index, item in enumerate(files):
        if not isinstance(item, ManifestFile):
            raise V2ValidationError(f"{field_name}[{index}] must be a ManifestFile")
        if item.relative_path in seen:
            raise V2ValidationError(f"{field_name} must not contain duplicate paths")
        seen.add(item.relative_path)


def _load_manifest_files(data: Any, field_name: str) -> tuple[ManifestFile, ...]:
    if not isinstance(data, list):
        raise V2ValidationError(f"{field_name} must be a list")
    files = tuple(ManifestFile.from_dict(item) for item in data)
    _validate_manifest_files(files, field_name)
    return files


def _require_file_reference(relative_path: str | None, files: tuple[ManifestFile, ...], field_name: str) -> str | None:
    if relative_path is None:
        return None
    path = _require_relative_path(relative_path, field_name)
    file_paths = {item.relative_path for item in files}
    if path not in file_paths:
        raise V2ValidationError(f"{field_name} must reference an entry in files")
    return path


@dataclass(frozen=True, slots=True)
class CheckpointManifest:
    schema_version: int
    kind: str
    object_id: str
    session_id: str
    created_at: str
    current_agent: str
    phase_hint: str
    task_status: str
    capture_mode: str
    next_action: str
    decisions: tuple[str, ...]
    blockers: tuple[str, ...]
    research_notes: tuple[str, ...]
    implementation_notes: tuple[str, ...]
    touched_files: tuple[str, ...]
    validation: ValidationState
    repo_state_file: str
    validation_file: str
    summary_file: str | None
    git_head_file: str | None
    workspace_patch_file: str | None
    untracked_manifest_file: str | None
    snapshot_manifest_file: str | None
    files: tuple[ManifestFile, ...]

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise V2ValidationError(f"checkpoint_manifest.schema_version must be {SCHEMA_VERSION}")
        if self.kind != "checkpoint_manifest":
            raise V2ValidationError("checkpoint_manifest.kind must be checkpoint_manifest")
        _require_str(self.object_id, "checkpoint_manifest.object_id")
        _require_str(self.session_id, "checkpoint_manifest.session_id")
        _require_str(self.created_at, "checkpoint_manifest.created_at")
        _require_choice(self.current_agent, "checkpoint_manifest.current_agent", set(AGENT_NAMES))
        _require_choice(self.phase_hint, "checkpoint_manifest.phase_hint", SESSION_PHASES)
        _require_choice(self.task_status, "checkpoint_manifest.task_status", TASK_STATUSES)
        _require_choice(self.capture_mode, "checkpoint_manifest.capture_mode", WORKSPACE_CAPTURE_MODES)
        if not isinstance(self.next_action, str):
            raise V2ValidationError("checkpoint_manifest.next_action must be a string")
        _validate_manifest_files(self.files, "checkpoint_manifest.files")
        _require_file_reference(self.repo_state_file, self.files, "checkpoint_manifest.repo_state_file")
        _require_file_reference(self.validation_file, self.files, "checkpoint_manifest.validation_file")
        _require_file_reference(self.summary_file, self.files, "checkpoint_manifest.summary_file")
        _require_file_reference(self.git_head_file, self.files, "checkpoint_manifest.git_head_file")
        _require_file_reference(
            self.workspace_patch_file,
            self.files,
            "checkpoint_manifest.workspace_patch_file",
        )
        _require_file_reference(
            self.untracked_manifest_file,
            self.files,
            "checkpoint_manifest.untracked_manifest_file",
        )
        _require_file_reference(
            self.snapshot_manifest_file,
            self.files,
            "checkpoint_manifest.snapshot_manifest_file",
        )
        if self.capture_mode == "git":
            if self.git_head_file is None or self.workspace_patch_file is None or self.untracked_manifest_file is None:
                raise V2ValidationError(
                    "checkpoint_manifest git capture requires git_head_file, workspace_patch_file, and untracked_manifest_file",
                )
            if self.snapshot_manifest_file is not None:
                raise V2ValidationError("checkpoint_manifest git capture must not set snapshot_manifest_file")
        if self.capture_mode == "snapshot":
            if self.snapshot_manifest_file is None:
                raise V2ValidationError("checkpoint_manifest snapshot capture requires snapshot_manifest_file")
            if self.git_head_file is not None or self.workspace_patch_file is not None:
                raise V2ValidationError(
                    "checkpoint_manifest snapshot capture must not set git_head_file or workspace_patch_file",
                )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CheckpointManifest:
        mapping = _expect_mapping(data, "checkpoint_manifest")
        return cls(
            schema_version=_require_int(mapping["schema_version"], "checkpoint_manifest.schema_version"),
            kind=mapping["kind"],
            object_id=mapping["object_id"],
            session_id=mapping["session_id"],
            created_at=mapping["created_at"],
            current_agent=mapping["current_agent"],
            phase_hint=mapping["phase_hint"],
            task_status=mapping["task_status"],
            capture_mode=mapping["capture_mode"],
            next_action=mapping["next_action"],
            decisions=_require_string_tuple(mapping["decisions"], "checkpoint_manifest.decisions"),
            blockers=_require_string_tuple(mapping["blockers"], "checkpoint_manifest.blockers"),
            research_notes=_require_string_tuple(
                mapping["research_notes"],
                "checkpoint_manifest.research_notes",
            ),
            implementation_notes=_require_string_tuple(
                mapping["implementation_notes"],
                "checkpoint_manifest.implementation_notes",
            ),
            touched_files=_require_string_tuple(mapping["touched_files"], "checkpoint_manifest.touched_files"),
            validation=ValidationState.from_dict(mapping["validation"]),
            repo_state_file=mapping["repo_state_file"],
            validation_file=mapping["validation_file"],
            summary_file=mapping.get("summary_file"),
            git_head_file=mapping.get("git_head_file"),
            workspace_patch_file=mapping.get("workspace_patch_file"),
            untracked_manifest_file=mapping.get("untracked_manifest_file"),
            snapshot_manifest_file=mapping.get("snapshot_manifest_file"),
            files=_load_manifest_files(mapping.get("files", []), "checkpoint_manifest.files"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "object_id": self.object_id,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "current_agent": self.current_agent,
            "phase_hint": self.phase_hint,
            "task_status": self.task_status,
            "capture_mode": self.capture_mode,
            "next_action": self.next_action,
            "decisions": list(self.decisions),
            "blockers": list(self.blockers),
            "research_notes": list(self.research_notes),
            "implementation_notes": list(self.implementation_notes),
            "touched_files": list(self.touched_files),
            "validation": self.validation.to_dict(),
            "repo_state_file": self.repo_state_file,
            "validation_file": self.validation_file,
            "summary_file": self.summary_file,
            "git_head_file": self.git_head_file,
            "workspace_patch_file": self.workspace_patch_file,
            "untracked_manifest_file": self.untracked_manifest_file,
            "snapshot_manifest_file": self.snapshot_manifest_file,
            "files": [item.to_dict() for item in self.files],
        }


@dataclass(frozen=True, slots=True)
class HandoffManifest:
    schema_version: int
    kind: str
    object_id: str
    session_id: str
    created_at: str
    from_agent: str
    to_agent: str
    reason: str
    source_checkpoint_id: str
    source_event_hash: str
    launch_profile: str
    launch_cwd: str
    launch_command: str
    launch_template: str
    launch_template_source: str
    launch_instructions: str
    launch_packet_aware: bool
    launch_execute_policy: str
    launch_warning: str | None
    packet_file: str
    packet_sha256_file: str
    launch_spec_file: str
    files: tuple[ManifestFile, ...]

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise V2ValidationError(f"handoff_manifest.schema_version must be {SCHEMA_VERSION}")
        if self.kind != "handoff_manifest":
            raise V2ValidationError("handoff_manifest.kind must be handoff_manifest")
        _require_str(self.object_id, "handoff_manifest.object_id")
        _require_str(self.session_id, "handoff_manifest.session_id")
        _require_str(self.created_at, "handoff_manifest.created_at")
        _require_choice(self.from_agent, "handoff_manifest.from_agent", set(AGENT_NAMES))
        _require_choice(self.to_agent, "handoff_manifest.to_agent", set(AGENT_NAMES))
        _require_str(self.reason, "handoff_manifest.reason")
        _require_str(self.source_checkpoint_id, "handoff_manifest.source_checkpoint_id")
        _require_sha256(self.source_event_hash, "handoff_manifest.source_event_hash")
        _require_str(self.launch_profile, "handoff_manifest.launch_profile")
        _require_str(self.launch_cwd, "handoff_manifest.launch_cwd")
        _require_str(self.launch_command, "handoff_manifest.launch_command")
        _require_str(self.launch_template, "handoff_manifest.launch_template")
        _require_str(self.launch_template_source, "handoff_manifest.launch_template_source")
        _require_str(self.launch_instructions, "handoff_manifest.launch_instructions")
        _require_bool(self.launch_packet_aware, "handoff_manifest.launch_packet_aware")
        _require_choice(
            self.launch_execute_policy,
            "handoff_manifest.launch_execute_policy",
            LAUNCH_EXECUTE_POLICIES,
        )
        _optional_str(self.launch_warning, "handoff_manifest.launch_warning")
        if self.launch_execute_policy == "allow" and not self.launch_packet_aware:
            raise V2ValidationError(
                "handoff_manifest.launch_execute_policy cannot be allow when launch_packet_aware is false"
            )
        _validate_manifest_files(self.files, "handoff_manifest.files")
        _require_file_reference(self.packet_file, self.files, "handoff_manifest.packet_file")
        _require_file_reference(self.packet_sha256_file, self.files, "handoff_manifest.packet_sha256_file")
        _require_file_reference(self.launch_spec_file, self.files, "handoff_manifest.launch_spec_file")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> HandoffManifest:
        mapping = _expect_mapping(data, "handoff_manifest")
        return cls(
            schema_version=_require_int(mapping["schema_version"], "handoff_manifest.schema_version"),
            kind=mapping["kind"],
            object_id=mapping["object_id"],
            session_id=mapping["session_id"],
            created_at=mapping["created_at"],
            from_agent=mapping["from_agent"],
            to_agent=mapping["to_agent"],
            reason=mapping["reason"],
            source_checkpoint_id=mapping["source_checkpoint_id"],
            source_event_hash=mapping["source_event_hash"],
            launch_profile=mapping["launch_profile"],
            launch_cwd=mapping["launch_cwd"],
            launch_command=mapping["launch_command"],
            launch_template=mapping["launch_template"],
            launch_template_source=mapping["launch_template_source"],
            launch_instructions=mapping["launch_instructions"],
            launch_packet_aware=mapping.get(
                "launch_packet_aware",
                launch_template_uses_resume_packet(mapping["launch_template"]),
            ),
            launch_execute_policy=mapping.get(
                "launch_execute_policy",
                "allow" if launch_template_uses_resume_packet(mapping["launch_template"]) else "refuse",
            ),
            launch_warning=mapping.get("launch_warning"),
            packet_file=mapping["packet_file"],
            packet_sha256_file=mapping["packet_sha256_file"],
            launch_spec_file=mapping["launch_spec_file"],
            files=_load_manifest_files(mapping.get("files", []), "handoff_manifest.files"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "object_id": self.object_id,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "from_agent": self.from_agent,
            "to_agent": self.to_agent,
            "reason": self.reason,
            "source_checkpoint_id": self.source_checkpoint_id,
            "source_event_hash": self.source_event_hash,
            "launch_profile": self.launch_profile,
            "launch_cwd": self.launch_cwd,
            "launch_command": self.launch_command,
            "launch_template": self.launch_template,
            "launch_template_source": self.launch_template_source,
            "launch_instructions": self.launch_instructions,
            "launch_packet_aware": self.launch_packet_aware,
            "launch_execute_policy": self.launch_execute_policy,
            "launch_warning": self.launch_warning,
            "packet_file": self.packet_file,
            "packet_sha256_file": self.packet_sha256_file,
            "launch_spec_file": self.launch_spec_file,
            "files": [item.to_dict() for item in self.files],
        }


@dataclass(frozen=True, slots=True)
class LaunchManifest:
    schema_version: int
    kind: str
    object_id: str
    session_id: str
    created_at: str
    handoff_id: str
    target_agent: str
    started_at: str
    finished_at: str
    status: str
    exit_code: int
    dispatched_command: str
    stdout_file: str | None
    stderr_file: str | None
    files: tuple[ManifestFile, ...]

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise V2ValidationError(f"launch_manifest.schema_version must be {SCHEMA_VERSION}")
        if self.kind != "launch_manifest":
            raise V2ValidationError("launch_manifest.kind must be launch_manifest")
        _require_str(self.object_id, "launch_manifest.object_id")
        _require_str(self.session_id, "launch_manifest.session_id")
        _require_str(self.created_at, "launch_manifest.created_at")
        _require_str(self.handoff_id, "launch_manifest.handoff_id")
        _require_choice(self.target_agent, "launch_manifest.target_agent", set(AGENT_NAMES))
        _require_str(self.started_at, "launch_manifest.started_at")
        _require_str(self.finished_at, "launch_manifest.finished_at")
        _require_choice(self.status, "launch_manifest.status", LAUNCH_RESULT_STATUSES)
        if not isinstance(self.exit_code, int):
            raise V2ValidationError("launch_manifest.exit_code must be an integer")
        _require_str(self.dispatched_command, "launch_manifest.dispatched_command")
        _validate_manifest_files(self.files, "launch_manifest.files")
        _require_file_reference(self.stdout_file, self.files, "launch_manifest.stdout_file")
        _require_file_reference(self.stderr_file, self.files, "launch_manifest.stderr_file")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> LaunchManifest:
        mapping = _expect_mapping(data, "launch_manifest")
        return cls(
            schema_version=_require_int(mapping["schema_version"], "launch_manifest.schema_version"),
            kind=mapping["kind"],
            object_id=mapping["object_id"],
            session_id=mapping["session_id"],
            created_at=mapping["created_at"],
            handoff_id=mapping["handoff_id"],
            target_agent=mapping["target_agent"],
            started_at=mapping["started_at"],
            finished_at=mapping["finished_at"],
            status=mapping["status"],
            exit_code=_require_int(mapping["exit_code"], "launch_manifest.exit_code"),
            dispatched_command=mapping["dispatched_command"],
            stdout_file=_optional_str(mapping.get("stdout_file"), "launch_manifest.stdout_file"),
            stderr_file=_optional_str(mapping.get("stderr_file"), "launch_manifest.stderr_file"),
            files=_load_manifest_files(mapping.get("files", []), "launch_manifest.files"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "object_id": self.object_id,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "handoff_id": self.handoff_id,
            "target_agent": self.target_agent,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "exit_code": self.exit_code,
            "dispatched_command": self.dispatched_command,
            "stdout_file": self.stdout_file,
            "stderr_file": self.stderr_file,
            "files": [item.to_dict() for item in self.files],
        }


ObjectManifest = CheckpointManifest | HandoffManifest | LaunchManifest


def object_manifest_from_dict(data: Mapping[str, Any]) -> ObjectManifest:
    mapping = _expect_mapping(data, "object_manifest")
    kind = mapping.get("kind")
    if kind == "checkpoint_manifest":
        return CheckpointManifest.from_dict(mapping)
    if kind == "handoff_manifest":
        return HandoffManifest.from_dict(mapping)
    if kind == "launch_manifest":
        return LaunchManifest.from_dict(mapping)
    raise V2ValidationError("object_manifest.kind must be a known manifest kind")


@dataclass(frozen=True, slots=True)
class DerivedHandoffView:
    handoff_id: str
    from_agent: str
    to_agent: str
    reason: str
    prepared_at: str
    checkpoint_id: str
    launch_status: str
    latest_launch_id: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "handoff_id": self.handoff_id,
            "from_agent": self.from_agent,
            "to_agent": self.to_agent,
            "reason": self.reason,
            "prepared_at": self.prepared_at,
            "checkpoint_id": self.checkpoint_id,
            "launch_status": self.launch_status,
            "latest_launch_id": self.latest_launch_id,
        }


@dataclass(frozen=True, slots=True)
class DerivedSessionView:
    schema_version: int
    kind: str
    session_id: str
    storage_model: str
    repo_root: str
    objective: str
    workstream_kind: str
    created_at: str
    updated_at: str
    initial_agent: str
    current_agent: str
    phase: str
    current_status: str
    task_status: str | None
    next_action: str
    decisions: tuple[str, ...]
    blockers: tuple[str, ...]
    research_notes: tuple[str, ...]
    implementation_notes: tuple[str, ...]
    touched_files: tuple[str, ...]
    validation: ValidationState
    latest_checkpoint_id: str | None
    prepared_handoff_id: str | None
    latest_launch_id: str | None
    last_resume_handoff_id: str | None
    event_count: int
    last_event_id: str
    last_event_hash: str
    built_from_sequence: int
    built_from_event_hash: str
    health: str
    handoffs: tuple[DerivedHandoffView, ...]
    checkpoint_ids: tuple[str, ...]
    launch_ids: tuple[str, ...]
    alerts: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise V2ValidationError(f"derived_view.schema_version must be {SCHEMA_VERSION}")
        if self.kind != "derived_session_view":
            raise V2ValidationError("derived_view.kind must be derived_session_view")
        if self.storage_model != "journal_v2":
            raise V2ValidationError("derived_view.storage_model must be journal_v2")
        _require_choice(self.workstream_kind, "derived_view.workstream_kind", WORKSTREAM_KINDS)
        _require_choice(self.initial_agent, "derived_view.initial_agent", set(AGENT_NAMES))
        _require_choice(self.current_agent, "derived_view.current_agent", set(AGENT_NAMES))
        _require_choice(self.phase, "derived_view.phase", SESSION_PHASES)
        _require_choice(self.current_status, "derived_view.current_status", SESSION_PHASES)
        if self.task_status is not None:
            _require_choice(self.task_status, "derived_view.task_status", TASK_STATUSES)
        if self.health != "healthy":
            raise V2ValidationError("derived_view.health must be healthy for persisted healthy views")
        for handoff in self.handoffs:
            if not isinstance(handoff, DerivedHandoffView):
                raise V2ValidationError("derived_view.handoffs entries must be DerivedHandoffView")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DerivedSessionView:
        mapping = _expect_mapping(data, "derived_view")
        return cls(
            schema_version=_require_int(mapping["schema_version"], "derived_view.schema_version"),
            kind=mapping["kind"],
            session_id=mapping["session_id"],
            storage_model=mapping["storage_model"],
            repo_root=mapping["repo_root"],
            objective=mapping["objective"],
            workstream_kind=mapping["workstream_kind"],
            created_at=mapping["created_at"],
            updated_at=mapping["updated_at"],
            initial_agent=mapping["initial_agent"],
            current_agent=mapping["current_agent"],
            phase=mapping["phase"],
            current_status=mapping["current_status"],
            task_status=mapping.get("task_status"),
            next_action=mapping["next_action"],
            decisions=_require_string_tuple(mapping["decisions"], "derived_view.decisions"),
            blockers=_require_string_tuple(mapping["blockers"], "derived_view.blockers"),
            research_notes=_require_string_tuple(mapping["research_notes"], "derived_view.research_notes"),
            implementation_notes=_require_string_tuple(
                mapping["implementation_notes"],
                "derived_view.implementation_notes",
            ),
            touched_files=_require_string_tuple(mapping["touched_files"], "derived_view.touched_files"),
            validation=ValidationState.from_dict(mapping["validation"]),
            latest_checkpoint_id=mapping.get("latest_checkpoint_id"),
            prepared_handoff_id=mapping.get("prepared_handoff_id"),
            latest_launch_id=mapping.get("latest_launch_id"),
            last_resume_handoff_id=mapping.get("last_resume_handoff_id"),
            event_count=_require_int(mapping["event_count"], "derived_view.event_count"),
            last_event_id=mapping["last_event_id"],
            last_event_hash=mapping["last_event_hash"],
            built_from_sequence=_require_int(mapping["built_from_sequence"], "derived_view.built_from_sequence"),
            built_from_event_hash=mapping["built_from_event_hash"],
            health=mapping["health"],
            handoffs=tuple(DerivedHandoffView(**item) for item in mapping.get("handoffs", [])),
            checkpoint_ids=_require_string_tuple(mapping["checkpoint_ids"], "derived_view.checkpoint_ids"),
            launch_ids=_require_string_tuple(mapping["launch_ids"], "derived_view.launch_ids"),
            alerts=_require_string_tuple(mapping.get("alerts", []), "derived_view.alerts"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "session_id": self.session_id,
            "storage_model": self.storage_model,
            "repo_root": self.repo_root,
            "objective": self.objective,
            "workstream_kind": self.workstream_kind,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "initial_agent": self.initial_agent,
            "current_agent": self.current_agent,
            "phase": self.phase,
            "current_status": self.current_status,
            "task_status": self.task_status,
            "next_action": self.next_action,
            "decisions": list(self.decisions),
            "blockers": list(self.blockers),
            "research_notes": list(self.research_notes),
            "implementation_notes": list(self.implementation_notes),
            "touched_files": list(self.touched_files),
            "validation": self.validation.to_dict(),
            "latest_checkpoint_id": self.latest_checkpoint_id,
            "prepared_handoff_id": self.prepared_handoff_id,
            "latest_launch_id": self.latest_launch_id,
            "last_resume_handoff_id": self.last_resume_handoff_id,
            "event_count": self.event_count,
            "last_event_id": self.last_event_id,
            "last_event_hash": self.last_event_hash,
            "built_from_sequence": self.built_from_sequence,
            "built_from_event_hash": self.built_from_event_hash,
            "health": self.health,
            "handoffs": [item.to_dict() for item in self.handoffs],
            "checkpoint_ids": list(self.checkpoint_ids),
            "launch_ids": list(self.launch_ids),
            "alerts": list(self.alerts),
        }

    def dashboard_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "current_agent": self.current_agent,
            "current_status": self.current_status,
            "objective": self.objective,
            "updated_at": self.updated_at,
            "storage_model": self.storage_model,
            "health": self.health,
        }


@dataclass(frozen=True, slots=True)
class HeadRef:
    schema_version: int
    kind: str
    session_id: str
    last_event_id: str
    last_sequence: int
    last_event_hash: str
    updated_at: str

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise V2ValidationError(f"head_ref.schema_version must be {SCHEMA_VERSION}")
        if self.kind != "session_head_ref":
            raise V2ValidationError("head_ref.kind must be session_head_ref")
        _require_str(self.session_id, "head_ref.session_id")
        _require_str(self.last_event_id, "head_ref.last_event_id")
        if self.last_sequence < 1:
            raise V2ValidationError("head_ref.last_sequence must be >= 1")
        _require_sha256(self.last_event_hash, "head_ref.last_event_hash")
        _require_str(self.updated_at, "head_ref.updated_at")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> HeadRef:
        mapping = _expect_mapping(data, "head_ref")
        return cls(
            schema_version=_require_int(mapping["schema_version"], "head_ref.schema_version"),
            kind=mapping["kind"],
            session_id=mapping["session_id"],
            last_event_id=mapping["last_event_id"],
            last_sequence=_require_int(mapping["last_sequence"], "head_ref.last_sequence"),
            last_event_hash=mapping["last_event_hash"],
            updated_at=mapping["updated_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "session_id": self.session_id,
            "last_event_id": self.last_event_id,
            "last_sequence": self.last_sequence,
            "last_event_hash": self.last_event_hash,
            "updated_at": self.updated_at,
        }


def build_session_manifest_hash(manifest: SessionManifest) -> str:
    return sha256_text(canonical_json(manifest.to_dict()))
