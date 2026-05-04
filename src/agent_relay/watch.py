"""Live watch source for an in-progress agent-relay session.

This module is the single source of truth for "what's happening in this
session right now." It polls the on-disk session artifacts (journal events,
workspace log, turn directories, the current turn's progressive output
stream, and the derived session view) and yields a unified stream of
``WatchEvent`` objects.

The module is purely an event iterator — it has no rendering responsibility.
The CLI handler chooses how to render the stream: a Rich live TUI, a JSONL
stream on stdout, or terse one-line-per-event quiet output.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Iterator
from typing import Any

from agent_relay.layout import (
    derived_view_path,
    journal_dir,
    turn_dir,
    turns_dir,
    workspace_log_path,
)
from agent_relay.read_views import list_sessions_for_dashboard
from agent_relay.storage import is_session

# Sessions in these phases are still doing work. The watcher exits on the
# first poll where the status moves out of this set (and follow=True).
_LIVE_STATUSES = frozenset({"active", "launching", "awaiting_resume", "paused"})

_TERMINAL_STATUSES = frozenset({"completed", "ready_for_handoff"})

# Filename pattern for journal events: {sequence:06d}-{event_type}.json
_JOURNAL_FILE_RE = re.compile(r"^(\d{6})-(.+)\.json$")

_TURN_DIR_RE = re.compile(r"^turn-(\d{3,})$")

# Workspace log heading pattern (matches workspace_log.WorkspaceLog format).
_WLOG_HEADING_RE = re.compile(
    r"^## \[([^\]]+)\] (.+?) \(slot (\d+)\) — (.+)$",
    re.MULTILINE,
)


def is_terminal_status(status: str | None) -> bool:
    """True if the given session status means the session has finished."""
    return status is not None and status in _TERMINAL_STATUSES


def is_live_status(status: str | None) -> bool:
    """True if the given session status means the session is still doing work."""
    return status is not None and status in _LIVE_STATUSES


@dataclass(frozen=True, slots=True)
class WatchEvent:
    """A single observable event from a watched session."""

    timestamp: str  # ISO-8601 UTC
    kind: str  # journal | workspace | turn_started | turn_completed
               # | output_chunk | status_change | heartbeat
    payload: dict[str, Any]
    sequence: int  # monotonic per-WatchSource

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "kind": self.kind,
            "payload": dict(self.payload),
            "sequence": self.sequence,
        }


@dataclass(frozen=True, slots=True)
class WatchSnapshot:
    """One-shot view of the session's current state."""

    session_id: str
    current_agent: str | None
    current_status: str | None
    objective: str
    current_turn: int | None
    turn_started_at: str | None
    elapsed_seconds: float | None
    last_turn_state: dict[str, Any] | None
    recent_events: tuple[WatchEvent, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "current_agent": self.current_agent,
            "current_status": self.current_status,
            "objective": self.objective,
            "current_turn": self.current_turn,
            "turn_started_at": self.turn_started_at,
            "elapsed_seconds": self.elapsed_seconds,
            "last_turn_state": self.last_turn_state,
            "recent_events": [e.to_dict() for e in self.recent_events],
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        # Accept both "...Z" and "+00:00" forms.
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def pick_latest_active_session(repo_root: Path) -> str | None:
    """Return the session_id of the most recently updated active session.

    "Active" means the derived current_status is in
    {active, launching, awaiting_resume, paused} — i.e. not completed/handoff.
    Returns ``None`` if there are no active sessions.
    """
    entries = list_sessions_for_dashboard(repo_root)
    candidates = [
        e for e in entries
        if is_live_status(e.get("current_status"))
        and e.get("health") != "corrupt"
    ]
    if not candidates:
        return None
    # list_sessions_for_dashboard already sorts by (updated_at, session_id)
    # descending, so the first matching candidate is the freshest.
    return candidates[0]["session_id"]


def pick_latest_session(repo_root: Path) -> dict[str, Any] | None:
    """Return the most recently updated session of any status.

    Returns the full dashboard entry (with session_id, current_status, etc.)
    so callers can communicate the fallback context. Skips corrupt sessions.
    Returns ``None`` if there are no sessions at all.
    """
    entries = list_sessions_for_dashboard(repo_root)
    for entry in entries:
        if entry.get("health") != "corrupt":
            return entry
    return None


# ---------------------------------------------------------------------------
# Pollers
# ---------------------------------------------------------------------------


class _JournalTail:
    """Yields new journal events as files appear in the journal/ directory."""

    def __init__(self, repo_root: Path, session_id: str) -> None:
        self._dir = journal_dir(repo_root, session_id)
        self._last_seen_seq = -1
        # Bootstrap: skip events that already existed when the watcher started,
        # so we only surface live activity.
        if self._dir.exists():
            for path in self._dir.glob("*.json"):
                m = _JOURNAL_FILE_RE.match(path.name)
                if m:
                    self._last_seen_seq = max(self._last_seen_seq, int(m.group(1)))

    def poll(self) -> list[tuple[str, dict[str, Any]]]:
        if not self._dir.exists():
            return []
        new: list[tuple[int, Path]] = []
        for path in self._dir.glob("*.json"):
            m = _JOURNAL_FILE_RE.match(path.name)
            if not m:
                continue
            seq = int(m.group(1))
            if seq > self._last_seen_seq:
                new.append((seq, path))
        new.sort(key=lambda item: item[0])
        out: list[tuple[str, dict[str, Any]]] = []
        for seq, path in new:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                # Partial / racy read: skip this poll, retry next time.
                continue
            self._last_seen_seq = seq
            out.append((
                "journal",
                {
                    "sequence": seq,
                    "event_type": data.get("type", path.stem),
                    "timestamp": data.get("timestamp"),
                    "phase_after": data.get("phase_after"),
                    "event_id": data.get("event_id"),
                },
            ))
        return out


class _WorkspaceLogTail:
    """Yields new entries appended to workspace-log.md."""

    def __init__(self, repo_root: Path, session_id: str) -> None:
        self._path = workspace_log_path(repo_root, session_id)
        self._last_size = self._path.stat().st_size if self._path.exists() else 0

    def poll(self) -> list[tuple[str, dict[str, Any]]]:
        if not self._path.exists():
            return []
        try:
            size = self._path.stat().st_size
        except OSError:
            return []
        if size <= self._last_size:
            return []
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                fh.seek(self._last_size)
                delta = fh.read()
        except OSError:
            return []
        self._last_size = size
        out: list[tuple[str, dict[str, Any]]] = []
        matches = list(_WLOG_HEADING_RE.finditer(delta))
        for i, m in enumerate(matches):
            timestamp = m.group(1)
            agent_name = m.group(2)
            slot = int(m.group(3))
            entry_type = m.group(4).lower().replace(" ", "_")
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(delta)
            summary = delta[start:end].strip()
            out.append((
                "workspace",
                {
                    "timestamp": timestamp,
                    "agent": agent_name,
                    "slot": slot,
                    "entry_type": entry_type,
                    "summary": summary,
                },
            ))
        return out


class _TurnTail:
    """Tracks the current turn directory.

    Emits ``turn_started`` when a new ``turn-NNN/`` directory appears, and
    ``turn_completed`` when its ``state.json`` lands.
    """

    def __init__(self, repo_root: Path, session_id: str) -> None:
        self._repo_root = repo_root
        self._session_id = session_id
        self._turns_root = turns_dir(repo_root, session_id)
        self._known: dict[int, _TurnState] = {}
        # Bootstrap with whatever is already on disk so we only emit
        # *new* turn events to the watcher.
        for n in self._scan_turn_numbers():
            self._known[n] = _TurnState(
                started_emitted=True,
                completed=self._has_state_json(n),
            )

    def current_turn(self) -> int | None:
        if not self._known:
            return None
        return max(self._known.keys())

    def started_at(self, turn_number: int) -> str | None:
        try:
            stat = turn_dir(self._repo_root, self._session_id, turn_number).stat()
        except OSError:
            return None
        return datetime.fromtimestamp(stat.st_mtime, UTC).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )

    def _scan_turn_numbers(self) -> list[int]:
        if not self._turns_root.exists():
            return []
        nums: list[int] = []
        for child in self._turns_root.iterdir():
            if not child.is_dir():
                continue
            m = _TURN_DIR_RE.match(child.name)
            if m:
                nums.append(int(m.group(1)))
        nums.sort()
        return nums

    def _has_state_json(self, turn_number: int) -> bool:
        return (
            turn_dir(self._repo_root, self._session_id, turn_number) / "state.json"
        ).exists()

    def poll(self) -> list[tuple[str, dict[str, Any]]]:
        out: list[tuple[str, dict[str, Any]]] = []
        for n in self._scan_turn_numbers():
            state = self._known.get(n)
            if state is None:
                started_at = self.started_at(n) or _utc_now_iso()
                out.append((
                    "turn_started",
                    {"turn_number": n, "started_at": started_at},
                ))
                self._known[n] = _TurnState(
                    started_emitted=True, completed=False
                )
                state = self._known[n]
            if not state.completed and self._has_state_json(n):
                payload: dict[str, Any] = {"turn_number": n}
                try:
                    raw = (
                        turn_dir(self._repo_root, self._session_id, n)
                        / "state.json"
                    ).read_text(encoding="utf-8")
                    payload["state"] = json.loads(raw)
                except (OSError, json.JSONDecodeError):
                    pass
                out.append(("turn_completed", payload))
                self._known[n] = _TurnState(
                    started_emitted=True, completed=True
                )
        return out


