from __future__ import annotations

import json
import secrets
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from agent_relay.v2.capture_support import (
    AUTOSAVE_GIT_TOUCHED_FILES_ENV,
    AUTOSAVE_IMPLEMENTATION_NOTE_FILE_ENV,
    AUTOSAVE_RESEARCH_NOTE_FILE_ENV,
    AUTOSAVE_VALIDATION_SUMMARY_FILE_ENV,
    CaptureOptions,
    autosave_enabled,
    capture_git_touched_files,
    load_capture_text,
)
from agent_relay.v2.hashing import sha256_bytes, sha256_text
from agent_relay.v2.integrity import require_session_mutable
from agent_relay.v2.lifecycle import (
    LifecycleState,
    LifecycleTransition,
    LifecycleViolation,
    plan_checkpoint_command,
)
from agent_relay.v2.models import CheckpointManifest, DerivedSessionView, ManifestFile, ValidationState
from agent_relay.v2.storage import load_session_view
from agent_relay.v2.tx import JournalCommitRequest, SessionTransaction


def checkpoint_id_now() -> str:
    return datetime.now(UTC).strftime("cp-%Y%m%dT%H%M%SZ-") + secrets.token_hex(3)


@dataclass(frozen=True, slots=True)
class CheckpointCommandResult:
    checkpoint_id: str
    phase: str
    next_action: str
    validation_status: str
    capture_mode: str


@dataclass(frozen=True, slots=True)
class WorkspaceCaptureResult:
    capture_mode: str
    files: tuple[ManifestFile, ...]
    file_contents: dict[str, str | bytes]
    repo_state_file: str
    validation_file: str
    summary_file: str
    git_head_file: str | None
    workspace_patch_file: str | None
    untracked_manifest_file: str | None
    snapshot_manifest_file: str | None


@dataclass(frozen=True, slots=True)
class CheckpointDraft:
    checkpoint_id: str
    created_at: str
    current_agent: str
    phase_after: str
    task_status: str
    next_action: str
    decisions: tuple[str, ...]
    blockers: tuple[str, ...]
    research_notes: tuple[str, ...]
    implementation_notes: tuple[str, ...]
    touched_files: tuple[str, ...]
    validation: ValidationState


def create_checkpoint_for_command(
    repo_root: Path,
    session_id: str,
    *,
    command_name: str,
    options: CaptureOptions,
    owner: str,
) -> CheckpointCommandResult:
    require_session_mutable(repo_root, session_id, command_name=command_name)
    view = load_session_view(repo_root, session_id)
    try:
        transition = plan_checkpoint_command(
            LifecycleState(phase=view.phase, task_status=view.task_status),
            command_name=command_name,
            status_directive=options.status,
        )
    except LifecycleViolation as exc:
        raise SystemExit(str(exc)) from exc
    draft = _build_checkpoint_draft(
        view,
        options=options,
        command_name=command_name,
        transition=transition,
    )
    capture = _capture_workspace(
        repo_root,
        view=view,
        draft=draft,
        command_name=command_name,
        snapshot_mode=options.snapshot_mode,
    )

    manifest = CheckpointManifest(
        schema_version=2,
        kind="checkpoint_manifest",
        object_id=draft.checkpoint_id,
        session_id=session_id,
        created_at=draft.created_at,
        current_agent=draft.current_agent,
        phase_hint=draft.phase_after,
        task_status=draft.task_status,
        capture_mode=capture.capture_mode,
        next_action=draft.next_action,
        decisions=draft.decisions,
        blockers=draft.blockers,
        research_notes=draft.research_notes,
        implementation_notes=draft.implementation_notes,
        touched_files=draft.touched_files,
        validation=draft.validation,
        repo_state_file=capture.repo_state_file,
        validation_file=capture.validation_file,
        summary_file=capture.summary_file,
        git_head_file=capture.git_head_file,
        workspace_patch_file=capture.workspace_patch_file,
        untracked_manifest_file=capture.untracked_manifest_file,
        snapshot_manifest_file=capture.snapshot_manifest_file,
        files=capture.files,
    )

    with SessionTransaction.begin(
        repo_root,
        session_id,
        operation=f"checkpoint:{command_name}",
        owner=owner,
    ) as tx:
        tx.stage_manifest_object(manifest, file_contents=capture.file_contents)
        tx.commit(
            JournalCommitRequest(
                event_type="checkpoint.recorded",
                phase_before=transition.phase_before,
                phase_after=transition.phase_after,
                payload={
                    "checkpoint_id": draft.checkpoint_id,
                    "command_name": command_name,
                    "capture_mode": capture.capture_mode,
                    "status_directive": transition.status_directive,
                },
                timestamp=draft.created_at,
            )
        )

    return CheckpointCommandResult(
        checkpoint_id=draft.checkpoint_id,
        phase=draft.phase_after,
        next_action=draft.next_action,
        validation_status=draft.validation.status,
        capture_mode=capture.capture_mode,
    )


