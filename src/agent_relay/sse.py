"""Server-Sent Events helpers for the stdlib dashboard server."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime

DEFAULT_TICK_SECONDS = 1.0
DEFAULT_HEARTBEAT_SECONDS = 15.0
DEFAULT_RETRY_MS = 3000


def format_event(event: str | None, data: str) -> bytes:
    """Encode one SSE message.

    ``data`` may be multi-line; the SSE wire format requires a ``data:``
    prefix on every line and a blank line after each message.
    """
    out: list[str] = []
    if event:
        out.append(f"event: {event}")
    for line in data.splitlines() or [""]:
        out.append(f"data: {line}")
    out.append("")
    out.append("")
    return "\n".join(out).encode()


def format_heartbeat() -> bytes:
    ts = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    return f": heartbeat {ts}\n\n".encode()


def format_retry(retry_ms: int = DEFAULT_RETRY_MS) -> bytes:
    return f"retry: {retry_ms}\n\n".encode()


def stream_updates(
    *,
    build_payload: Callable[[], dict[str, object]],
    stop_event: threading.Event,
    tick_seconds: float = DEFAULT_TICK_SECONDS,
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
) -> Iterator[bytes]:
    """Yield SSE-encoded frames until ``stop_event`` is set.

    An ``update`` event is emitted immediately on connect and then only
    when semantic payload content changes. ``generatedAt`` / ``renderedAt``
    are intentionally ignored for hashing so an idle page does not churn
    solely because the server re-rendered the same regions at a new time.
    """
    yield format_retry()

    last_hash = ""
    last_heartbeat = time.monotonic()
    tick = max(0.0, float(tick_seconds))
    heartbeat = max(0.0, float(heartbeat_seconds))

    while not stop_event.is_set():
        try:
            payload = build_payload()
        except Exception:  # noqa: BLE001
            now = time.monotonic()
            if now - last_heartbeat >= heartbeat:
                yield format_heartbeat()
                last_heartbeat = now
            time.sleep(tick)
            continue

        body = json.dumps(payload, separators=(",", ":"))
        new_hash = _payload_hash(payload)
        now = time.monotonic()

        if new_hash != last_hash:
            yield format_event("update", body)
            last_hash = new_hash
            last_heartbeat = now
        elif now - last_heartbeat >= heartbeat:
            yield format_heartbeat()
            last_heartbeat = now

        time.sleep(tick)

    yield format_event("shutdown", "{}")


def _payload_hash(payload: dict[str, object]) -> str:
    semantic = dict(payload)
    semantic.pop("generatedAt", None)
    semantic.pop("renderedAt", None)
    body = json.dumps(semantic, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()


__all__ = [
    "DEFAULT_HEARTBEAT_SECONDS",
    "DEFAULT_RETRY_MS",
    "DEFAULT_TICK_SECONDS",
    "format_event",
    "format_heartbeat",
    "format_retry",
    "stream_updates",
]
