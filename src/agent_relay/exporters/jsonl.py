"""JSONL exporter — streams metric events as one JSON object per line.

Subscribes to a :class:`WatchSource` and emits a metrics line each time a
turn completes; emits a final session-level rollup when the session
reaches a terminal status (or when the source's iterator returns, e.g.
``--no-follow``).

Two delivery modes share the same trigger logic:

* ``output`` text stream (default ``sys.stdout``) — for piping into
  ``jq`` / ``grep`` / files.
* ``webhook_url`` — POSTs each line as a JSON body. Non-2xx responses
  are logged to stderr; failures never block subsequent emissions.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Iterable, Mapping, TextIO
from urllib import error as urllib_error
from urllib import request as urllib_request

from agent_relay.metrics import (
    SessionMetrics,
    TurnMetrics,
    extract_session_metrics,
    extract_turn_metrics,
)
from agent_relay.metrics_ui import metrics_to_jsonl_line
from agent_relay.watch import WatchEvent, WatchSource, is_terminal_status


def tail_jsonl(
    source: WatchSource,
    *,
    output: TextIO | None = None,
    webhook_url: str | None = None,
    webhook_headers: Mapping[str, str] | None = None,
    webhook_timeout: float = 5.0,
) -> int:
    """Stream metric events from a session to stdout and/or a webhook.

    Returns the exit code (0 normal, 130 on KeyboardInterrupt).
    """
    out = output if output is not None else sys.stdout
    repo = source.repo_root
    sid = source.session_id
    seen_session_rollup = False

    try:
        for event in source.iter_events():
            line = _line_for_event(repo, sid, event)
            if line is not None:
                _deliver(line, out, webhook_url, webhook_headers, webhook_timeout)

            if event.kind == "status_change":
                new_status = event.payload.get("to_status") or event.payload.get(
                    "current_status"
                )
                if is_terminal_status(new_status) and not seen_session_rollup:
                    rollup = _session_rollup_line(repo, sid)
                    if rollup is not None:
                        _deliver(
                            rollup, out, webhook_url, webhook_headers, webhook_timeout
                        )
                        seen_session_rollup = True

        # Source iterator finished (e.g. follow=False or terminal status).
        if not seen_session_rollup:
            rollup = _session_rollup_line(repo, sid)
            if rollup is not None:
                _deliver(rollup, out, webhook_url, webhook_headers, webhook_timeout)
    except KeyboardInterrupt:
        return 130
    return 0


def post_webhook(
    source: WatchSource,
    *,
    webhook_url: str,
    webhook_headers: Mapping[str, str] | None = None,
    webhook_timeout: float = 5.0,
) -> int:
    """Webhook-only delivery (no stdout). Thin wrapper over :func:`tail_jsonl`."""
    return tail_jsonl(
        source,
        output=_NullStream(),
        webhook_url=webhook_url,
        webhook_headers=webhook_headers,
        webhook_timeout=webhook_timeout,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _line_for_event(repo: Path, session_id: str, event: WatchEvent) -> str | None:
    if event.kind != "turn_completed":
        return None
    turn_number = event.payload.get("turn_number")
    if not isinstance(turn_number, int):
        return None
    try:
        turn = extract_turn_metrics(repo, session_id, turn_number)
    except Exception:
        return None
    return metrics_to_jsonl_line(turn)


def _session_rollup_line(repo: Path, session_id: str) -> str | None:
    try:
        session = extract_session_metrics(repo, session_id)
    except Exception:
        return None
    return metrics_to_jsonl_line(session)


def _deliver(
    line: str,
    output: TextIO,
    webhook_url: str | None,
    webhook_headers: Mapping[str, str] | None,
    timeout: float,
) -> None:
    output.write(line + "\n")
    output.flush()
    if webhook_url:
        _post_line(line, webhook_url, webhook_headers, timeout)


def _post_line(
    line: str,
    url: str,
    headers: Mapping[str, str] | None,
    timeout: float,
) -> None:
    body = line.encode("utf-8")
    base_headers = {"Content-Type": "application/json"}
    if headers:
        base_headers.update(headers)
    req = urllib_request.Request(url, data=body, headers=base_headers, method="POST")
    try:
        with urllib_request.urlopen(req, timeout=timeout) as resp:
            if not (200 <= resp.status < 300):
                sys.stderr.write(
                    f"webhook delivery failed: HTTP {resp.status} from {url}\n"
                )
    except urllib_error.HTTPError as exc:
        sys.stderr.write(f"webhook delivery failed: HTTP {exc.code} from {url}\n")
    except (urllib_error.URLError, TimeoutError, OSError) as exc:
        sys.stderr.write(f"webhook delivery failed: {exc} ({url})\n")


def parse_header_pairs(pairs: Iterable[str] | None) -> dict[str, str]:
    """Parse ``Key: Value`` or ``Key=Value`` arguments into a header dict."""
    out: dict[str, str] = {}
    if not pairs:
        return out
    for raw in pairs:
        if ":" in raw:
            key, _, value = raw.partition(":")
        elif "=" in raw:
            key, _, value = raw.partition("=")
        else:
            raise ValueError(
                f"Invalid header (expected 'Key: Value' or 'Key=Value'): {raw!r}"
            )
        key = key.strip()
        value = value.strip()
        if not key:
            raise ValueError(f"Invalid header (empty key): {raw!r}")
        out[key] = value
    return out


class _NullStream:
    def write(self, _data: str) -> int:  # noqa: D401
        return 0

    def flush(self) -> None:
        return None


__all__ = ["tail_jsonl", "post_webhook", "parse_header_pairs"]
