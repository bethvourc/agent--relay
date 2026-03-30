"""Concurrent agent execution orchestrator (tmux-backed).

Runs multiple agents simultaneously in separate tmux sessions, giving them live
visibility into each other's work through relay-managed snapshot files and a
shared workspace log.
"""
from __future__ import annotations

import json
import os
import secrets
import shlex
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from agent_relay.agents import (
    get_agent_adapter,
    get_agent_display_name,
    require_available,
)
from agent_relay.bootstrap import start_session
from agent_relay.fs import write_json_atomic, write_text_atomic
from agent_relay.hashing import sha256_path
from agent_relay.layout import (
    concurrent_agent_dir,
    concurrent_dir,
    workspace_log_path,
)
from agent_relay.storage import is_session
from agent_relay.workspace_log import LogEntry, WorkspaceLog, utc_timestamp


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ClaimSpec:
    path: str
    role: str = "owner"


@dataclass(frozen=True, slots=True)
class AgentOutcome:
    slot: int
    agent_key: str
    tmux_session: str
    phase: str
    exit_code: int | None   # None if still running / killed
    raw_stdout: str
    raw_stderr: str
    text: str
    summary: str
    done_signal: bool
    started_at: str
    finished_at: str
    worktree_path: str | None = None
    control_status: str = "continue"
    control_reason: str = ""
    claims: tuple[str, ...] = field(default_factory=tuple)
    claim_specs: tuple[ClaimSpec, ...] = field(default_factory=tuple)
    changed_paths: tuple[str, ...] = field(default_factory=tuple)
    merged_paths: tuple[str, ...] = field(default_factory=tuple)
    merge_conflicts: tuple[str, ...] = field(default_factory=tuple)
    scope_violations: tuple[str, ...] = field(default_factory=tuple)
    remaining_work: tuple[str, ...] = field(default_factory=tuple)
    verification: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class ConcurrentResult:
    session_id: str
    agents: tuple[str, ...]
    tmux_sessions: tuple[str, ...]
    continued_from_session_id: str | None
    claim_ledger_path: str | None
    stop_reason: str   # "all_done" | "incomplete" | "max_time" | "agent_error" | "interrupted" | "scope_violation" | "merge_conflict"
    elapsed_seconds: float
    outcomes: tuple[AgentOutcome, ...]


@dataclass(frozen=True, slots=True)
class ConcurrentControl:
    status: str = "continue"
    reason: str = ""
    claims: tuple[str, ...] = field(default_factory=tuple)
    claim_specs: tuple[ClaimSpec, ...] = field(default_factory=tuple)
    remaining_work: tuple[str, ...] = field(default_factory=tuple)
    verification: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class PhaseRunResult:
    phase: str
    stop_reason: str  # "completed" | "max_time" | "interrupted"
    tmux_sessions: tuple[str, ...]
    outcomes: tuple[AgentOutcome, ...]


# ---------------------------------------------------------------------------
# tmux helpers
# ---------------------------------------------------------------------------

_DONE_MARKER = "CONVERSATION_COMPLETE"
_STATUS_PREFIX = "RELAY_STATUS:"
_VALID_CONTROL_STATUSES = frozenset({
    "continue",
    "blocked",
    "done",
    "error",
    "planning",
})
_VALID_CLAIM_ROLES = frozenset({"owner", "reviewer", "shared"})

def _require_tmux() -> str:
    """Return tmux path or raise SystemExit."""
    path = shutil.which("tmux")
    if not path:
        raise SystemExit(
            "tmux is required for concurrent mode (race).\n"
            "Install it: brew install tmux (macOS) or apt install tmux (Linux)"
        )
    return path


def _tmux(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", *args],
        capture_output=True, text=True, check=check,
    )


def _tmux_session_exists(session_name: str) -> bool:
    result = _tmux("has-session", "-t", session_name, check=False)
    return result.returncode == 0


def _tmux_capture_pane(session_name: str, pane_index: int) -> str:
    """Capture the visible content of a tmux pane."""
    result = _tmux(
        "capture-pane", "-t", f"{session_name}:{0}.{pane_index}",
        "-p",  # print to stdout
        check=False,
    )
    return result.stdout if result.returncode == 0 else ""


def _tmux_pane_pid(session_name: str, pane_index: int) -> int | None:
    """Get the PID of the process running in a pane."""
    result = _tmux(
        "display-message", "-t", f"{session_name}:{0}.{pane_index}",
        "-p", "#{pane_pid}",
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip().isdigit():
        return int(result.stdout.strip())
    return None


def _tmux_pane_dead(session_name: str, pane_index: int) -> bool:
    """Check if the pane's process has exited."""
    result = _tmux(
        "display-message", "-t", f"{session_name}:{0}.{pane_index}",
        "-p", "#{pane_dead}",
        check=False,
    )
    return result.stdout.strip() == "1"


def _tmux_session_name(session_id: str, slot: int, *, phase: str) -> str:
    if phase == "implementation":
        return f"relay-{session_id}-{slot:02d}"
    return f"relay-{session_id}-{phase}-{slot:02d}"


def _claims_ledger_path(repo_root: Path, session_id: str) -> Path:
    return concurrent_dir(repo_root, session_id) / "claims.json"


def _baseline_snapshot_dir(repo_root: Path, session_id: str) -> Path:
    return concurrent_dir(repo_root, session_id) / "baseline"


def _worktree_root(repo_root: Path, session_id: str) -> Path:
    return Path(tempfile.gettempdir()) / "agent-relay-worktrees" / repo_root.name / session_id


def _agent_worktree_path(repo_root: Path, session_id: str, slot: int) -> Path:
    return _worktree_root(repo_root, session_id) / f"agent-{slot:02d}"


def _worktree_coordination_dir(worktree_path: Path) -> Path:
    return worktree_path / ".agent-relay" / "concurrent"


def _worktree_snapshot_paths(worktree_path: Path, slot_count: int) -> tuple[Path, ...]:
    cdir = _worktree_coordination_dir(worktree_path)
    return tuple(cdir / f"slot-{slot:02d}.txt" for slot in range(slot_count))


def _worktree_workspace_log_path(worktree_path: Path) -> Path:
    return _worktree_coordination_dir(worktree_path) / "workspace-log.md"


def _worktree_claim_ledger_path(worktree_path: Path) -> Path:
    return _worktree_coordination_dir(worktree_path) / "claims.json"


def _worktree_continued_workspace_log_path(worktree_path: Path) -> Path:
    return _worktree_coordination_dir(worktree_path) / "continued-workspace-log.md"


def _git(repo_root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=check,
    )


def _git_ls_files(repo_root: Path, *args: str) -> tuple[str, ...]:
    result = _git(repo_root, "ls-files", "-z", *args)
    entries = [entry for entry in result.stdout.split("\0") if entry]
    return tuple(sorted(entry for entry in entries if not entry.startswith(".agent-relay/")))


def _current_repo_file_paths(repo_root: Path) -> tuple[str, ...]:
    tracked = {
        relative_path
        for relative_path in _git_ls_files(repo_root)
        if (repo_root / relative_path).exists() or (repo_root / relative_path).is_symlink()
    }
    untracked = set(_git_ls_files(repo_root, "--others", "--exclude-standard"))
    return tuple(sorted(tracked | untracked))


def _path_hash_or_none(path: Path) -> str | None:
    if not path.exists():
        return None
    return sha256_path(path)


def _is_runtime_metadata_path(relative_path: str) -> bool:
    return relative_path == ".agent-relay" or relative_path.startswith(".agent-relay/") or relative_path.startswith(".git/")


def _scan_runtime_manifest(root: Path) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for current_root, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in {".git", ".agent-relay"}]
        current_dir = Path(current_root)
        for filename in filenames:
            path = current_dir / filename
            relative = path.relative_to(root).as_posix()
            if _is_runtime_metadata_path(relative):
                continue
            manifest[relative] = sha256_path(path)
    return dict(sorted(manifest.items()))