@dataclass(slots=True)
class _TurnState:
    started_emitted: bool
    completed: bool


class _OutputTail:
    """Tails ``turns/turn-{current}/output.jsonl`` line-by-line.

    Tolerates the writer flushing a partial last line by tracking the byte
    offset of the last consumed newline. Resets when the underlying turn
    advances.
    """

    def __init__(self, repo_root: Path, session_id: str) -> None:
        self._repo_root = repo_root
        self._session_id = session_id
        self._current_turn: int | None = None
        self._byte_offset = 0

    def reset_to_turn(self, turn_number: int) -> None:
        if turn_number == self._current_turn:
            return
        self._current_turn = turn_number
        self._byte_offset = 0

    def poll(self) -> list[tuple[str, dict[str, Any]]]:
        if self._current_turn is None:
            return []
        path = (
            turn_dir(self._repo_root, self._session_id, self._current_turn)
            / "output.jsonl"
        )
        if not path.exists():
            return []
        try:
            with path.open("rb") as fh:
                fh.seek(self._byte_offset)
                buf = fh.read()
        except OSError:
            return []
        if not buf:
            return []
        # Only consume through the last newline; leave any partial trailing
        # line in the file for the next poll.
        last_nl = buf.rfind(b"\n")
        if last_nl < 0:
            return []
        consumed = buf[: last_nl + 1]
        self._byte_offset += len(consumed)
        out: list[tuple[str, dict[str, Any]]] = []
        for raw_line in consumed.split(b"\n"):
            if not raw_line:
                continue
            try:
                line = raw_line.decode("utf-8")
            except UnicodeDecodeError:
                continue
            payload: dict[str, Any] = {
                "turn_number": self._current_turn,
                "line": line,
            }
            try:
                parsed = json.loads(line)
                payload["parsed"] = parsed
                # Best-effort tag for the UI.
                if isinstance(parsed, dict):
                    payload["event_subtype"] = (
                        parsed.get("type")
                        or (parsed.get("message") or {}).get("type")
                        or ""
                    )
            except json.JSONDecodeError:
                payload["event_subtype"] = "raw"
            out.append(("output_chunk", payload))
        return out