def _build_checkpoint_draft(
    view: DerivedSessionView,
    *,
    options: CaptureOptions,
    command_name: str,
    transition: LifecycleTransition,
) -> CheckpointDraft:
    checkpoint_id = checkpoint_id_now()
    created_at = utc_now()
    phase_after = transition.phase_after
    task_status = transition.task_status_after or "working"

    next_action = view.next_action
    if options.next_action is not None:
        next_action = options.next_action
    next_action = next_action.strip()
    if command_name == "prepare" and not next_action:
        raise SystemExit("prepare requires a next action; pass --next-action or record one first")

    validation_status = options.validation_status or view.validation.status
    validation_summary = view.validation.summary
    if options.validation_summary is not None:
        validation_summary = options.validation_summary

    research_notes = list(view.research_notes)
    implementation_notes = list(view.implementation_notes)
    touched_files = list(view.touched_files)
    decisions = list(view.decisions)
    blockers = list(view.blockers)

    _extend_unique(decisions, options.decisions)
    _extend_unique(blockers, options.blockers)
    _extend_unique(touched_files, options.touched_files)
    _extend_unique(research_notes, options.research_notes)
    _extend_unique(implementation_notes, options.implementation_notes)

    research_note, _ = load_capture_text(
        Path(view.repo_root),
        explicit_path=options.research_note_file,
        env_var=AUTOSAVE_RESEARCH_NOTE_FILE_ENV,
    )
    if research_note:
        _append_unique(research_notes, research_note)

    implementation_note, _ = load_capture_text(
        Path(view.repo_root),
        explicit_path=options.implementation_note_file,
        env_var=AUTOSAVE_IMPLEMENTATION_NOTE_FILE_ENV,
    )
    if implementation_note:
        _append_unique(implementation_notes, implementation_note)

    if options.validation_summary is None:
        validation_text, _ = load_capture_text(
            Path(view.repo_root),
            explicit_path=options.validation_summary_file,
            env_var=AUTOSAVE_VALIDATION_SUMMARY_FILE_ENV,
        )
        if validation_text is not None:
            validation_summary = validation_text

    if options.capture_git_changes or autosave_enabled(AUTOSAVE_GIT_TOUCHED_FILES_ENV):
        _extend_unique(touched_files, capture_git_touched_files(Path(view.repo_root)))

    return CheckpointDraft(
        checkpoint_id=checkpoint_id,
        created_at=created_at,
        current_agent=view.current_agent,
        phase_after=phase_after,
        task_status=task_status,
        next_action=next_action,
        decisions=tuple(decisions),
        blockers=tuple(blockers),
        research_notes=tuple(research_notes),
        implementation_notes=tuple(implementation_notes),
        touched_files=tuple(touched_files),
        validation=ValidationState(status=validation_status, summary=validation_summary),
    )
def _capture_workspace(
    repo_root: Path,
    *,
    view: DerivedSessionView,
    draft: CheckpointDraft,
    command_name: str,
    snapshot_mode: str | None,
) -> WorkspaceCaptureResult:
    git_repo = _detect_git_repo(repo_root)
    if snapshot_mode == "full":
        return _capture_snapshot_workspace(repo_root, view=view, draft=draft, command_name=command_name)
    if snapshot_mode is not None:
        raise SystemExit(f"Unsupported snapshot mode: {snapshot_mode}")
    if git_repo is None:
        raise SystemExit("v2 checkpoints require a Git-backed repo or --snapshot-mode full")
    head = _git(repo_root, "rev-parse", "--verify", "HEAD", check=False)
    if head.returncode != 0:
        raise SystemExit("v2 Git-backed checkpoints require at least one commit or --snapshot-mode full")
    return _capture_git_workspace(
        repo_root,
        view=view,
        draft=draft,
        command_name=command_name,
        head_sha=head.stdout.strip(),
    )


