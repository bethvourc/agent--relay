from __future__ import annotations

from dataclasses import dataclass

from agent_relay.v2.models import LAUNCH_RESULT_STATUSES, SESSION_PHASES, TASK_STATUSES

CHECKPOINT_COMMANDS = {"checkpoint", "pause", "prepare"}
CHECKPOINT_STATUS_DIRECTIVES = {"active", "paused", "blocked", "done"}


class LifecycleViolation(ValueError):
    """Raised when a command or journal event violates the lifecycle state machine."""


@dataclass(frozen=True, slots=True)
class LifecycleState:
    phase: str
    task_status: str | None

    def __post_init__(self) -> None:
        if self.phase not in SESSION_PHASES:
            allowed = ", ".join(sorted(SESSION_PHASES))
            raise LifecycleViolation(f"phase must be one of: {allowed}")
        if self.task_status is not None and self.task_status not in TASK_STATUSES:
            allowed = ", ".join(sorted(TASK_STATUSES))
            raise LifecycleViolation(f"task_status must be one of: {allowed}")


@dataclass(frozen=True, slots=True)
class LifecycleTransition:
    command_name: str
    phase_before: str | None
    phase_after: str
    task_status_before: str | None
    task_status_after: str | None
    status_directive: str | None = None


def plan_session_started() -> LifecycleTransition:
    return LifecycleTransition(
        command_name="start",
        phase_before=None,
        phase_after="active",
        task_status_before=None,
        task_status_after=None,
    )


def plan_checkpoint_command(
    state: LifecycleState,
    *,
    command_name: str,
    status_directive: str | None = None,
) -> LifecycleTransition:
    _require_command_name(command_name, CHECKPOINT_COMMANDS)
    phase_before = _require_phase(
        command_name,
        state.phase,
        {
            "checkpoint": {"active", "paused", "ready_for_handoff"},
            "pause": {"active", "paused"},
            "prepare": {"active", "paused", "ready_for_handoff"},
        }[command_name],
    )
    normalized_task_status = _normalize_checkpoint_task_status(state.task_status)
    directive = normalize_checkpoint_status_directive(status_directive)
    if command_name != "checkpoint" and directive is not None:
        raise LifecycleViolation(f"{command_name} does not accept a status override")

    phase_after = phase_before
    task_status_after = normalized_task_status

    if command_name == "pause":
        phase_after = "paused"
    elif command_name == "prepare":
        phase_after = "ready_for_handoff"

    if directive == "active":
        phase_after = "active"
        task_status_after = "working"
    elif directive == "paused":
        phase_after = "paused"
    elif directive == "blocked":
        phase_after = "active"
        task_status_after = "blocked"
    elif directive == "done":
        phase_after = "active"
        task_status_after = "done"

    return LifecycleTransition(
        command_name=command_name,
        phase_before=phase_before,
        phase_after=phase_after,
        task_status_before=state.task_status,
        task_status_after=task_status_after,
        status_directive=directive,
    )


def plan_failover_command(state: LifecycleState) -> LifecycleTransition:
    phase_before = _require_phase("failover", state.phase, {"ready_for_handoff"})
    return LifecycleTransition(
        command_name="failover",
        phase_before=phase_before,
        phase_after="ready_for_handoff",
        task_status_before=state.task_status,
        task_status_after=state.task_status,
    )


def plan_launch_started(state: LifecycleState) -> LifecycleTransition:
    phase_before = _require_phase("launch", state.phase, {"ready_for_handoff"})
    return LifecycleTransition(
        command_name="launch",
        phase_before=phase_before,
        phase_after="launching",
        task_status_before=state.task_status,
        task_status_after=state.task_status,
    )


def plan_launch_finished(state: LifecycleState, *, launch_status: str) -> LifecycleTransition:
    if launch_status not in LAUNCH_RESULT_STATUSES:
        allowed = ", ".join(sorted(LAUNCH_RESULT_STATUSES))
        raise LifecycleViolation(f"launch_status must be one of: {allowed}")
    phase_before = _require_phase("launch.finish", state.phase, {"launching"})
    phase_after = "awaiting_resume" if launch_status == "succeeded" else "ready_for_handoff"
    return LifecycleTransition(
        command_name="launch.finish",
        phase_before=phase_before,
        phase_after=phase_after,
        task_status_before=state.task_status,
        task_status_after=state.task_status,
    )


def plan_resume_command(state: LifecycleState) -> LifecycleTransition:
    phase_before = _require_phase("resume", state.phase, {"ready_for_handoff", "awaiting_resume"})
    return LifecycleTransition(
        command_name="resume",
        phase_before=phase_before,
        phase_after="active",
        task_status_before=state.task_status,
        task_status_after=state.task_status,
    )


def plan_complete_command(state: LifecycleState) -> LifecycleTransition:
    phase_before = _require_phase("complete", state.phase, {"active", "paused"})
    return LifecycleTransition(
        command_name="complete",
        phase_before=phase_before,
        phase_after="completed",
        task_status_before=state.task_status,
        task_status_after=state.task_status,
    )


def plan_inspect_command(state: LifecycleState) -> LifecycleTransition:
    phase_before = _require_phase("inspect", state.phase, set(SESSION_PHASES))
    return LifecycleTransition(
        command_name="inspect",
        phase_before=phase_before,
        phase_after=phase_before,
        task_status_before=state.task_status,
        task_status_after=state.task_status,
    )


def plan_repair_command(state: LifecycleState) -> LifecycleTransition:
    phase_before = _require_phase("repair", state.phase, set(SESSION_PHASES))
    return LifecycleTransition(
        command_name="repair",
        phase_before=phase_before,
        phase_after=phase_before,
        task_status_before=state.task_status,
        task_status_after=state.task_status,
    )


def normalize_checkpoint_status_directive(status_directive: str | None) -> str | None:
    if status_directive is None:
        return None
    if not isinstance(status_directive, str):
        raise LifecycleViolation("checkpoint status override must be a string")
    if status_directive not in CHECKPOINT_STATUS_DIRECTIVES:
        raise LifecycleViolation(f"v2 checkpoint does not support --status {status_directive!r}")
    return status_directive


def _normalize_checkpoint_task_status(task_status: str | None) -> str:
    return task_status or "working"


def _require_command_name(command_name: str, allowed: set[str]) -> None:
    if command_name not in allowed:
        names = ", ".join(sorted(allowed))
        raise LifecycleViolation(f"unsupported lifecycle command {command_name!r}; expected one of: {names}")


def _require_phase(command_name: str, current_phase: str, allowed_phases: set[str]) -> str:
    if current_phase not in allowed_phases:
        allowed = ", ".join(sorted(allowed_phases))
        raise LifecycleViolation(
            f"{command_name} is not allowed while session phase is {current_phase}; allowed phases: {allowed}"
        )
    return current_phase
