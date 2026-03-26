from __future__ import annotations

from pathlib import Path


class V2Error(ValueError):
    """Base error for the v2 session core."""


class V2ValidationError(V2Error):
    """Raised when a v2 record fails schema validation."""


class LockTimeoutError(V2Error):
    """Raised when a repo or session lock cannot be acquired in time."""


class TransactionError(V2Error):
    """Raised when a transaction cannot be committed safely."""


class V2CorruptionError(V2Error):
    """Raised when canonical v2 state cannot be trusted."""

    def __init__(
        self,
        message: str,
        *,
        session_id: str | None = None,
        path: Path | None = None,
    ) -> None:
        details: list[str] = []
        if session_id:
            details.append(f"session={session_id}")
        if path:
            details.append(f"path={path}")
        suffix = f" ({', '.join(details)})" if details else ""
        super().__init__(f"{message}{suffix}")
        self.session_id = session_id
        self.path = path