def _copy_file_like(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        if destination.is_dir() and not destination.is_symlink():
            shutil.rmtree(destination)
        else:
            destination.unlink()
    if source.is_symlink():
        destination.symlink_to(os.readlink(source))
        return
    shutil.copy2(source, destination)


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def _normalize_claim_path(raw_path: str) -> str:
    text = raw_path.strip()
    if not text:
        return ""
    if text.startswith("./"):
        text = text[2:]
    if text == ".":
        return ""
    if text.endswith("/"):
        stripped = text.rstrip("/")
        return f"{stripped}/" if stripped else ""
    return text.rstrip("/")


def _normalize_claim_spec(path: str, role: str) -> ClaimSpec | None:
    normalized_path = _normalize_claim_path(path)
    normalized_role = role.strip().lower()
    if not normalized_path or normalized_role not in _VALID_CLAIM_ROLES:
        return None
    return ClaimSpec(path=normalized_path, role=normalized_role)


def _parse_claim_specs(value: object) -> tuple[ClaimSpec, ...]:
    items: list[ClaimSpec] = []
    raw_items: list[object]
    if isinstance(value, (list, tuple)):
        raw_items = list(value)
    elif value is None:
        raw_items = []
    else:
        raw_items = [value]

    for item in raw_items:
        if isinstance(item, str):
            spec = _normalize_claim_spec(item, "owner")
        elif isinstance(item, dict):
            path = item.get("path", item.get("claim", ""))
            role = item.get("role", "owner")
            spec = _normalize_claim_spec(str(path), str(role))
        else:
            spec = None
        if spec is not None:
            items.append(spec)

    deduped: dict[str, ClaimSpec] = {}
    for spec in items:
        deduped[spec.path.casefold()] = spec
    return tuple(sorted(deduped.values(), key=lambda spec: spec.path.casefold()))


def _coerce_string_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        stripped = value.strip()
        return (stripped,) if stripped else ()
    if isinstance(value, (list, tuple)):
        items: list[str] = []
        for item in value:
            if isinstance(item, str):
                stripped = item.strip()
                if stripped:
                    items.append(stripped)
        return tuple(items)
    return ()


def _has_legacy_done_line(text: str) -> bool:
    return any(line.strip().upper() == _DONE_MARKER for line in text.splitlines())


def _strip_concurrent_control(text: str) -> str:
    kept_lines = [
        line
        for line in text.splitlines()
        if not line.strip().startswith(_STATUS_PREFIX)
        and line.strip().upper() != _DONE_MARKER
    ]
    return "\n".join(kept_lines).strip()


def _claim_paths(claim_specs: Sequence[ClaimSpec]) -> tuple[str, ...]:
    return tuple(spec.path for spec in claim_specs)


def parse_concurrent_control(text: str) -> ConcurrentControl:
    """Parse the last machine-readable RELAY_STATUS line from pane content."""
    for raw_line in reversed(text.splitlines()):
        line = raw_line.strip()
        if not line.startswith(_STATUS_PREFIX):
            continue

        payload = line[len(_STATUS_PREFIX):].strip()
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue

        status = str(data.get("status", "continue")).strip().lower()
        if status not in _VALID_CONTROL_STATUSES:
            continue

        claim_specs = _parse_claim_specs(data.get("claims"))
        return ConcurrentControl(
            status=status,
            reason=str(data.get("reason", "")).strip(),
            claims=_claim_paths(claim_specs),
            claim_specs=claim_specs,
            remaining_work=_coerce_string_tuple(data.get("remaining_work")),
            verification=_coerce_string_tuple(data.get("verification")),
        )

    if _has_legacy_done_line(text):
        return ConcurrentControl(
            status="done",
            reason="Legacy CONVERSATION_COMPLETE marker",
        )

    return ConcurrentControl()


def _make_summary(text: str, *, exit_code: int | None) -> str:
    for line in _strip_concurrent_control(text).splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:117] + "..." if len(stripped) > 120 else stripped
    if exit_code not in (None, 0):
        return f"(exited with code {exit_code})"
    if exit_code is None:
        return "(still running)"
    return "(no output)"


def _read_exit_code(path: Path) -> int | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError:
        return None
    return int(text) if text and text.lstrip("-").isdigit() else None


def _build_outcome(
    *,
    slot: int,
    agent_key: str,
    tmux_session: str,
    phase: str,
    worktree_path: Path | None,
    pane_content: str,
    exit_code: int | None,
    started_at: str,
    finished_at: str,
) -> AgentOutcome:
    control = parse_concurrent_control(pane_content)
    display_text = _strip_concurrent_control(pane_content)
    return AgentOutcome(
        slot=slot,
        agent_key=agent_key,
        tmux_session=tmux_session,
        phase=phase,
        worktree_path=str(worktree_path) if worktree_path is not None else None,
        exit_code=exit_code,
        raw_stdout=pane_content,
        raw_stderr="",
        text=display_text,
        summary=_make_summary(pane_content, exit_code=exit_code),
        done_signal=control.status == "done",
        started_at=started_at,
        finished_at=finished_at,
        control_status=control.status,
        control_reason=control.reason,
        claims=control.claims,
        claim_specs=control.claim_specs,
        remaining_work=control.remaining_work,
        verification=control.verification,
    )


