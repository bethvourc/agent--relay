from __future__ import annotations

from pathlib import Path


class RelayError(ValueError):
    """Base error for the relay session core."""


class ValidationError(RelayError):
    """Raised when a persisted record fails schema validation."""


class LockTimeoutError(RelayError):
    """Raised when a repo or session lock cannot be acquired in time."""


class TransactionError(RelayError):
    """Raised when a transaction cannot be committed safely."""


class CorruptionError(RelayError):
    """Raised when canonical session state cannot be trusted."""

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
