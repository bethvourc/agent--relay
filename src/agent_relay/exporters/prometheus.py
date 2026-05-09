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
import re
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agent_relay.alerts import alerts_config_path
from agent_relay.dashboard import render_dashboard_html, render_dashboard_update_payload
from agent_relay.dashboard_alerts import (
    AlertConfigCache,
    evaluate_alerts_for_view,
    render_alert_banner_html,
    render_alerts_page_html,
    render_alerts_payload,
)
from agent_relay.dashboard_query import parse_filter_from_query
from agent_relay.dashboard_session import (
    render_session_detail_html,
    render_session_detail_payload,
    render_session_not_found_html,
    render_turn_detail_html,
    render_turn_detail_payload,
    render_turn_not_found_html,
)
from agent_relay.integrity import SessionIntegrityReport, inspect_session_integrity
from agent_relay.layout import turn_dir
from agent_relay.metrics import (
    CrossSessionMetrics,
    MetricsFilter,
    SessionMetrics,
    TokenUsage,
    extract_cross_session_metrics,
    extract_session_metrics,
    extract_turn_metrics,
)
from agent_relay.storage import is_session
from agent_relay.turn_artifacts import load_turn_artifacts
from agent_relay.watch import WatchSource

_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
_HTML_TYPE = "text/html; charset=utf-8"
_JSON_TYPE = "application/json; charset=utf-8"
_SESSION_PATH = re.compile(r"^/session/([A-Za-z0-9-]+)(?:/(data))?$")
_TURN_PATH = re.compile(r"^/session/([A-Za-z0-9-]+)/turn/(\d+)(?:/(data))?$")
_ALERTS_PATH = re.compile(r"^/alerts(?:/(data))?$")


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

    alert_config = AlertConfigCache(repo_root)

    def load_dashboard_metrics(filter: MetricsFilter) -> tuple[CrossSessionMetrics, datetime]:
        # Filter is applied at extraction time when non-identity. The cached
        # unfiltered snapshot stays warm for the common case ("/").
        if filter.is_identity:
            return snapshot_cache.get_with_loaded_at()
        return extract(repo_root, filter=filter), datetime.now(UTC)

    def evaluate_active_alerts(metrics: CrossSessionMetrics) -> tuple:
        return evaluate_alerts_for_view(metrics, alert_config.get())

    def render_html(filter: MetricsFilter, errors: tuple[str, ...]) -> str:
        metrics, generated_at = load_dashboard_metrics(filter)
        alerts = evaluate_active_alerts(metrics)
        return render_dashboard_html(
            metrics,
            filter=filter,
            filter_errors=errors,
            alerts_banner_html=render_alert_banner_html(alerts, filtered=not filter.is_identity),
            generated_at=generated_at,
        )

    def render_dashboard_data(filter: MetricsFilter, errors: tuple[str, ...]) -> str:
        metrics, generated_at = load_dashboard_metrics(filter)
        alerts = evaluate_active_alerts(metrics)
        payload = render_dashboard_update_payload(
            metrics,
            filter=filter,
            filter_errors=errors,
            alerts_banner_html=render_alert_banner_html(alerts, filtered=not filter.is_identity),
            generated_at=generated_at,
        )
        return json.dumps(payload, separators=(",", ":"))

    def render_alerts_html_page(query: str) -> str:
        filter, _errors = parse_filter_from_query(query)
        metrics, generated_at = load_dashboard_metrics(filter)
        alerts = evaluate_active_alerts(metrics)
        return render_alerts_page_html(
            alerts,
            alert_config.get(),
            available_filter_query=query,
            generated_at=generated_at,
            config_path=alerts_config_path(repo_root),
        )

    def render_alerts_data(query: str) -> str:
        filter, _errors = parse_filter_from_query(query)
        metrics, generated_at = load_dashboard_metrics(filter)
        alerts = evaluate_active_alerts(metrics)
        payload = render_alerts_payload(alerts, cfg=alert_config.get(), generated_at=generated_at)
        return json.dumps(payload, separators=(",", ":"))

    def load_session_view_parts(
        session_id: str,
    ) -> tuple[SessionMetrics, SessionIntegrityReport, str | None, datetime]:
        metrics = extract_session_metrics(repo_root, session_id)
        generated_at = datetime.now(UTC)
        try:
            integrity = inspect_session_integrity(repo_root, session_id).report
        except Exception as exc:  # noqa: BLE001
            integrity = _integrity_error_report(repo_root, session_id, metrics, exc)
        try:
            objective = WatchSource(repo_root, session_id, follow=False).snapshot().objective
        except Exception:  # noqa: BLE001
            objective = metrics.objective or integrity.objective
        return metrics, integrity, objective, generated_at

    def render_session_page(session_id: str, query: str) -> tuple[HTTPStatus, str]:
        if not is_session(repo_root, session_id):
            return HTTPStatus.NOT_FOUND, render_session_not_found_html(session_id)
        metrics, integrity, objective, generated_at = load_session_view_parts(session_id)
        return HTTPStatus.OK, render_session_detail_html(
            session_id=session_id,
            metrics=metrics,
            integrity=integrity,
            objective=objective,
            available_filter_query=query,
            generated_at=generated_at,
        )

    def render_session_data_page(session_id: str, _query: str) -> tuple[HTTPStatus, str]:
        if not is_session(repo_root, session_id):
            return HTTPStatus.NOT_FOUND, render_session_not_found_html(session_id)
        metrics, integrity, objective, generated_at = load_session_view_parts(session_id)
        payload = render_session_detail_payload(
            session_id=session_id,
            metrics=metrics,
            integrity=integrity,
            objective=objective,
            generated_at=generated_at,
        )
        return HTTPStatus.OK, json.dumps(payload, separators=(",", ":"))

    def render_turn_page(session_id: str, turn_number: int, query: str) -> tuple[HTTPStatus, str]:
        if (
            not is_session(repo_root, session_id)
            or not turn_dir(repo_root, session_id, turn_number).exists()
        ):
            return HTTPStatus.NOT_FOUND, render_turn_not_found_html(session_id, turn_number)
        metrics = extract_turn_metrics(repo_root, session_id, turn_number)
        artifacts = load_turn_artifacts(repo_root, session_id, turn_number)
        return HTTPStatus.OK, render_turn_detail_html(
            artifacts=artifacts,
            metrics=metrics,
            session_id=session_id,
            available_filter_query=query,
            generated_at=datetime.now(UTC),
        )

    def render_turn_data_page(
        session_id: str, turn_number: int, _query: str
    ) -> tuple[HTTPStatus, str]:
        if (
            not is_session(repo_root, session_id)
            or not turn_dir(repo_root, session_id, turn_number).exists()
        ):
            return HTTPStatus.NOT_FOUND, render_turn_not_found_html(session_id, turn_number)
        metrics = extract_turn_metrics(repo_root, session_id, turn_number)
        artifacts = load_turn_artifacts(repo_root, session_id, turn_number)
        payload = render_turn_detail_payload(
            artifacts=artifacts,
            metrics=metrics,
            session_id=session_id,
            generated_at=datetime.now(UTC),
        )
        return HTTPStatus.OK, json.dumps(payload, separators=(",", ":"))

    Handler = _make_handler(
        prom_cache,
        render_html,
        render_dashboard_data,
        render_session_page,
        render_session_data_page,
        render_turn_page,
        render_turn_data_page,
        render_alerts_html_page,
        render_alerts_data,
    )
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