class _StatusPoller:
    """Reads the latest derived/view.json and emits transitions."""

    def __init__(self, repo_root: Path, session_id: str) -> None:
        self._path = derived_view_path(repo_root, session_id)
        self._last_status: str | None = None
        self._last_agent: str | None = None
        self._last_objective: str = ""
        # Prime initial state without emitting a transition event.
        snap = self.read()
        self._last_status = snap.get("status")
        self._last_agent = snap.get("current_agent")
        self._last_objective = snap.get("objective", "")

    def read(self) -> dict[str, Any]:
        """Return the current derived view (best-effort)."""
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            "status": data.get("phase") or data.get("current_status"),
            "current_agent": data.get("current_agent"),
            "objective": data.get("objective", ""),
        }

    def status(self) -> str | None:
        return self._last_status

    def agent(self) -> str | None:
        return self._last_agent

    def objective(self) -> str:
        return self._last_objective

    def poll(self) -> list[tuple[str, dict[str, Any]]]:
        snap = self.read()
        out: list[tuple[str, dict[str, Any]]] = []
        new_status = snap.get("status")
        new_agent = snap.get("current_agent")
        new_objective = snap.get("objective", "")
        if new_status != self._last_status or new_agent != self._last_agent:
            out.append((
                "status_change",
                {
                    "from_status": self._last_status,
                    "to_status": new_status,
                    "from_agent": self._last_agent,
                    "to_agent": new_agent,
                },
            ))
        self._last_status = new_status
        self._last_agent = new_agent
        if new_objective:
            self._last_objective = new_objective
        return out