def _capture_git_workspace(
    repo_root: Path,
    *,
    view: DerivedSessionView,
    draft: CheckpointDraft,
    command_name: str,
    head_sha: str,
) -> WorkspaceCaptureResult:
    branch = _git(repo_root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    branch_text = branch.stdout.strip() if branch.returncode == 0 else "(detached)"
    patch = _git(
        repo_root,
        "diff",
        "--binary",
        "--full-index",
        "--no-ext-diff",
        "HEAD",
        "--",
        ".",
        ":(exclude).agent-relay/**",
    )
    untracked_paths = _git_untracked_files(repo_root)
    file_contents: dict[str, str | bytes] = {}

    repo_state = _repo_state_payload(
        view=view,
        draft=draft,
        command_name=command_name,
        capture_mode="git",
    )
    validation_payload = _validation_payload(session_id=view.session_id, draft=draft)
    summary_text = _render_checkpoint_summary(view=view, draft=draft, capture_mode="git")
    git_head_text = f"{head_sha}\nbranch={branch_text}\n"
    untracked_entries: list[dict[str, Any]] = []

    file_contents["repo-state.json"] = _json_text(repo_state)
    file_contents["validation.json"] = _json_text(validation_payload)
    file_contents["summary.md"] = summary_text
    file_contents["git-head.txt"] = git_head_text
    file_contents["workspace.patch"] = patch.stdout

    for path in untracked_paths:
        source = repo_root / path
        if source.is_symlink() or not source.is_file():
            raise SystemExit(f"Cannot safely capture untracked non-regular file in Git mode: {path}")
        stored_as = f"untracked/{path.as_posix()}"
        content = source.read_bytes()
        file_contents[stored_as] = content
        untracked_entries.append(
            {
                "path": path.as_posix(),
                "stored_as": stored_as,
                "sha256": sha256_bytes(content),
                "size_bytes": len(content),
            }
        )

    file_contents["untracked-manifest.json"] = _json_text({"files": untracked_entries})
    manifest_files = _manifest_files_from_contents(file_contents)
    return WorkspaceCaptureResult(
        capture_mode="git",
        files=manifest_files,
        file_contents=file_contents,
        repo_state_file="repo-state.json",
        validation_file="validation.json",
        summary_file="summary.md",
        git_head_file="git-head.txt",
        workspace_patch_file="workspace.patch",
        untracked_manifest_file="untracked-manifest.json",
        snapshot_manifest_file=None,
    )


def _capture_snapshot_workspace(
    repo_root: Path,
    *,
    view: DerivedSessionView,
    draft: CheckpointDraft,
    command_name: str,
) -> WorkspaceCaptureResult:
    file_contents: dict[str, str | bytes] = {}
    repo_state = _repo_state_payload(
        view=view,
        draft=draft,
        command_name=command_name,
        capture_mode="snapshot",
    )
    validation_payload = _validation_payload(session_id=view.session_id, draft=draft)
    summary_text = _render_checkpoint_summary(view=view, draft=draft, capture_mode="snapshot")

    file_contents["repo-state.json"] = _json_text(repo_state)
    file_contents["validation.json"] = _json_text(validation_payload)
    file_contents["summary.md"] = summary_text

    snapshot_entries: list[dict[str, Any]] = []
    for path in sorted(_iter_snapshot_paths(repo_root)):
        if path.is_symlink() or not path.is_file():
            raise SystemExit(f"Snapshot mode only supports regular files: {path.relative_to(repo_root)}")
        relative = path.relative_to(repo_root).as_posix()
        stored_as = f"snapshot/{relative}"
        content = path.read_bytes()
        file_contents[stored_as] = content
        snapshot_entries.append(
            {
                "path": relative,
                "stored_as": stored_as,
                "sha256": sha256_bytes(content),
                "size_bytes": len(content),
            }
        )

    file_contents["snapshot-manifest.json"] = _json_text({"files": snapshot_entries})
    manifest_files = _manifest_files_from_contents(file_contents)
    return WorkspaceCaptureResult(
        capture_mode="snapshot",
        files=manifest_files,
        file_contents=file_contents,
        repo_state_file="repo-state.json",
        validation_file="validation.json",
        summary_file="summary.md",
        git_head_file=None,
        workspace_patch_file=None,
        untracked_manifest_file=None,
        snapshot_manifest_file="snapshot-manifest.json",
    )


def _repo_state_payload(
    *,
    view: DerivedSessionView,
    draft: CheckpointDraft,
    command_name: str,
    capture_mode: str,
) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "kind": "checkpoint_repo_state",
        "session_id": view.session_id,
        "checkpoint_id": draft.checkpoint_id,
        "captured_at": draft.created_at,
        "command_name": command_name,
        "capture_mode": capture_mode,
        "repo_root": view.repo_root,
        "objective": view.objective,
        "workstream_kind": view.workstream_kind,
        "current_agent": draft.current_agent,
        "phase": draft.phase_after,
        "task_status": draft.task_status,
        "next_action": draft.next_action,
        "decisions": list(draft.decisions),
        "blockers": list(draft.blockers),
        "research_notes": list(draft.research_notes),
        "implementation_notes": list(draft.implementation_notes),
        "touched_files": list(draft.touched_files),
    }


