from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from agent_relay.agents import AGENT_NAMES, LAUNCH_EXECUTE_POLICIES, launch_template_uses_resume_packet

SCHEMA_VERSION = 1
VALIDATION_STATUSES = {"not_run", "passed", "failed", "partial"}
WORKSTREAM_KINDS = {"research", "implementation", "mixed"}
SESSION_STATUSES = {
    "active",
    "paused",
    "blocked",
    "completed",
    "handoff_prepared",
    "launching",
    "launch_failed",
}
LAUNCH_STATUSES = {"ready", "launching", "succeeded", "failed"}


class ModelValidationError(ValueError):
    """Raised when persisted session state is malformed."""


def _expect_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ModelValidationError(f"{field_name} must be an object")
    return value


def _require_str(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ModelValidationError(f"{field_name} must be a non-empty string")
    return value


def _optional_str(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ModelValidationError(f"{field_name} must be a string when provided")
    return value


def _require_int(value: Any, field_name: str) -> int:
    if not isinstance(value, int):
        raise ModelValidationError(f"{field_name} must be an integer")
    return value


def _require_choice(value: Any, field_name: str, choices: set[str]) -> str:
    text = _require_str(value, field_name)
    if text not in choices:
        allowed = ", ".join(sorted(choices))
        raise ModelValidationError(f"{field_name} must be one of: {allowed}")
    return text


def _require_str_list(value: Any, field_name: str) -> list[str]:
    if not isinstance(value, list):
        raise ModelValidationError(f"{field_name} must be a list of strings")
    items: list[str] = []
    for index, item in enumerate(value):
        items.append(_require_str(item, f"{field_name}[{index}]"))
    return items


def _require_artifacts(value: Any, field_name: str) -> dict[str, str | list[str]]:
    mapping = _expect_mapping(value, field_name)
    artifacts: dict[str, str | list[str]] = {}
    for key, item in mapping.items():
        artifact_key = _require_str(key, f"{field_name} key")
        if isinstance(item, str):
            artifacts[artifact_key] = item
            continue
        if isinstance(item, list):
            artifacts[artifact_key] = _require_str_list(item, f"{field_name}.{artifact_key}")
            continue
        raise ModelValidationError(f"{field_name}.{artifact_key} must be a string or list of strings")
    return artifacts


@dataclass
class ValidationState:
    status: str
    summary: str

    def __post_init__(self) -> None:
        self.status = _require_choice(self.status, "validation.status", VALIDATION_STATUSES)
        if not isinstance(self.summary, str):
            raise ModelValidationError("validation.summary must be a string")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ValidationState:
        mapping = _expect_mapping(data, "validation")
        if "status" not in mapping:
            raise ModelValidationError("validation.status is required")
        if "summary" not in mapping:
            raise ModelValidationError("validation.summary is required")
        return cls(
            status=mapping["status"],
            summary=mapping["summary"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "summary": self.summary,
        }


@dataclass
class HandoffRecord:
    from_agent: str
    to_agent: str
    reason: str
    prepared_at: str
    checkpoint_id: str
    resume_packet_path: str
    launch_status: str
    launch_profile: str
    launch_cwd: str
    launch_command: str
    launch_template: str
    launch_template_source: str
    launch_instructions: str
    launch_packet_aware: bool = True
    launch_execute_policy: str = "allow"
    launch_warning: str | None = None
    launched_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None

    def __post_init__(self) -> None:
        self.from_agent = _require_choice(self.from_agent, "handoff.from_agent", set(AGENT_NAMES))
        self.to_agent = _require_choice(self.to_agent, "handoff.to_agent", set(AGENT_NAMES))
        self.reason = _require_str(self.reason, "handoff.reason")
        self.prepared_at = _require_str(self.prepared_at, "handoff.prepared_at")
        self.checkpoint_id = _require_str(self.checkpoint_id, "handoff.checkpoint_id")
        self.resume_packet_path = _require_str(self.resume_packet_path, "handoff.resume_packet_path")
        self.launch_status = _require_choice(self.launch_status, "handoff.launch_status", LAUNCH_STATUSES)
        self.launch_profile = _require_str(self.launch_profile, "handoff.launch_profile")
        self.launch_cwd = _require_str(self.launch_cwd, "handoff.launch_cwd")
        self.launch_command = _require_str(self.launch_command, "handoff.launch_command")
        self.launch_template = _require_str(self.launch_template, "handoff.launch_template")
        self.launch_template_source = _require_str(
            self.launch_template_source,
            "handoff.launch_template_source",
        )
        self.launch_instructions = _require_str(self.launch_instructions, "handoff.launch_instructions")
        if not isinstance(self.launch_packet_aware, bool):
            raise ModelValidationError("handoff.launch_packet_aware must be a boolean")
        self.launch_execute_policy = _require_choice(
            self.launch_execute_policy,
            "handoff.launch_execute_policy",
            LAUNCH_EXECUTE_POLICIES,
        )
        self.launch_warning = _optional_str(self.launch_warning, "handoff.launch_warning")
        if self.launch_execute_policy == "allow" and not self.launch_packet_aware:
            raise ModelValidationError("handoff.launch_execute_policy cannot be allow when launch_packet_aware is false")
        self.launched_at = _optional_str(self.launched_at, "handoff.launched_at")
        self.finished_at = _optional_str(self.finished_at, "handoff.finished_at")
        if self.exit_code is not None and not isinstance(self.exit_code, int):
            raise ModelValidationError("handoff.exit_code must be an integer when provided")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> HandoffRecord:
        mapping = _expect_mapping(data, "handoff")
        required_fields = [
            "from_agent",
            "to_agent",
            "reason",
            "prepared_at",
            "checkpoint_id",
            "resume_packet_path",
            "launch_status",
            "launch_profile",
            "launch_cwd",
            "launch_command",
            "launch_template",
            "launch_template_source",
            "launch_instructions",
        ]
        missing = [field_name for field_name in required_fields if field_name not in mapping]
        if missing:
            raise ModelValidationError(f"handoff is missing required fields: {', '.join(missing)}")
        return cls(
            from_agent=mapping["from_agent"],
            to_agent=mapping["to_agent"],
            reason=mapping["reason"],
            prepared_at=mapping["prepared_at"],
            checkpoint_id=mapping["checkpoint_id"],
            resume_packet_path=mapping["resume_packet_path"],
            launch_status=mapping["launch_status"],
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
            launched_at=mapping.get("launched_at"),
            finished_at=mapping.get("finished_at"),
            exit_code=mapping.get("exit_code"),
        )

    def to_dict(self) -> dict[str, Any]:
        data = {
            "from_agent": self.from_agent,
            "to_agent": self.to_agent,
            "reason": self.reason,
            "prepared_at": self.prepared_at,
            "checkpoint_id": self.checkpoint_id,
            "resume_packet_path": self.resume_packet_path,
            "launch_status": self.launch_status,
            "launch_profile": self.launch_profile,
            "launch_cwd": self.launch_cwd,
            "launch_command": self.launch_command,
            "launch_template": self.launch_template,
            "launch_template_source": self.launch_template_source,
            "launch_instructions": self.launch_instructions,
            "launch_packet_aware": self.launch_packet_aware,
            "launch_execute_policy": self.launch_execute_policy,
        }
        if self.launch_warning is not None:
            data["launch_warning"] = self.launch_warning
        if self.launched_at is not None:
            data["launched_at"] = self.launched_at
        if self.finished_at is not None:
            data["finished_at"] = self.finished_at
        if self.exit_code is not None:
            data["exit_code"] = self.exit_code
        return data


@dataclass
class CheckpointRecord:
    checkpoint_id: str
    session_id: str
    created_at: str
    status: str
    next_action: str
    decisions: list[str]
    blockers: list[str]
    research_notes: list[str]
    implementation_notes: list[str]
    touched_files: list[str]
    validation: ValidationState
    artifacts: dict[str, str | list[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.checkpoint_id = _require_str(self.checkpoint_id, "checkpoint.checkpoint_id")
        self.session_id = _require_str(self.session_id, "checkpoint.session_id")
        self.created_at = _require_str(self.created_at, "checkpoint.created_at")
        self.status = _require_choice(self.status, "checkpoint.status", SESSION_STATUSES)
        if not isinstance(self.next_action, str):
            raise ModelValidationError("checkpoint.next_action must be a string")
        self.decisions = _require_str_list(self.decisions, "checkpoint.decisions")
        self.blockers = _require_str_list(self.blockers, "checkpoint.blockers")
        self.research_notes = _require_str_list(self.research_notes, "checkpoint.research_notes")
        self.implementation_notes = _require_str_list(
            self.implementation_notes,
            "checkpoint.implementation_notes",
        )
        self.touched_files = _require_str_list(self.touched_files, "checkpoint.touched_files")
        if not isinstance(self.validation, ValidationState):
            raise ModelValidationError("checkpoint.validation must be a ValidationState")
        self.artifacts = _require_artifacts(self.artifacts, "checkpoint.artifacts")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CheckpointRecord:
        mapping = _expect_mapping(data, "checkpoint")
        required_fields = [
            "checkpoint_id",
            "session_id",
            "created_at",
            "status",
            "next_action",
            "decisions",
            "blockers",
            "research_notes",
            "implementation_notes",
            "touched_files",
            "validation",
            "artifacts",
        ]
        missing = [field_name for field_name in required_fields if field_name not in mapping]
        if missing:
            raise ModelValidationError(f"checkpoint is missing required fields: {', '.join(missing)}")
        return cls(
            checkpoint_id=mapping["checkpoint_id"],
            session_id=mapping["session_id"],
            created_at=mapping["created_at"],
            status=mapping["status"],
            next_action=mapping["next_action"],
            decisions=mapping["decisions"],
            blockers=mapping["blockers"],
            research_notes=mapping["research_notes"],
            implementation_notes=mapping["implementation_notes"],
            touched_files=mapping["touched_files"],
            validation=ValidationState.from_dict(mapping["validation"]),
            artifacts=_require_artifacts(mapping["artifacts"], "checkpoint.artifacts"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "status": self.status,
            "next_action": self.next_action,
            "decisions": self.decisions,
            "blockers": self.blockers,
            "research_notes": self.research_notes,
            "implementation_notes": self.implementation_notes,
            "touched_files": self.touched_files,
            "validation": self.validation.to_dict(),
            "artifacts": self.artifacts,
        }


@dataclass
class SessionState:
    schema_version: int
    session_id: str
    repo_root: str
    objective: str
    workstream_kind: str
    current_agent: str
    current_status: str
    created_at: str
    updated_at: str
    next_action: str
    decisions: list[str]
    blockers: list[str]
    research_notes: list[str]
    implementation_notes: list[str]
    touched_files: list[str]
    validation: ValidationState
    handoffs: list[HandoffRecord]
    latest_checkpoint_id: str | None = None

    def __post_init__(self) -> None:
        self.schema_version = _require_int(self.schema_version, "session.schema_version")
        if self.schema_version != SCHEMA_VERSION:
            raise ModelValidationError(
                f"session.schema_version must be {SCHEMA_VERSION}, got {self.schema_version}"
            )
        self.session_id = _require_str(self.session_id, "session.session_id")
        self.repo_root = _require_str(self.repo_root, "session.repo_root")
        self.objective = _require_str(self.objective, "session.objective")
        self.workstream_kind = _require_choice(
            self.workstream_kind,
            "session.workstream_kind",
            WORKSTREAM_KINDS,
        )
        self.current_agent = _require_choice(self.current_agent, "session.current_agent", set(AGENT_NAMES))
        self.current_status = _require_choice(
            self.current_status,
            "session.current_status",
            SESSION_STATUSES,
        )
        self.created_at = _require_str(self.created_at, "session.created_at")
        self.updated_at = _require_str(self.updated_at, "session.updated_at")
        if not isinstance(self.next_action, str):
            raise ModelValidationError("session.next_action must be a string")
        self.decisions = _require_str_list(self.decisions, "session.decisions")
        self.blockers = _require_str_list(self.blockers, "session.blockers")
        self.research_notes = _require_str_list(self.research_notes, "session.research_notes")
        self.implementation_notes = _require_str_list(
            self.implementation_notes,
            "session.implementation_notes",
        )
        self.touched_files = _require_str_list(self.touched_files, "session.touched_files")
        if not isinstance(self.validation, ValidationState):
            raise ModelValidationError("session.validation must be a ValidationState")
        if not isinstance(self.handoffs, list):
            raise ModelValidationError("session.handoffs must be a list")
        for index, handoff in enumerate(self.handoffs):
            if not isinstance(handoff, HandoffRecord):
                raise ModelValidationError(f"session.handoffs[{index}] must be a HandoffRecord")
        self.latest_checkpoint_id = _optional_str(
            self.latest_checkpoint_id,
            "session.latest_checkpoint_id",
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SessionState:
        mapping = _expect_mapping(data, "session")
        required_fields = [
            "schema_version",
            "session_id",
            "repo_root",
            "objective",
            "workstream_kind",
            "current_agent",
            "current_status",
            "created_at",
            "updated_at",
            "next_action",
            "decisions",
            "blockers",
            "research_notes",
            "implementation_notes",
            "touched_files",
            "validation",
            "handoffs",
        ]
        missing = [field_name for field_name in required_fields if field_name not in mapping]
        if missing:
            raise ModelValidationError(f"session is missing required fields: {', '.join(missing)}")
        handoffs_data = mapping["handoffs"]
        if not isinstance(handoffs_data, list):
            raise ModelValidationError("session.handoffs must be a list")
        handoffs = [HandoffRecord.from_dict(item) for item in handoffs_data]
        return cls(
            schema_version=mapping["schema_version"],
            session_id=mapping["session_id"],
            repo_root=mapping["repo_root"],
            objective=mapping["objective"],
            workstream_kind=mapping["workstream_kind"],
            current_agent=mapping["current_agent"],
            current_status=mapping["current_status"],
            created_at=mapping["created_at"],
            updated_at=mapping["updated_at"],
            next_action=mapping["next_action"],
            decisions=mapping["decisions"],
            blockers=mapping["blockers"],
            research_notes=mapping["research_notes"],
            implementation_notes=mapping["implementation_notes"],
            touched_files=mapping["touched_files"],
            validation=ValidationState.from_dict(mapping["validation"]),
            handoffs=handoffs,
            latest_checkpoint_id=mapping.get("latest_checkpoint_id"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "repo_root": self.repo_root,
            "objective": self.objective,
            "workstream_kind": self.workstream_kind,
            "current_agent": self.current_agent,
            "current_status": self.current_status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "next_action": self.next_action,
            "decisions": self.decisions,
            "blockers": self.blockers,
            "research_notes": self.research_notes,
            "implementation_notes": self.implementation_notes,
            "touched_files": self.touched_files,
            "validation": self.validation.to_dict(),
            "handoffs": [handoff.to_dict() for handoff in self.handoffs],
            "latest_checkpoint_id": self.latest_checkpoint_id,
        }