def _integrity_error_report(
    repo_root: Path,
    session_id: str,
    metrics: SessionMetrics,
    exc: Exception,
) -> SessionIntegrityReport:
    message = str(exc) or exc.__class__.__name__
    return SessionIntegrityReport(
        session_id=session_id,
        storage_model="journal_v2",
        repo_root=str(repo_root),
        objective=metrics.objective or message,
        workstream_kind="mixed",
        created_at=metrics.started_at or "",
        updated_at=metrics.updated_at or "",
        initial_agent=metrics.current_agent,
        current_agent=metrics.current_agent,
        current_status="corrupt",
        task_status=None,
        next_action="",
        decisions=tuple(),
        blockers=tuple(),
        research_notes=tuple(),
        implementation_notes=tuple(),
        touched_files=tuple(),
        validation={"status": "not_run", "summary": ""},
        latest_checkpoint_id=None,
        prepared_handoff_id=None,
        latest_launch_id=None,
        last_resume_handoff_id=None,
        handoffs=tuple(),
        checkpoint_ids=tuple(),
        launch_ids=tuple(),
        health="corrupt",
        error=message,
        last_valid_event=None,
        broken_paths=tuple(),
        suggested_repair=("inspect the session journal before mutating this session",),
        alerts=(message,),
    )