def _classify_stop_reason(
    current_stop_reason: str,
    outcomes: Sequence[AgentOutcome],
) -> str:
    if current_stop_reason in {"max_time", "interrupted", "agent_error"}:
        return current_stop_reason
    if any(outcome.exit_code is None for outcome in outcomes):
        return "agent_error"
    if any(
        outcome.exit_code != 0 or outcome.control_status == "error"
        for outcome in outcomes
    ):
        return "agent_error"
    if all(
        outcome.exit_code == 0 and outcome.control_status == "done"
        for outcome in outcomes
    ):
        return "all_done"
    return "incomplete"


def _claim_targets_overlap(left: ClaimSpec, right: ClaimSpec) -> bool:
    left_path = left.path.rstrip("/")
    right_path = right.path.rstrip("/")
    left_is_dir = left.path.endswith("/")
    right_is_dir = right.path.endswith("/")
    if left_path == right_path:
        return True
    if left_is_dir and (right_path.startswith(left_path + "/")):
        return True
    if right_is_dir and (left_path.startswith(right_path + "/")):
        return True
    return False


def _claim_specs_can_coexist(left: ClaimSpec, right: ClaimSpec) -> bool:
    if not _claim_targets_overlap(left, right):
        return True
    if "reviewer" in {left.role, right.role}:
        return True
    if left.role == right.role == "shared":
        return True
    return False


def _find_claim_conflicts(outcomes: Sequence[AgentOutcome]) -> list[dict[str, object]]:
    conflicts: list[dict[str, object]] = []
    for index, left_outcome in enumerate(outcomes):
        for right_outcome in outcomes[index + 1:]:
            for left_claim in left_outcome.claim_specs:
                for right_claim in right_outcome.claim_specs:
                    if _claim_specs_can_coexist(left_claim, right_claim):
                        continue
                    conflicts.append({
                        "left_slot": left_outcome.slot,
                        "left_agent": left_outcome.agent_key,
                        "left_claim": left_claim.path,
                        "left_role": left_claim.role,
                        "right_slot": right_outcome.slot,
                        "right_agent": right_outcome.agent_key,
                        "right_claim": right_claim.path,
                        "right_role": right_claim.role,
                    })
    return conflicts


def _write_claim_ledger(
    path: Path,
    *,
    session_id: str,
    continued_from_session_id: str | None,
    outcomes: Sequence[AgentOutcome],
    status: str,
    conflicts: Sequence[dict[str, object]] = (),
) -> None:
    payload = {
        "session_id": session_id,
        "continued_from_session_id": continued_from_session_id,
        "status": status,
        "generated_at": utc_timestamp(),
        "claims": [
            {
                "slot": outcome.slot,
                "agent": outcome.agent_key,
                "claims": list(outcome.claims),
                "claim_specs": [
                    {"path": spec.path, "role": spec.role}
                    for spec in outcome.claim_specs
                ],
                "reason": outcome.control_reason,
                "status": outcome.control_status,
            }
            for outcome in outcomes
        ],
        "conflicts": list(conflicts),
    }
    write_json_atomic(path, payload)


def _classify_planning_result(
    session_id: str,
    continued_from_session_id: str | None,
    claim_ledger_path: Path,
    outcomes: Sequence[AgentOutcome],
) -> tuple[str, dict[int, tuple[ClaimSpec, ...]]]:
    accepted_claims = {outcome.slot: outcome.claim_specs for outcome in outcomes}
    if any(outcome.exit_code is None for outcome in outcomes):
        _write_claim_ledger(
            claim_ledger_path,
            session_id=session_id,
            continued_from_session_id=continued_from_session_id,
            outcomes=outcomes,
            status="planning_incomplete",
        )
        return "planning_incomplete", accepted_claims
    if any(
        outcome.exit_code != 0 or outcome.control_status in {"blocked", "error"}
        for outcome in outcomes
    ):
        _write_claim_ledger(
            claim_ledger_path,
            session_id=session_id,
            continued_from_session_id=continued_from_session_id,
            outcomes=outcomes,
            status="planning_incomplete",
        )
        return "planning_incomplete", accepted_claims
    if any(
        outcome.control_status != "planning" or not outcome.claim_specs
        for outcome in outcomes
    ):
        _write_claim_ledger(
            claim_ledger_path,
            session_id=session_id,
            continued_from_session_id=continued_from_session_id,
            outcomes=outcomes,
            status="planning_incomplete",
        )
        return "planning_incomplete", accepted_claims

    conflicts = _find_claim_conflicts(outcomes)
    if conflicts:
        _write_claim_ledger(
            claim_ledger_path,
            session_id=session_id,
            continued_from_session_id=continued_from_session_id,
            outcomes=outcomes,
            status="claim_conflict",
            conflicts=conflicts,
        )
        return "claim_conflict", accepted_claims

    _write_claim_ledger(
        claim_ledger_path,
        session_id=session_id,
        continued_from_session_id=continued_from_session_id,
        outcomes=outcomes,
        status="accepted",
    )
    return "accepted", accepted_claims


