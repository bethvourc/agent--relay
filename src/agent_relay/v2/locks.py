from __future__ import annotations

import fcntl
import json
import os
import socket
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, TextIO

from agent_relay.v2.errors import LockTimeoutError
from agent_relay.v2.layout import repo_lock_path, session_lock_path


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(slots=True)
class FileLock:
    path: Path
    scope: str
    owner: str
    timeout_seconds: float
    poll_interval_seconds: float
    handle: TextIO | None = None
    acquired_at: str | None = None
    exclusive: bool = True

    def acquire(self) -> FileLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout_seconds

        while True:
            handle = self.path.open("a+", encoding="utf-8")
            try:
                lock_mode = fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH
                fcntl.flock(handle.fileno(), lock_mode | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                if time.monotonic() >= deadline:
                    raise LockTimeoutError(f"Timed out acquiring {self.scope} lock at {self.path}")
                time.sleep(self.poll_interval_seconds)
                continue

            self.handle = handle
            self.acquired_at = utc_now()
            self._write_metadata()
            return self

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None

    def __enter__(self) -> FileLock:
        if self.handle is not None:
            return self
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

    def _write_metadata(self) -> None:
        if self.handle is None or self.acquired_at is None:
            return
        metadata = {
            "scope": self.scope,
            "owner": self.owner,
            "mode": "exclusive" if self.exclusive else "shared",
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "acquired_at": self.acquired_at,
        }
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
        self.handle.flush()
        os.fsync(self.handle.fileno())


def acquire_repo_lock(
    repo_root: Path,
    *,
    owner: str,
    timeout_seconds: float = 5.0,
    poll_interval_seconds: float = 0.05,
) -> FileLock:
    return FileLock(
        path=repo_lock_path(repo_root),
        scope="repo",
        owner=owner,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    ).acquire()


class LockHandle(Protocol):
    def release(self) -> None: ...


@dataclass(slots=True)
class CompositeLock:
    locks: tuple[FileLock, ...]

    def release(self) -> None:
        for lock in reversed(self.locks):
            lock.release()

    def __enter__(self) -> CompositeLock:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def acquire_session_lock(
    repo_root: Path,
    session_id: str,
    *,
    owner: str,
    timeout_seconds: float = 5.0,
    poll_interval_seconds: float = 0.05,
) -> CompositeLock:
    repo_guard = FileLock(
        path=repo_lock_path(repo_root),
        scope="repo-shared",
        owner=owner,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        exclusive=False,
    ).acquire()
    try:
        session_guard = FileLock(
        path=session_lock_path(repo_root, session_id),
        scope=f"session:{session_id}",
        owner=owner,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        ).acquire()
    except Exception:
        repo_guard.release()
        raise
    return CompositeLock((repo_guard, session_guard))
