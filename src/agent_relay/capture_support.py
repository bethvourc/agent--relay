from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from agent_relay.layout import STATE_DIRNAME

AUTOSAVE_GIT_TOUCHED_FILES_ENV = "AGENT_RELAY_AUTOSAVE_GIT_TOUCHED_FILES"
AUTOSAVE_RESEARCH_NOTE_FILE_ENV = "AGENT_RELAY_AUTOSAVE_RESEARCH_NOTE_FILE"
AUTOSAVE_IMPLEMENTATION_NOTE_FILE_ENV = "AGENT_RELAY_AUTOSAVE_IMPLEMENTATION_NOTE_FILE"
AUTOSAVE_VALIDATION_SUMMARY_FILE_ENV = "AGENT_RELAY_AUTOSAVE_VALIDATION_SUMMARY_FILE"

_TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}


@dataclass
class CaptureOptions:
    status: str | None = None
    snapshot_mode: str | None = None
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


def autosave_enabled(env_var: str) -> bool:
    return os.environ.get(env_var, "").strip().lower() in _TRUTHY_ENV_VALUES


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


def load_capture_text(
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