def _make_handler(
    prom_cache: _Cache[str],
    render_html: Callable[[MetricsFilter, tuple[str, ...]], str],
    render_dashboard_data: Callable[[MetricsFilter, tuple[str, ...]], str],
    render_session_page: Callable[[str, str], tuple[HTTPStatus, str]],
    render_session_data_page: Callable[[str, str], tuple[HTTPStatus, str]],
    render_turn_page: Callable[[str, int, str], tuple[HTTPStatus, str]],
    render_turn_data_page: Callable[[str, int, str], tuple[HTTPStatus, str]],
    render_alerts_html_page: Callable[[str], str],
    render_alerts_data: Callable[[str], str],
) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            raw_path, _, query = self.path.partition("?")
            if raw_path == "/metrics":
                self._send(prom_cache.get(), _CONTENT_TYPE)
                return
            turn_match = _TURN_PATH.match(raw_path)
            if turn_match:
                session_id, turn_text, data_suffix = turn_match.groups()
                turn_number = int(turn_text)
                if turn_number <= 0:
                    self._send(
                        render_turn_not_found_html(session_id, turn_number),
                        _HTML_TYPE,
                        status=HTTPStatus.NOT_FOUND,
                    )
                    return
                if data_suffix:
                    status, payload = render_turn_data_page(session_id, turn_number, query)
                    content_type = _JSON_TYPE if status == HTTPStatus.OK else _HTML_TYPE
                else:
                    status, payload = render_turn_page(session_id, turn_number, query)
                    content_type = _HTML_TYPE
                self._send(payload, content_type, status=status)
                return
            session_match = _SESSION_PATH.match(raw_path)
            if session_match:
                session_id, data_suffix = session_match.groups()
                if data_suffix:
                    status, payload = render_session_data_page(session_id, query)
                    content_type = _JSON_TYPE if status == HTTPStatus.OK else _HTML_TYPE
                else:
                    status, payload = render_session_page(session_id, query)
                    content_type = _HTML_TYPE
                self._send(payload, content_type, status=status)
                return
            if raw_path in ("/", "/dashboard"):
                filter, errors = parse_filter_from_query(query)
                self._send(render_html(filter, errors), _HTML_TYPE)
                return
            if raw_path == "/dashboard/data":
                filter, errors = parse_filter_from_query(query)
                self._send(render_dashboard_data(filter, errors), _JSON_TYPE)
                return
            alerts_match = _ALERTS_PATH.match(raw_path)
            if alerts_match:
                if alerts_match.group(1):
                    self._send(render_alerts_data(query), _JSON_TYPE)
                else:
                    self._send(render_alerts_html_page(query), _HTML_TYPE)
                return
            if raw_path.startswith("/session/"):
                label = raw_path.removeprefix("/session/") or "unknown"
                self._send(
                    render_session_not_found_html(label),
                    _HTML_TYPE,
                    status=HTTPStatus.NOT_FOUND,
                )
                return
            self.send_error(
                HTTPStatus.NOT_FOUND,
                "use /, /dashboard, /dashboard/data, /alerts, or /metrics",
            )

        def _send(
            self,
            payload: str,
            content_type: str,
            *,
            status: HTTPStatus = HTTPStatus.OK,
        ) -> None:
            try:
                body = payload.encode("utf-8")
            except Exception as exc:  # noqa: BLE001
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
                return
            self.send_response(status)
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
