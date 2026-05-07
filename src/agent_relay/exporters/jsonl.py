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

import sys
from collections.abc import Iterable, Mapping
from typing import TextIO
from urllib import error as urllib_error
from urllib import request as urllib_request

from agent_relay.alerts import (
    AlertConfig,
    evaluate_session,
    evaluate_turn,
    load_alert_config,
)
from agent_relay.metrics import (
    extract_session_metrics,
    extract_turn_metrics,
)
from agent_relay.metrics_ui import metrics_to_jsonl_line
from agent_relay.watch import WatchSource, is_terminal_status


def tail_jsonl(
    source: WatchSource,
    *,
    output: TextIO | None = None,
    webhook_url: str | None = None,
    webhook_headers: Mapping[str, str] | None = None,
    webhook_timeout: float = 5.0,
    alert_config: AlertConfig | None = None,
) -> int:
    """Stream metric events from a session to stdout and/or a webhook.

    On every ``turn_completed`` and on the final session rollup, threshold
    alerts (per ``alert_config`` or, if None, ``alerts.toml``) are
    evaluated and emitted as additional ``metrics.alert`` JSONL lines plus
    colored stderr lines.

    Returns the exit code (0 normal, 130 on KeyboardInterrupt).
    """
    out = output if output is not None else sys.stdout
    repo = source.repo_root
    sid = source.session_id
    cfg = alert_config if alert_config is not None else load_alert_config(repo)
    seen_session_rollup = False

    def _emit_session_rollup() -> None:
        nonlocal seen_session_rollup
        try:
            session = extract_session_metrics(repo, sid)
        except Exception:
            return
        line = metrics_to_jsonl_line(session)
        _deliver(line, out, webhook_url, webhook_headers, webhook_timeout)
        if not cfg.is_empty:
            for alert in evaluate_session(session, cfg):
                _emit_alert_line(alert, out, webhook_url, webhook_headers, webhook_timeout)
        seen_session_rollup = True

    try:
        for event in source.iter_events():
            if event.kind == "turn_completed":
                turn_number = event.payload.get("turn_number")
                if isinstance(turn_number, int):
                    try:
                        turn = extract_turn_metrics(repo, sid, turn_number)
                        session = extract_session_metrics(repo, sid)
                    except Exception:
                        turn = None
                        session = None
                    if turn is not None and session is not None:
                        _deliver(
                            metrics_to_jsonl_line(turn),
                            out,
                            webhook_url,
                            webhook_headers,
                            webhook_timeout,
                        )
                        if not cfg.is_empty:
                            for alert in evaluate_turn(turn, session, cfg):
                                _emit_alert_line(
                                    alert,
                                    out,
                                    webhook_url,
                                    webhook_headers,
                                    webhook_timeout,
                                )

            elif event.kind == "status_change":
                new_status = event.payload.get("to_status") or event.payload.get("current_status")
                if is_terminal_status(new_status) and not seen_session_rollup:
                    _emit_session_rollup()

        if not seen_session_rollup:
            _emit_session_rollup()
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


def _emit_alert_line(
    alert,
    output: TextIO,
    webhook_url: str | None,
    webhook_headers: Mapping[str, str] | None,
    timeout: float,
) -> None:
    line = alert.to_jsonl_line()
    output.write(line + "\n")
    output.flush()
    sys.stderr.write(_format_alert_stderr(alert))
    sys.stderr.flush()
    if webhook_url:
        _post_line(line, webhook_url, webhook_headers, timeout)


def _format_alert_stderr(alert) -> str:
    color = "\033[31m" if alert.severity == "critical" else "\033[33m"
    reset = "\033[0m"
    turn_part = f" turn {alert.turn_number}" if alert.turn_number is not None else ""
    return (
        f"{color}[{alert.severity}] {alert.rule}{reset} "
        f"session {alert.session_id}{turn_part}: {alert.message}\n"
    )


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
                sys.stderr.write(f"webhook delivery failed: HTTP {resp.status} from {url}\n")
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
            raise ValueError(f"Invalid header (expected 'Key: Value' or 'Key=Value'): {raw!r}")
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