# ---------------------------------------------------------------------------
# WatchSource
# ---------------------------------------------------------------------------


class WatchSource:
    """Polls a session's on-disk artifacts and yields ``WatchEvent`` items.

    The source is single-use. Iterate ``iter_events()`` exactly once.
    """

    def __init__(
        self,
        repo_root: Path,
        session_id: str,
        *,
        poll_interval: float = 0.25,
        follow: bool = True,
        heartbeat_interval: float = 1.0,
        sleep: Any = time.sleep,
        clock: Any = time.monotonic,
    ) -> None:
        if not is_session(repo_root, session_id):
            raise ValueError(f"not a relay session: {session_id}")
        self.repo_root = repo_root
        self.session_id = session_id
        self.poll_interval = max(0.05, float(poll_interval))
        self.follow = bool(follow)
        self.heartbeat_interval = max(0.1, float(heartbeat_interval))
        self._sleep = sleep
        self._clock = clock
        self._sequence = 0

        self._journal = _JournalTail(repo_root, session_id)
        self._workspace = _WorkspaceLogTail(repo_root, session_id)
        self._turns = _TurnTail(repo_root, session_id)
        self._output = _OutputTail(repo_root, session_id)
        self._status = _StatusPoller(repo_root, session_id)

        # Sync the output tail to the current turn (if any) so subsequent
        # progressive output is captured from the start of the watcher.
        cur = self._turns.current_turn()
        if cur is not None:
            self._output.reset_to_turn(cur)

    def _next_event(self, kind: str, payload: dict[str, Any]) -> WatchEvent:
        self._sequence += 1
        return WatchEvent(
            timestamp=_utc_now_iso(),
            kind=kind,
            payload=payload,
            sequence=self._sequence,
        )

    def snapshot(self) -> WatchSnapshot:
        """One-shot summary of the session state right now."""
        # Re-read status fresh so callers don't depend on prior polls.
        status_view = self._status.read()
        cur_turn = self._turns.current_turn()
        started_at = self._turns.started_at(cur_turn) if cur_turn else None
        elapsed: float | None = None
        if started_at:
            dt = _parse_iso(started_at)
            if dt is not None:
                elapsed = max(
                    0.0,
                    (datetime.now(UTC) - dt).total_seconds(),
                )

        last_state: dict[str, Any] | None = None
        if cur_turn:
            for n in range(cur_turn, 0, -1):
                state_path = (
                    turn_dir(self.repo_root, self.session_id, n) / "state.json"
                )
                if state_path.exists():
                    try:
                        last_state = json.loads(
                            state_path.read_text(encoding="utf-8")
                        )
                    except (OSError, json.JSONDecodeError):
                        last_state = None
                    break

        return WatchSnapshot(
            session_id=self.session_id,
            current_agent=status_view.get("current_agent"),
            current_status=status_view.get("status"),
            objective=status_view.get("objective", "") or self._status.objective(),
            current_turn=cur_turn,
            turn_started_at=started_at,
            elapsed_seconds=elapsed,
            last_turn_state=last_state,
        )

    def iter_events(self) -> Iterator[WatchEvent]:
        """Yield events until the session reaches a terminal status,
        ``follow`` is False, or the caller stops consuming."""
        last_heartbeat = self._clock()

        while True:
            # 1. Status changes — drives terminal-status detection.
            for kind, payload in self._status.poll():
                yield self._next_event(kind, payload)

            current_status = self._status.status()

            # 2. New turn dirs / completed-turn state.
            for kind, payload in self._turns.poll():
                if kind == "turn_started":
                    self._output.reset_to_turn(int(payload["turn_number"]))
                yield self._next_event(kind, payload)

            # 3. Progressive output for the current turn.
            for kind, payload in self._output.poll():
                yield self._next_event(kind, payload)

            # 4. Workspace activity log.
            for kind, payload in self._workspace.poll():
                yield self._next_event(kind, payload)

            # 5. Journal events.
            for kind, payload in self._journal.poll():
                yield self._next_event(kind, payload)

            # 6. Heartbeat (only if no other event was emitted recently).
            now = self._clock()
            if now - last_heartbeat >= self.heartbeat_interval:
                yield self._next_event(
                    "heartbeat",
                    {
                        "current_status": current_status,
                        "current_turn": self._turns.current_turn(),
                    },
                )
                last_heartbeat = now

            # Termination: in non-follow mode, exit after a single pass.
            if not self.follow:
                return
            if is_terminal_status(current_status):
                return

            self._sleep(self.poll_interval)