def _validation_payload(*, session_id: str, draft: CheckpointDraft) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "kind": "checkpoint_validation",
        "session_id": session_id,
        "checkpoint_id": draft.checkpoint_id,
        "captured_at": draft.created_at,
        "status": draft.validation.status,
        "summary": draft.validation.summary,
    }


def _render_checkpoint_summary(
    *,
    view: DerivedSessionView,
    draft: CheckpointDraft,
    capture_mode: str,
) -> str:
    lines = [
        "# Agent Relay Checkpoint",
        "",
        f"Checkpoint ID: {draft.checkpoint_id}",
        f"Session ID: {view.session_id}",
        f"Objective: {view.objective}",
        f"Current agent: {draft.current_agent}",
        f"Phase: {draft.phase_after}",
        f"Task status: {draft.task_status}",
        f"Capture mode: {capture_mode}",
        f"Next action: {draft.next_action or 'Not recorded'}",
        f"Validation: {draft.validation.status} - {draft.validation.summary or 'None recorded'}",
        "",
    ]
    _append_bullets(lines, "Decisions:", draft.decisions)
    _append_bullets(lines, "Blockers:", draft.blockers)
    _append_bullets(lines, "Research notes:", draft.research_notes)
    _append_bullets(lines, "Implementation notes:", draft.implementation_notes)
    _append_bullets(lines, "Touched files:", draft.touched_files)
    return "\n".join(lines) + "\n"


def _manifest_files_from_contents(file_contents: dict[str, str | bytes]) -> tuple[ManifestFile, ...]:
    entries: list[ManifestFile] = []
    for relative_path in sorted(file_contents):
        content = file_contents[relative_path]
        if isinstance(content, bytes):
            payload = content
        else:
            payload = content.encode("utf-8")
        entries.append(
            ManifestFile(
                relative_path=relative_path,
                sha256=sha256_bytes(payload),
                size_bytes=len(payload),
            )
        )
    return tuple(entries)


def _append_bullets(lines: list[str], heading: str, items: Iterable[str]) -> None:
    lines.append(heading)
    values = list(items)
    if values:
        lines.extend([f"- {item}" for item in values])
    else:
        lines.append("- None recorded")
    lines.append("")


def _git(repo_root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if check and completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip() or "git command failed"
        raise SystemExit(stderr)
    return completed


def _git_untracked_files(repo_root: Path) -> list[Path]:
    completed = _git(repo_root, "status", "--porcelain", "--untracked-files=all")
    paths: list[Path] = []
    for raw_line in completed.stdout.splitlines():
        line = raw_line.rstrip()
        if not line.startswith("?? "):
            continue
        relative = line[3:].strip()
        if not relative or relative == ".agent-relay" or relative.startswith(".agent-relay/"):
            continue
        paths.append(Path(relative))
    return paths


def _iter_snapshot_paths(repo_root: Path) -> list[Path]:
    captured: list[Path] = []
    for path in repo_root.rglob("*"):
        if any(part in {".git", ".agent-relay"} for part in path.parts):
            continue
        if path.is_dir():
            continue
        captured.append(path)
    return captured


def _detect_git_repo(repo_root: Path) -> Path | None:
    completed = _git(repo_root, "rev-parse", "--show-toplevel", check=False)
    if completed.returncode != 0:
        return None
    top = Path(completed.stdout.strip()).resolve()
    return top if top == repo_root.resolve() else None


def _json_text(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def _append_unique(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def _extend_unique(items: list[str], values: Iterable[str]) -> None:
    for value in values:
        _append_unique(items, value)
def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
