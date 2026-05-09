"""Prometheus exporter — pull-based scrape endpoint + HTML dashboard.

Renders cross-session metrics in the Prometheus text exposition format
(version 0.0.4). Stdlib only — no ``prometheus_client`` dependency.

Server lifecycle:

* :func:`serve_prometheus` runs a :class:`ThreadingHTTPServer` until
  Ctrl-C, returning exit code 0 (clean shutdown) or 130 (KeyboardInterrupt).
* ``GET /metrics`` → Prometheus text exposition.
* ``GET /`` and ``GET /dashboard`` → HTML dashboard (see
  :mod:`agent_relay.dashboard`).
* ``GET /dashboard/data`` → JSON payload for in-place dashboard refreshes.
* Other paths → 404.
* A small in-process cache (default 5s TTL) avoids re-extracting on every
  scrape or dashboard refresh.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agent_relay.dashboard import render_dashboard_html, render_dashboard_update_payload
from agent_relay.dashboard_query import parse_filter_from_query
from agent_relay.metrics import (
    CrossSessionMetrics,
    MetricsFilter,
    TokenUsage,
    extract_cross_session_metrics,
)

_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def render_prometheus_text(metrics: CrossSessionMetrics) -> str:
    """Render cross-session metrics in Prometheus text exposition format."""
    lines: list[str] = []

    # ---- Tokens ----
    lines.append("# HELP agent_relay_tokens_total Cumulative tokens by agent and direction")
    lines.append("# TYPE agent_relay_tokens_total counter")
    for agent in sorted(metrics.by_agent):
        usage = metrics.by_agent[agent]
        for direction, value in _token_directions(usage):
            if value is None:
                continue
            lines.append(
                f'agent_relay_tokens_total{{agent="{_esc(agent)}",direction="{direction}"}} {value}'
            )

    # ---- Cost ----
    lines.append("# HELP agent_relay_cost_usd_total Cumulative cost in USD by agent")
    lines.append("# TYPE agent_relay_cost_usd_total counter")
    for agent in sorted(metrics.cost_by_agent):
        cost = metrics.cost_by_agent[agent]
        lines.append(f'agent_relay_cost_usd_total{{agent="{_esc(agent)}"}} {_fmt_float(cost)}')

    # ---- Turn duration ----
    duration_sum, duration_count = _per_agent_durations(metrics)
    lines.append("# HELP agent_relay_turn_duration_ms Total and count of turn durations by agent")
    lines.append("# TYPE agent_relay_turn_duration_ms summary")
    for agent in sorted(set(duration_sum) | set(duration_count)):
        lines.append(
            f'agent_relay_turn_duration_ms_sum{{agent="{_esc(agent)}"}} '
            f"{duration_sum.get(agent, 0)}"
        )
        lines.append(
            f'agent_relay_turn_duration_ms_count{{agent="{_esc(agent)}"}} '
            f"{duration_count.get(agent, 0)}"
        )

    # ---- Turn outcomes ----
    outcomes = _per_agent_outcomes(metrics)
    lines.append("# HELP agent_relay_turns_total Cumulative turn count by agent and outcome")
    lines.append("# TYPE agent_relay_turns_total counter")
    for agent in sorted(outcomes):
        for result, count in sorted(outcomes[agent].items()):
            lines.append(
                f'agent_relay_turns_total{{agent="{_esc(agent)}",result="{result}"}} {count}'
            )

    # ---- Session counts ----
    statuses = _session_status_counts(metrics)
    active = sum(
        v for k, v in statuses.items() if k in ("active", "launching", "awaiting_resume", "paused")
    )
    lines.append("# HELP agent_relay_session_active Number of sessions in a live status")
    lines.append("# TYPE agent_relay_session_active gauge")
    lines.append(f"agent_relay_session_active {active}")

    lines.append("# HELP agent_relay_sessions_total Sessions by current status")
    lines.append("# TYPE agent_relay_sessions_total gauge")
    for status, count in sorted(statuses.items()):
        lines.append(f'agent_relay_sessions_total{{status="{_esc(status)}"}} {count}')

    return "\n".join(lines) + "\n"


def serve_prometheus(
    repo_root: Path,
    host: str,
    port: int,
    *,
    refresh_interval: float = 5.0,
    extractor: Callable[[Path], CrossSessionMetrics] | None = None,
    server_factory: Callable[..., ThreadingHTTPServer] | None = None,
) -> int:
    """Run the Prometheus scrape endpoint and HTML dashboard until Ctrl-C.

    ``extractor`` and ``server_factory`` exist so tests can swap them out.
    """
    extract = extractor or extract_cross_session_metrics
    ttl = max(0.0, float(refresh_interval))
    snapshot_cache: _Cache[CrossSessionMetrics] = _Cache(
        loader=lambda: extract(repo_root), ttl_seconds=ttl
    )
    prom_cache: _Cache[str] = _Cache(
        loader=lambda: render_prometheus_text(snapshot_cache.get()),
        ttl_seconds=ttl,
    )

    def load_dashboard_metrics(filter: MetricsFilter) -> tuple[CrossSessionMetrics, datetime]:
        # Filter is applied at extraction time when non-identity. The cached
        # unfiltered snapshot stays warm for the common case ("/").
        if filter.is_identity:
            return snapshot_cache.get_with_loaded_at()
        return extract(repo_root, filter=filter), datetime.now(UTC)

    def render_html(filter: MetricsFilter, errors: tuple[str, ...]) -> str:
        metrics, generated_at = load_dashboard_metrics(filter)
        return render_dashboard_html(
            metrics,
            filter=filter,
            filter_errors=errors,
            generated_at=generated_at,
        )

    def render_dashboard_data(filter: MetricsFilter, errors: tuple[str, ...]) -> str:
        metrics, generated_at = load_dashboard_metrics(filter)
        payload = render_dashboard_update_payload(
            metrics,
            filter_errors=errors,
            generated_at=generated_at,
        )
        return json.dumps(payload, separators=(",", ":"))

    Handler = _make_handler(prom_cache, render_html, render_dashboard_data)
    factory = server_factory or ThreadingHTTPServer
    server = factory((host, port), Handler)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
        server.server_close()
        return 130
    server.server_close()
    return 0


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


class _Cache[T]:
    """Tiny TTL cache, threadsafe. Used for both the Prom text body and the
    underlying metrics snapshot the dashboard shares."""

    def __init__(self, *, loader: Callable[[], T], ttl_seconds: float) -> None:
        self._loader = loader
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._value: T | None = None
        self._expires_at = 0.0
        self._loaded_at: datetime | None = None

    def get(self) -> T:
        return self.get_with_loaded_at()[0]

    def get_with_loaded_at(self) -> tuple[T, datetime]:
        with self._lock:
            now = time.monotonic()
            if self._value is None or now >= self._expires_at:
                self._value = self._loader()
                self._expires_at = now + self._ttl
                self._loaded_at = datetime.now(UTC)
            assert self._loaded_at is not None
            return self._value, self._loaded_at


# Backwards-compatible alias for any external callers.
_MetricsCache = _Cache


def _make_handler(
    prom_cache: _Cache[str],
    render_html: Callable[[MetricsFilter, tuple[str, ...]], str],
    render_dashboard_data: Callable[[MetricsFilter, tuple[str, ...]], str],
) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            raw_path, _, query = self.path.partition("?")
            if raw_path == "/metrics":
                self._send(prom_cache.get(), _CONTENT_TYPE)
                return
            if raw_path in ("/", "/dashboard"):
                filter, errors = parse_filter_from_query(query)
                self._send(render_html(filter, errors), "text/html; charset=utf-8")
                return
            if raw_path == "/dashboard/data":
                filter, errors = parse_filter_from_query(query)
                self._send(render_dashboard_data(filter, errors), "application/json; charset=utf-8")
                return
            self.send_error(HTTPStatus.NOT_FOUND, "use /, /dashboard, /dashboard/data, or /metrics")

        def _send(self, payload: str, content_type: str) -> None:
            try:
                body = payload.encode("utf-8")
            except Exception as exc:  # noqa: BLE001
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return  # silence default access log

    return _Handler


def _esc(value: str) -> str:
    """Escape a label value per the Prometheus text format spec."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _fmt_float(value: float) -> str:
    # Avoid scientific notation; Prometheus accepts decimals fine.
    return f"{value:.6f}".rstrip("0").rstrip(".") or "0"


def _token_directions(usage: TokenUsage):
    yield "input", usage.input
    yield "output", usage.output
    yield "cache_read", usage.cache_read
    yield "cache_creation", usage.cache_creation


def _per_agent_durations(
    metrics: CrossSessionMetrics,
) -> tuple[dict[str, int], dict[str, int]]:
    sums: dict[str, int] = {}
    counts: dict[str, int] = {}
    for s in metrics.sessions:
        for t in s.turns:
            counts[t.agent] = counts.get(t.agent, 0) + 1
            if t.duration_ms is not None:
                sums[t.agent] = sums.get(t.agent, 0) + t.duration_ms
    return sums, counts


def _per_agent_outcomes(metrics: CrossSessionMetrics) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for s in metrics.sessions:
        for t in s.turns:
            bucket = out.setdefault(t.agent, {})
            key = "success" if t.succeeded else "error"
            bucket[key] = bucket.get(key, 0) + 1
    return out


def _session_status_counts(metrics: CrossSessionMetrics) -> dict[str, int]:
    out: dict[str, int] = {}
    for s in metrics.sessions:
        status = s.current_status or "unknown"
        out[status] = out.get(status, 0) + 1
    return out


__all__ = ["render_prometheus_text", "serve_prometheus"]