def _create_agent_worktree(
    repo_root: Path,
    *,
    session_id: str,
    slot: int,
    baseline_paths: Sequence[str],
) -> Path:
    worktree_path = _agent_worktree_path(repo_root, session_id, slot)
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    if worktree_path.exists():
        shutil.rmtree(worktree_path)
    result = _git(
        repo_root,
        "worktree",
        "add",
        "--detach",
        "--force",
        str(worktree_path),
        "HEAD",
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "git worktree add failed"
        raise SystemExit(f"Unable to create isolated worktree: {message}")

    baseline_set = set(baseline_paths)
    for tracked_path in _git_ls_files(worktree_path):
        if tracked_path not in baseline_set:
            _remove_path(worktree_path / tracked_path)
    for relative_path in baseline_paths:
        source = repo_root / relative_path
        if source.exists() or source.is_symlink():
            _copy_file_like(source, worktree_path / relative_path)
    return worktree_path


def _build_baseline_manifest(repo_root: Path, relative_paths: Sequence[str]) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for relative_path in relative_paths:
        source = repo_root / relative_path
        if source.exists() or source.is_symlink():
            manifest[relative_path] = sha256_path(source)
    return dict(sorted(manifest.items()))


def _create_baseline_snapshot(
    repo_root: Path,
    *,
    session_id: str,
    relative_paths: Sequence[str],
) -> Path:
    snapshot_dir = _baseline_snapshot_dir(repo_root, session_id)
    if snapshot_dir.exists():
        shutil.rmtree(snapshot_dir)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    for relative_path in relative_paths:
        source = repo_root / relative_path
        if source.exists() or source.is_symlink():
            _copy_file_like(source, snapshot_dir / relative_path)
    return snapshot_dir


def _prune_stale_worktrees(repo_root: Path, *, max_age_seconds: int = 7 * 24 * 60 * 60) -> None:
    root = _worktree_root(repo_root, "stale-scan").parent
    if not root.exists():
        return
    cutoff = time.time() - max_age_seconds
    for candidate in root.iterdir():
        try:
            if candidate.stat().st_mtime < cutoff:
                shutil.rmtree(candidate, ignore_errors=True)
        except FileNotFoundError:
            continue


def _cleanup_worktrees(repo_root: Path, worktree_paths: Sequence[Path]) -> None:
    for worktree_path in worktree_paths:
        _git(repo_root, "worktree", "remove", "--force", str(worktree_path), check=False)
        shutil.rmtree(worktree_path, ignore_errors=True)


def _read_text_if_possible(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, UnicodeDecodeError, OSError):
        return None


def _merge_shared_text_change(destination: Path, baseline_path: Path, source: Path) -> bool:
    current_text = _read_text_if_possible(destination)
    baseline_text = _read_text_if_possible(baseline_path)
    source_text = _read_text_if_possible(source)
    if current_text is None or baseline_text is None or source_text is None:
        return False
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_root = Path(tmpdir)
        current_path = tmp_root / "current.txt"
        base_path = tmp_root / "base.txt"
        other_path = tmp_root / "other.txt"
        current_path.write_text(current_text, encoding="utf-8")
        base_path.write_text(baseline_text, encoding="utf-8")
        other_path.write_text(source_text, encoding="utf-8")
        result = subprocess.run(
            ["git", "merge-file", "-p", str(current_path), str(base_path), str(other_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return False
        write_text_atomic(destination, result.stdout)
    return True


def _sync_worktree_coordination_files(
    *,
    worktree_paths: Sequence[Path],
    pane_snapshot_paths: Sequence[Path],
    workspace_log_path: Path,
    claim_ledger_path: Path | None = None,
    continued_workspace_log_path: Path | None = None,
) -> None:
    snapshot_texts = [
        snapshot_path.read_text(encoding="utf-8") if snapshot_path.exists() else ""
        for snapshot_path in pane_snapshot_paths
    ]
    workspace_log_text = workspace_log_path.read_text(encoding="utf-8") if workspace_log_path.exists() else ""
    claim_ledger_text = (
        claim_ledger_path.read_text(encoding="utf-8")
        if claim_ledger_path is not None and claim_ledger_path.exists()
        else None
    )
    continued_workspace_log_text = (
        continued_workspace_log_path.read_text(encoding="utf-8")
        if continued_workspace_log_path is not None and continued_workspace_log_path.exists()
        else None
    )
    for worktree_path in worktree_paths:
        coordination_dir = _worktree_coordination_dir(worktree_path)
        coordination_dir.mkdir(parents=True, exist_ok=True)
        for slot, snapshot_text in enumerate(snapshot_texts):
            write_text_atomic(coordination_dir / f"slot-{slot:02d}.txt", snapshot_text)
        write_text_atomic(_worktree_workspace_log_path(worktree_path), workspace_log_text)
        if claim_ledger_text is not None:
            write_text_atomic(_worktree_claim_ledger_path(worktree_path), claim_ledger_text)
        if continued_workspace_log_text is not None:
            write_text_atomic(_worktree_continued_workspace_log_path(worktree_path), continued_workspace_log_text)


def _path_matches_claim(relative_path: str, claim: str, repo_root: Path) -> bool:
    normalized_path = relative_path.strip("/")
    normalized_claim = claim.strip("/")
    if not normalized_path or not normalized_claim:
        return False
    if claim.endswith("/"):
        return normalized_path.startswith(normalized_claim + "/") or normalized_path == normalized_claim
    claim_path = repo_root / normalized_claim
    if claim_path.is_dir():
        return normalized_path.startswith(normalized_claim + "/") or normalized_path == normalized_claim
    return normalized_path == normalized_claim


def _path_matches_claim_spec(relative_path: str, claim_spec: ClaimSpec, repo_root: Path) -> bool:
    return _path_matches_claim(relative_path, claim_spec.path, repo_root)


def _editable_claim_specs(claim_specs: Sequence[ClaimSpec]) -> tuple[ClaimSpec, ...]:
    return tuple(spec for spec in claim_specs if spec.role in {"owner", "shared"})


def _shared_collaboration_enabled(
    relative_path: str,
    accepted_claims_by_slot: dict[int, tuple[ClaimSpec, ...]],
    repo_root: Path,
) -> bool:
    shared_slots = [
        slot
        for slot, claim_specs in accepted_claims_by_slot.items()
        if any(
            spec.role == "shared" and _path_matches_claim_spec(relative_path, spec, repo_root)
            for spec in claim_specs
        )
    ]
    return len(shared_slots) >= 2


def _changed_paths_from_manifest(
    worktree_path: Path,
    baseline_manifest: dict[str, str],
) -> tuple[dict[str, str], tuple[str, ...]]:
    current_manifest = _scan_runtime_manifest(worktree_path)
    changed_paths = tuple(sorted(
        path
        for path in set(baseline_manifest) | set(current_manifest)
        if baseline_manifest.get(path) != current_manifest.get(path)
    ))
    return current_manifest, changed_paths


def _merge_worktree_changes(
    repo_root: Path,
    *,
    worktree_path: Path,
    baseline_root: Path,
    baseline_manifest: dict[str, str],
    changed_paths: Sequence[str],
    accepted_claims_by_slot: dict[int, tuple[ClaimSpec, ...]],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    merged_paths: list[str] = []
    merge_conflicts: list[str] = []
    for relative_path in changed_paths:
        destination = repo_root / relative_path
        source = worktree_path / relative_path
        baseline_path = baseline_root / relative_path
        baseline_hash = baseline_manifest.get(relative_path)
        current_hash = _path_hash_or_none(destination)
        if current_hash != baseline_hash:
            if _shared_collaboration_enabled(relative_path, accepted_claims_by_slot, repo_root):
                merged = _merge_shared_text_change(destination, baseline_path, source)
                if merged:
                    merged_paths.append(relative_path)
                    continue
            merge_conflicts.append(relative_path)
            continue
        if source.exists() or source.is_symlink():
            _copy_file_like(source, destination)
        else:
            _remove_path(destination)
        merged_paths.append(relative_path)
    return tuple(sorted(merge_conflicts)), tuple(sorted(merged_paths))


def _postprocess_implementation_outcomes(
    repo_root: Path,
    *,
    outcomes: Sequence[AgentOutcome],
    worktree_paths: Sequence[Path],
    baseline_root: Path,
    baseline_manifest: dict[str, str],
    accepted_claims_by_slot: dict[int, tuple[ClaimSpec, ...]],
    merge_mode: str,
) -> tuple[tuple[AgentOutcome, ...], str | None]:
    worktree_by_slot = dict(enumerate(worktree_paths))
    processed: list[AgentOutcome] = []
    for outcome in outcomes:
        worktree_path = worktree_by_slot.get(outcome.slot)
        if worktree_path is None:
            processed.append(outcome)
            continue
        effective_claims = accepted_claims_by_slot.get(outcome.slot, outcome.claim_specs)
        editable_claims = _editable_claim_specs(effective_claims)
        _current_manifest, changed_paths = _changed_paths_from_manifest(worktree_path, baseline_manifest)
        scope_violations = tuple(sorted(
            path
            for path in changed_paths
            if not any(_path_matches_claim_spec(path, claim, repo_root) for claim in editable_claims)
        ))
        merged_paths: tuple[str, ...] = ()
        merge_conflicts: tuple[str, ...] = ()
        should_merge = False
        if merge_mode == "completed":
            should_merge = outcome.exit_code == 0
        elif merge_mode == "partial":
            should_merge = True
        if should_merge and not scope_violations:
            merge_conflicts, merged_paths = _merge_worktree_changes(
                repo_root,
                worktree_path=worktree_path,
                baseline_root=baseline_root,
                baseline_manifest=baseline_manifest,
                changed_paths=changed_paths,
                accepted_claims_by_slot=accepted_claims_by_slot,
            )
        processed.append(replace(
            outcome,
            worktree_path=str(worktree_path),
            claims=_claim_paths(effective_claims),
            claim_specs=effective_claims,
            changed_paths=changed_paths,
            merged_paths=merged_paths,
            merge_conflicts=merge_conflicts,
            scope_violations=scope_violations,
        ))

    if any(outcome.scope_violations for outcome in processed):
        return tuple(processed), "scope_violation"
    if any(outcome.merge_conflicts for outcome in processed):
        return tuple(processed), "merge_conflict"
    return tuple(processed), None


# ---------------------------------------------------------------------------
# Concurrent prompt builder
# ---------------------------------------------------------------------------

_CONCURRENT_PREAMBLE = """\
You are participating in a CONCURRENT multi-agent session.
You are {current_agent_name} ({current_agent_key}), running in slot {slot_index}.

{participants_section}

Your shared workspace is: {repo_root}

## Concurrent Mode Rules
- You are running AT THE SAME TIME as the other agents — not taking turns.
- You are running inside an isolated per-agent worktree. Any edits stay local to that worktree until the relay validates and merges them.
- The relay writes local pane snapshot files for you. Read those files instead of invoking tmux commands yourself.
  {pane_snapshot_instructions}
- A shared activity log is at: {workspace_log}
- There is no interactive approval loop in concurrent mode. Do not wait for the user to approve commands.
- Before editing a file, check its current state — another agent may have changed it.
- Coordinate: decide who handles what. Don't duplicate work.
- If a command is blocked or denied, adapt your approach and report that in RELAY_STATUS instead of asking for approval.

{phase_rules}

## Task

{continuation_section}

{task}
"""


def _build_concurrent_prompt(
    task: str,
    slot: int,
    agent_key: str,
    all_agents: Sequence[str],
    repo_root: Path,
    workspace_log: Path,
    pane_snapshot_paths: Sequence[Path],
    phase: str = "implementation",
    continued_from_session_id: str | None = None,
    continued_workspace_log: Path | None = None,
    continued_session_root: Path | None = None,
    claim_ledger_path: Path | None = None,
    accepted_claims_by_slot: dict[int, tuple[ClaimSpec, ...]] | None = None,
) -> str:
    agent_name = get_agent_display_name(agent_key)
    others = [(i, a) for i, a in enumerate(all_agents) if i != slot]
    unique_others = list(dict.fromkeys(a for _, a in others))

    if unique_others:
        lines = ["Other agents running concurrently:"]
        for i, a in others:
            lines.append(f"- Slot {i}: {get_agent_display_name(a)} ({a})")
        participants_section = "\n".join(lines)
    else:
        participants_section = ""

    # Build pane snapshot instructions
    pane_lines = []
    for i, a in others:
        name = get_agent_display_name(a)
        pane_lines.append(f"  Slot {i} ({name}): {pane_snapshot_paths[i]}")
    pane_snapshot_instructions = "\n".join(pane_lines) if pane_lines else "  No other agent snapshots."

    continuation_lines = []
    if continued_from_session_id:
        continuation_lines.extend([
            "## Continuation Context",
            f"- This run continues prior relay session: {continued_from_session_id}",
        ])
        if continued_workspace_log is not None:
            continuation_lines.append(f"- Prior workspace log: {continued_workspace_log}")
        if continued_session_root is not None:
            continuation_lines.append(f"- Prior session root: {continued_session_root}")
        continuation_lines.append("- Build on that existing work. Do not restart from scratch.")
    continuation_section = "\n".join(continuation_lines)

    if phase == "planning":
        phase_lines = [
            "## Planning Phase",
            "- This is the planning phase. Do not make implementation changes in this phase unless you are only prototyping inside your isolated worktree.",
            "- Inspect the repo, snapshot files, and shared log, then decide who owns what before implementation begins.",
            '- End with a machine-readable status line:',
            '  RELAY_STATUS: {"status":"planning","reason":"...","claims":[{"path":"README.md","role":"owner"},{"path":"src/agent_relay/","role":"reviewer"}],"remaining_work":["implement your claimed slice"],"verification":[]}',
            "- Allowed statuses: planning, blocked, error",
            "- Use planning only if claims is non-empty and concrete.",
            "- Claim roles: owner = exclusive editor, shared = multiple agents may edit and the relay will try to merge, reviewer = inspect/review only and must not edit that scope.",
            "- Claims must be repo-relative file paths or directory paths. Use a trailing / for directory claims.",
            "- If you post multiple RELAY_STATUS lines during the session, the last one wins.",
        ]
    else:
        phase_lines = [
            "## Implementation Phase",
            "- Planning is complete. Implement only the work assigned in the accepted claim ledger.",
        ]
        if claim_ledger_path is not None:
            phase_lines.append(f"- Accepted claim ledger: {claim_ledger_path}")
        own_claims = accepted_claims_by_slot.get(slot, ()) if accepted_claims_by_slot else ()
        phase_lines.append(
            "- Your accepted claims: "
            + (
                ", ".join(f"{claim.role}:{claim.path}" for claim in own_claims)
                if own_claims
                else "None recorded"
            )
        )
        if accepted_claims_by_slot:
            phase_lines.append("- Accepted claims for all slots:")
            for other_slot, claims in sorted(accepted_claims_by_slot.items()):
                agent_label = get_agent_display_name(all_agents[other_slot])
                claim_text = ", ".join(f"{claim.role}:{claim.path}" for claim in claims) if claims else "None recorded"
                phase_lines.append(f"  Slot {other_slot} ({agent_label}): {claim_text}")
        phase_lines.extend([
            "- Only owner and shared claims may be edited. Reviewer claims are review-only.",
            "- Stay within your accepted claims. If you discover a scope problem, report it with blocked instead of freelancing into another slot's work.",
            '- End with a machine-readable status line:',
            '  RELAY_STATUS: {"status":"continue","reason":"...","claims":[],"remaining_work":["..."],"verification":[]}',
            "- Allowed statuses: continue, blocked, done, error",
            "- Use done only when your part is truly complete and remaining_work is [].",
            "- Use error if you hit a terminal failure you could not resolve.",
            "- If you post multiple RELAY_STATUS lines during the session, the last one wins.",
        ])
    phase_rules = "\n".join(phase_lines)

    return _CONCURRENT_PREAMBLE.format(
        current_agent_name=agent_name,
        current_agent_key=agent_key,
        slot_index=slot,
        participants_section=participants_section,
        repo_root=str(repo_root),
        workspace_log=str(workspace_log),
        pane_snapshot_instructions=pane_snapshot_instructions,
        phase_rules=phase_rules,
        continuation_section=continuation_section,
        task=task,
    )


def _build_agent_command(agent_key: str, prompt_path: Path, repo_root: Path) -> str:
    """Build the underlying agent command for a concurrent slot."""
    adapter = get_agent_adapter(agent_key)
    cli = shlex.quote(adapter.cli_command)
    pp = shlex.quote(str(prompt_path))
    rr = shlex.quote(str(repo_root))

    if agent_key == "claude":
        # Concurrent mode must not depend on pane-local approval prompts.
        return f'cd {rr} && {cli} --permission-mode dontAsk -p "$(cat {pp})"'
    elif agent_key == "codex":
        return f'cd {rr} && {cli} -a never -s workspace-write "$(cat {pp})"'
    else:
        return f'cd {rr} && {cli} "$(cat {pp})"'


def _build_shell_command(
    agent_key: str,
    prompt_path: Path,
    repo_root: Path,
    exit_code_path: Path,
) -> str:
    """Build the slot shell command, persisting the agent's real exit code."""
    exit_path = shlex.quote(str(exit_code_path))
    inner = _build_agent_command(agent_key, prompt_path, repo_root)
    script = (
        f"rm -f {exit_path}; "
        f"{inner}; "
        'code=$?; '
        f'printf "%s\\n" "$code" > {exit_path}; '
        'exit "$code"'
    )
    return f"/bin/sh -lc {shlex.quote(script)}"


def _write_pane_snapshot(snapshot_path: Path, pane_content: str) -> None:
    write_text_atomic(snapshot_path, pane_content)


def _refresh_pane_snapshots(
    tmux_sessions: Sequence[str],
    snapshot_paths: Sequence[Path],
) -> None:
    for session_name, snapshot_path in zip(tmux_sessions, snapshot_paths, strict=False):
        if _tmux_session_exists(session_name):
            pane_content = _tmux_capture_pane(session_name, 0)
        else:
            pane_content = "(session terminated)"
        _write_pane_snapshot(snapshot_path, pane_content)


def _run_concurrent_phase(
    *,
    session_id: str,
    phase: str,
    agents: Sequence[str],
    commands: Sequence[str],
    worktree_paths: Sequence[Path],
    exit_code_paths: Sequence[Path],
    pane_snapshot_paths: Sequence[Path],
    wlog: WorkspaceLog,
    deadline_timestamp: float,
    claim_ledger_path: Path | None = None,
    continued_workspace_log_path: Path | None = None,
    on_agent_start: Callable[[int, str, str], None] | None = None,
    on_agent_done: Callable[[AgentOutcome], None] | None = None,
) -> PhaseRunResult:
    tmux_sessions = tuple(
        _tmux_session_name(session_id, slot, phase=phase)
        for slot in range(len(agents))
    )
    started_at = [utc_timestamp() for _ in agents]
    session_names_by_slot = dict(enumerate(tmux_sessions))

    for slot, tmux_session in enumerate(tmux_sessions):
        _tmux(
            "new-session", "-d",
            "-s", tmux_session,
            "-x", "200", "-y", "50",
            commands[slot],
        )
        _tmux(
            "set-window-option",
            "-t", f"{tmux_session}:0",
            "remain-on-exit",
            "on",
            check=False,
        )
        _tmux(
            "set-option",
            "-t", tmux_session,
            "mouse",
            "on",
            check=False,
        )

        wlog.append(LogEntry(
            timestamp=started_at[slot],
            agent_key=agents[slot],
            agent_slot=slot,
            entry_type="agent_started",
            summary=f"Started {phase} in tmux session {tmux_session}.",
        ))
        if on_agent_start:
            on_agent_start(slot, agents[slot], tmux_session)

    _refresh_pane_snapshots(tmux_sessions, pane_snapshot_paths)
    _sync_worktree_coordination_files(
        worktree_paths=worktree_paths,
        pane_snapshot_paths=pane_snapshot_paths,
        workspace_log_path=wlog.path,
        claim_ledger_path=claim_ledger_path,
        continued_workspace_log_path=continued_workspace_log_path,
    )

    stop_reason = "completed"
    finished_slots: dict[int, AgentOutcome] = {}
    reported_slots: set[int] = set()

    def maybe_report_outcome(outcome: AgentOutcome) -> None:
        if not on_agent_done or outcome.slot in reported_slots:
            return
        on_agent_done(outcome)
        reported_slots.add(outcome.slot)

    try:
        while len(finished_slots) < len(agents):
            if time.time() > deadline_timestamp:
                stop_reason = "max_time"
                break

            _refresh_pane_snapshots(tmux_sessions, pane_snapshot_paths)
            _sync_worktree_coordination_files(
                worktree_paths=worktree_paths,
                pane_snapshot_paths=pane_snapshot_paths,
                workspace_log_path=wlog.path,
                claim_ledger_path=claim_ledger_path,
                continued_workspace_log_path=continued_workspace_log_path,
            )

            for slot in range(len(agents)):
                if slot in finished_slots:
                    continue

                tmux_session = session_names_by_slot[slot]
                if not _tmux_session_exists(tmux_session):
                    finished_slots[slot] = _build_outcome(
                        slot=slot,
                        agent_key=agents[slot],
                        tmux_session=tmux_session,
                        phase=phase,
                        worktree_path=worktree_paths[slot],
                        pane_content="(session terminated)",
                        exit_code=None,
                        started_at=started_at[slot],
                        finished_at=utc_timestamp(),
                    )
                    stop_reason = "interrupted"
                    continue

                if _tmux_pane_dead(tmux_session, 0):
                    finished_at = utc_timestamp()
                    pane_content = _tmux_capture_pane(tmux_session, 0)
                    outcome = _build_outcome(
                        slot=slot,
                        agent_key=agents[slot],
                        tmux_session=tmux_session,
                        phase=phase,
                        worktree_path=worktree_paths[slot],
                        exit_code=_read_exit_code(exit_code_paths[slot]),
                        pane_content=pane_content,
                        started_at=started_at[slot],
                        finished_at=finished_at,
                    )
                    finished_slots[slot] = outcome

                    wlog.append(LogEntry(
                        timestamp=finished_at,
                        agent_key=agents[slot],
                        agent_slot=slot,
                        entry_type="signal" if outcome.done_signal else "turn_complete",
                        summary=outcome.summary,
                    ))
                    maybe_report_outcome(outcome)

            if stop_reason != "completed":
                break
            if len(finished_slots) < len(agents):
                time.sleep(_POLL_INTERVAL)

    except KeyboardInterrupt:
        stop_reason = "interrupted"

    for slot in range(len(agents)):
        if slot not in finished_slots:
            pane_content = ""
            tmux_session = session_names_by_slot[slot]
            if _tmux_session_exists(tmux_session):
                pane_content = _tmux_capture_pane(tmux_session, 0)
            finished_slots[slot] = _build_outcome(
                slot=slot,
                agent_key=agents[slot],
                tmux_session=tmux_session,
                phase=phase,
                worktree_path=worktree_paths[slot],
                exit_code=None,
                pane_content=pane_content,
                started_at=started_at[slot],
                finished_at=utc_timestamp(),
            )

    for tmux_session in tmux_sessions:
        if _tmux_session_exists(tmux_session):
            _tmux("kill-session", "-t", tmux_session, check=False)

    outcomes = tuple(sorted(finished_slots.values(), key=lambda o: o.slot))
    for outcome in outcomes:
        maybe_report_outcome(outcome)
    return PhaseRunResult(
        phase=phase,
        stop_reason=stop_reason,
        tmux_sessions=tmux_sessions,
        outcomes=outcomes,
    )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

_POLL_INTERVAL = 5  # seconds between completion checks


def run_concurrent(
    repo_root: Path,
    *,
    agents: Sequence[str],
    task: str,
    continue_from_session_id: str | None = None,
    max_time_seconds: int = 600,
    owner: str = "cli:race",
    on_agent_start: Callable[[int, str, str], None] | None = None,
    on_agent_done: Callable[[AgentOutcome], None] | None = None,
) -> ConcurrentResult:
    """Run agents concurrently in separate tmux sessions with shared visibility."""
    if len(agents) < 2:
        raise SystemExit("Concurrent mode requires at least 2 agents.")

    _require_tmux()
    require_available(agents)
    if continue_from_session_id and not is_session(repo_root, continue_from_session_id):
        raise SystemExit(f"Session not found: {continue_from_session_id}")
    _prune_stale_worktrees(repo_root)

    start_time = datetime.now(UTC)
    deadline_timestamp = start_time.timestamp() + max_time_seconds
    session_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
    agent_names = [get_agent_display_name(a) for a in agents]
    continued_workspace_log = (
        workspace_log_path(repo_root, continue_from_session_id)
        if continue_from_session_id is not None
        else None
    )

    start_session(
        repo_root,
        session_id=session_id,
        objective=task,
        # Concurrency is the execution mode; the persisted workstream kind
        # still needs to satisfy the session schema.
        workstream_kind="mixed",
        initial_agent=agents[0],
        next_action=f"Race with {', '.join(agent_names)}",
        snapshot_mode=None,
        owner=f"{owner}:start",
    )

    # Setup directories and workspace log
    cdir = concurrent_dir(repo_root, session_id)
    cdir.mkdir(parents=True, exist_ok=True)
    wlog_path = workspace_log_path(repo_root, session_id)
    wlog = WorkspaceLog(wlog_path)
    claim_ledger_path = _claims_ledger_path(repo_root, session_id)

    # Prepare per-agent files up front so prompts can reference other sessions.
    pane_snapshot_paths: list[Path] = []
    agent_dirs: list[Path] = []
    for slot, agent_key in enumerate(agents):
        agent_dir = concurrent_agent_dir(repo_root, session_id, slot)
        agent_dir.mkdir(parents=True, exist_ok=True)
        agent_dirs.append(agent_dir)
        pane_snapshot_path = agent_dir / "pane.txt"
        pane_snapshot_paths.append(pane_snapshot_path)
        write_text_atomic(pane_snapshot_path, "")

    baseline_paths = _current_repo_file_paths(repo_root)
    baseline_manifest = _build_baseline_manifest(repo_root, baseline_paths)
    baseline_root = _create_baseline_snapshot(
        repo_root,
        session_id=session_id,
        relative_paths=baseline_paths,
    )
    worktree_paths = tuple(
        _create_agent_worktree(
            repo_root,
            session_id=session_id,
            slot=slot,
            baseline_paths=baseline_paths,
        )
        for slot in range(len(agents))
    )
    _sync_worktree_coordination_files(
        worktree_paths=worktree_paths,
        pane_snapshot_paths=pane_snapshot_paths,
        workspace_log_path=wlog_path,
        continued_workspace_log_path=continued_workspace_log,
    )

    planning_commands: list[str] = []
    planning_exit_code_paths: list[Path] = []
    for slot, agent_key in enumerate(agents):
        agent_dir = agent_dirs[slot]
        worktree_path = worktree_paths[slot]
        planning_exit_code_path = agent_dir / "planning-exit-code.txt"
        planning_exit_code_paths.append(planning_exit_code_path)

        prompt_text = _build_concurrent_prompt(
            task=task,
            slot=slot,
            agent_key=agent_key,
            all_agents=agents,
            repo_root=worktree_path,
            workspace_log=_worktree_workspace_log_path(worktree_path),
            pane_snapshot_paths=_worktree_snapshot_paths(worktree_path, len(agents)),
            phase="planning",
            continued_from_session_id=continue_from_session_id,
            continued_workspace_log=(
                _worktree_continued_workspace_log_path(worktree_path)
                if continue_from_session_id is not None
                else None
            ),
            continued_session_root=None,
        )
        prompt_path = agent_dir / "planning-prompt.md"
        prompt_path.write_text(prompt_text, encoding="utf-8")
        planning_commands.append(
            _build_shell_command(agent_key, prompt_path, worktree_path, planning_exit_code_path)
        )

    planning_phase = _run_concurrent_phase(
        session_id=session_id,
        phase="planning",
        agents=agents,
        commands=planning_commands,
        worktree_paths=worktree_paths,
        exit_code_paths=planning_exit_code_paths,
        pane_snapshot_paths=pane_snapshot_paths,
        wlog=wlog,
        deadline_timestamp=deadline_timestamp,
        continued_workspace_log_path=continued_workspace_log,
    )

    if planning_phase.stop_reason != "completed":
        _write_claim_ledger(
            claim_ledger_path,
            session_id=session_id,
            continued_from_session_id=continue_from_session_id,
            outcomes=planning_phase.outcomes,
            status=planning_phase.stop_reason,
        )
        outcomes = planning_phase.outcomes
        tmux_sessions = planning_phase.tmux_sessions
        stop_reason = planning_phase.stop_reason
    else:
        planning_status, accepted_claims = _classify_planning_result(
            session_id,
            continue_from_session_id,
            claim_ledger_path,
            planning_phase.outcomes,
        )
        if planning_status != "accepted":
            outcomes = planning_phase.outcomes
            tmux_sessions = planning_phase.tmux_sessions
            stop_reason = planning_status
        else:
            _sync_worktree_coordination_files(
                worktree_paths=worktree_paths,
                pane_snapshot_paths=pane_snapshot_paths,
                workspace_log_path=wlog_path,
                claim_ledger_path=claim_ledger_path,
                continued_workspace_log_path=continued_workspace_log,
            )
            implementation_commands: list[str] = []
            implementation_exit_code_paths: list[Path] = []
            for slot, agent_key in enumerate(agents):
                agent_dir = agent_dirs[slot]
                worktree_path = worktree_paths[slot]
                implementation_exit_code_path = agent_dir / "implementation-exit-code.txt"
                implementation_exit_code_paths.append(implementation_exit_code_path)
                prompt_text = _build_concurrent_prompt(
                    task=task,
                    slot=slot,
                    agent_key=agent_key,
                    all_agents=agents,
                    repo_root=worktree_path,
                    workspace_log=_worktree_workspace_log_path(worktree_path),
                    pane_snapshot_paths=_worktree_snapshot_paths(worktree_path, len(agents)),
                    phase="implementation",
                    continued_from_session_id=continue_from_session_id,
                    continued_workspace_log=(
                        _worktree_continued_workspace_log_path(worktree_path)
                        if continue_from_session_id is not None
                        else None
                    ),
                    continued_session_root=None,
                    claim_ledger_path=_worktree_claim_ledger_path(worktree_path),
                    accepted_claims_by_slot=accepted_claims,
                )
                prompt_path = agent_dir / "implementation-prompt.md"
                prompt_path.write_text(prompt_text, encoding="utf-8")
                implementation_commands.append(
                    _build_shell_command(agent_key, prompt_path, worktree_path, implementation_exit_code_path)
                )

            implementation_phase = _run_concurrent_phase(
                session_id=session_id,
                phase="implementation",
                agents=agents,
                commands=implementation_commands,
                worktree_paths=worktree_paths,
                exit_code_paths=implementation_exit_code_paths,
                pane_snapshot_paths=pane_snapshot_paths,
                wlog=wlog,
                deadline_timestamp=deadline_timestamp,
                claim_ledger_path=claim_ledger_path,
                continued_workspace_log_path=continued_workspace_log,
                on_agent_start=on_agent_start,
                on_agent_done=on_agent_done,
            )
            merge_mode = "none"
            if implementation_phase.stop_reason == "completed":
                merge_mode = "completed"
            elif implementation_phase.stop_reason in {"max_time", "interrupted"}:
                merge_mode = "partial"
            outcomes, enforcement_stop_reason = _postprocess_implementation_outcomes(
                repo_root,
                outcomes=implementation_phase.outcomes,
                worktree_paths=worktree_paths,
                baseline_root=baseline_root,
                baseline_manifest=baseline_manifest,
                accepted_claims_by_slot=accepted_claims,
                merge_mode=merge_mode,
            )
            tmux_sessions = implementation_phase.tmux_sessions
            if implementation_phase.stop_reason == "completed":
                base_stop_reason = _classify_stop_reason("all_done", outcomes)
                if base_stop_reason == "agent_error":
                    stop_reason = base_stop_reason
                elif enforcement_stop_reason is not None:
                    stop_reason = enforcement_stop_reason
                else:
                    stop_reason = base_stop_reason
            else:
                stop_reason = implementation_phase.stop_reason

    should_preserve_worktrees = any(
        set(outcome.changed_paths) != set(outcome.merged_paths)
        for outcome in outcomes
    )
    if not should_preserve_worktrees:
        _cleanup_worktrees(repo_root, worktree_paths)

    elapsed = (datetime.now(UTC) - start_time).total_seconds()

    return ConcurrentResult(
        session_id=session_id,
        agents=tuple(agents),
        tmux_sessions=tuple(tmux_sessions),
        continued_from_session_id=continue_from_session_id,
        claim_ledger_path=str(claim_ledger_path),
        stop_reason=stop_reason,
        elapsed_seconds=round(elapsed, 1),
        outcomes=tuple(outcomes),
    )
