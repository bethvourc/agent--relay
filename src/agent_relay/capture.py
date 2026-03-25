from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from agent_relay.checkpoints import create_checkpoint, utc_now
from agent_relay.models import CheckpointRecord, SessionState
from agent_relay.storage import STATE_DIRNAME, save_session
from agent_relay.summary import write_summary

AUTOSAVE_GIT_TOUCHED_FILES_ENV = "AGENT_RELAY_AUTOSAVE_GIT_TOUCHED_FILES"
AUTOSAVE_RESEARCH_NOTE_FILE_ENV = "AGENT_RELAY_AUTOSAVE_RESEARCH_NOTE_FILE"
AUTOSAVE_IMPLEMENTATION_NOTE_FILE_ENV = "AGENT_RELAY_AUTOSAVE_IMPLEMENTATION_NOTE_FILE"
AUTOSAVE_VALIDATION_SUMMARY_FILE_ENV = "AGENT_RELAY_AUTOSAVE_VALIDATION_SUMMARY_FILE"

_TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}


@dataclass
class CaptureOptions:
    status: str | None = None
    next_action: str | None = None
    decisions: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    touched_files: list[str] = field(default_factory=list)
    research_notes: list[str] = field(default_factory=list)
    implementation_notes: list[str] = field(default_factory=list)
    validation_status: str | None = None
    validation_summary: str | None = None
    research_note_file: str | None = None
    implementation_note_file: str | None = None
    validation_summary_file: str | None = None
    capture_git_changes: bool = False


def capture_session(
    repo_root: Path,
    session: SessionState,
    *,
    options: CaptureOptions,
    updated_at: str | None = None,
) -> CheckpointRecord:
    artifacts = apply_capture_options(repo_root, session, options=options)
    timestamp = updated_at or utc_now()
    session.updated_at = timestamp
    checkpoint = create_checkpoint(
        repo_root,
        session,
        created_at=timestamp,
        artifacts=artifacts,
    )
    save_session(repo_root, session)
    write_summary(repo_root, session, checkpoint)
    return checkpoint


def apply_capture_options(
    repo_root: Path,
    session: SessionState,
    *,
    options: CaptureOptions,
) -> dict[str, str | list[str]]:
    artifacts: dict[str, str | list[str]] = {}

    if options.status:
        session.current_status = options.status
    if options.next_action is not None:
        session.next_action = options.next_action
    if options.validation_status:
        session.validation.status = options.validation_status
    if options.validation_summary is not None:
        session.validation.summary = options.validation_summary

    _extend_unique(session.decisions, options.decisions)
    _extend_unique(session.blockers, options.blockers)
    _extend_unique(session.touched_files, options.touched_files)
    _extend_unique(session.research_notes, options.research_notes)
    _extend_unique(session.implementation_notes, options.implementation_notes)

    if options.capture_git_changes or _autosave_enabled(AUTOSAVE_GIT_TOUCHED_FILES_ENV):
        captured_touched_files = capture_git_touched_files(repo_root)
        _extend_unique(session.touched_files, captured_touched_files)
        if captured_touched_files:
            artifacts["touched_files_source"] = "git status --short --untracked-files=all"

    research_note, research_path = _load_capture_text(
        repo_root,
        explicit_path=options.research_note_file,
        env_var=AUTOSAVE_RESEARCH_NOTE_FILE_ENV,
    )
    if research_note:
        _append_unique(session.research_notes, research_note)
    if research_path is not None:
        artifacts["research_note_source"] = str(research_path)

    implementation_note, implementation_path = _load_capture_text(
        repo_root,
        explicit_path=options.implementation_note_file,
        env_var=AUTOSAVE_IMPLEMENTATION_NOTE_FILE_ENV,
    )
    if implementation_note:
        _append_unique(session.implementation_notes, implementation_note)
    if implementation_path is not None:
        artifacts["implementation_note_source"] = str(implementation_path)

    if options.validation_summary is None:
        validation_summary, validation_path = _load_capture_text(
            repo_root,
            explicit_path=options.validation_summary_file,
            env_var=AUTOSAVE_VALIDATION_SUMMARY_FILE_ENV,
        )
        if validation_summary is not None:
            session.validation.summary = validation_summary
        if validation_path is not None:
            artifacts["validation_summary_source"] = str(validation_path)

    return artifacts


def capture_git_touched_files(repo_root: Path) -> list[str]:
    if not (repo_root / ".git").exists():
        return []

    completed = subprocess.run(
        ["git", "-C", str(repo_root), "status", "--short", "--untracked-files=all"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return []

    touched_files: list[str] = []
    for raw_line in completed.stdout.splitlines():
        line = raw_line.rstrip()
        if not line:
            continue
        path_text = line[3:] if len(line) >= 4 else line
        if " -> " in path_text:
            path_text = path_text.split(" -> ", 1)[1]
        normalized = path_text.strip()
        if not normalized or normalized == STATE_DIRNAME or normalized.startswith(f"{STATE_DIRNAME}/"):
            continue
        _append_unique(touched_files, normalized)
    return touched_files


def _autosave_enabled(env_var: str) -> bool:
    value = os.environ.get(env_var, "")
    return value.strip().lower() in _TRUTHY_ENV_VALUES


def _load_capture_text(
    repo_root: Path,
    *,
    explicit_path: str | None,
    env_var: str,
) -> tuple[str | None, Path | None]:
    if explicit_path:
        return _read_capture_text(repo_root, explicit_path, required=True)

    default_path = os.environ.get(env_var)
    if not default_path:
        return None, None
    return _read_capture_text(repo_root, default_path, required=False)


def _read_capture_text(
    repo_root: Path,
    raw_path: str,
    *,
    required: bool,
) -> tuple[str | None, Path | None]:
    path = Path(raw_path)
    resolved = (repo_root / path).resolve() if not path.is_absolute() else path.resolve()

    if not resolved.exists():
        if required:
            raise SystemExit(f"Capture file not found: {raw_path}")
        return None, None
    if not resolved.is_file():
        if required:
            raise SystemExit(f"Capture file is not a regular file: {raw_path}")
        return None, None

    text = resolved.read_text(encoding="utf-8").strip()
    if not text:
        return None, resolved
    return text, resolved


def _append_unique(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def _extend_unique(items: list[str], values: list[str]) -> None:
    for value in values:
        _append_unique(items, value)
