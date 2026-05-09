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
from typing import IO

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
from agent_relay.sse import DEFAULT_HEARTBEAT_SECONDS, DEFAULT_TICK_SECONDS, stream_updates
from agent_relay.storage import is_session
from agent_relay.turn_artifacts import load_turn_artifacts
from agent_relay.watch import WatchSource

_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
_HTML_TYPE = "text/html; charset=utf-8"
_JSON_TYPE = "application/json; charset=utf-8"
_SSE_TYPE = "text/event-stream"
_SESSION_PATH = re.compile(r"^/session/([A-Za-z0-9-]+)(?:/(data))?$")
_TURN_PATH = re.compile(r"^/session/([A-Za-z0-9-]+)/turn/(\d+)(?:/(data))?$")
_ALERTS_PATH = re.compile(r"^/alerts(?:/(data))?$")
_DASHBOARD_EVENTS_PATHS = frozenset({"/events", "/dashboard/events"})
_ALERTS_EVENTS_PATH = "/alerts/events"
_SESSION_EVENTS_PATH = re.compile(r"^/session/([A-Za-z0-9-]+)/events$")
_TURN_EVENTS_PATH = re.compile(r"^/session/([A-Za-z0-9-]+)/turn/(\d+)/events$")
_DEFAULT_SSE_CONNECTION_CAP = 8

# Tightened to what the dashboard actually loads: self-hosted markup,
# inline styles (one <style> block), Google Fonts CSS + woff2 files.
# No remote scripts, no remote XHR/fetch — Phase E SSE will need
# ``connect-src 'self'``, which is already covered.
_CSP_DASHBOARD = (
    "default-src 'self'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'self'"
)
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1"})


def is_loopback_bind(host: str) -> bool:
    """True when ``host`` only accepts connections from the local machine."""
    return host.strip().lower() in _LOOPBACK_HOSTS


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
    dashboard_refresh_interval: float = DEFAULT_TICK_SECONDS,
    sse_heartbeat_interval: float = DEFAULT_HEARTBEAT_SECONDS,
    sse_connection_cap: int = _DEFAULT_SSE_CONNECTION_CAP,
    extractor: Callable[[Path], CrossSessionMetrics] | None = None,
    server_factory: Callable[..., ThreadingHTTPServer] | None = None,
    dashboard_enabled: bool = True,
    allow_remote: bool = False,
    access_log: IO[str] | None = None,
    telemetry: DashboardTelemetry | None = None,
) -> int:
    """Run the Prometheus scrape endpoint and HTML dashboard until Ctrl-C.

    Phase F adds three operability knobs:

    * ``dashboard_enabled=False`` strips the HTML/JSON dashboard surface;
      only ``/metrics`` answers and everything else 404s. Useful for
      Prom-only deployments.
    * ``allow_remote=False`` (default) refuses to bind anything but
      loopback. Pass ``True`` only after the operator has accepted the
      risk of exposing session content on the network.
    * ``access_log`` (e.g. ``sys.stderr``) opt-in JSONL access log.

    ``extractor`` and ``server_factory`` exist so tests can swap them out.
    """
    if not allow_remote and not is_loopback_bind(host):
        raise RuntimeError(
            f"refusing to bind {host}: dashboard exposes session content. "
            "Pass allow_remote=True (CLI: --allow-remote) to override."
        )

    extract = extractor or extract_cross_session_metrics
    ttl = max(0.0, float(refresh_interval))
    snapshot_cache: _Cache[CrossSessionMetrics] = _Cache(
        loader=lambda: extract(repo_root), ttl_seconds=ttl
    )
    metrics_telemetry = telemetry or DashboardTelemetry()
    prom_cache: _Cache[str] = _Cache(
        loader=lambda: _render_prometheus_with_self_metrics(
            snapshot_cache.get(), metrics_telemetry
        ),
        ttl_seconds=min(ttl, 1.0),
    )

    alert_config = AlertConfigCache(repo_root)
    sse_stop_event = threading.Event()

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

    def render_dashboard_payload(
        filter: MetricsFilter, errors: tuple[str, ...]
    ) -> dict[str, object]:
        metrics, generated_at = load_dashboard_metrics(filter)
        alerts = evaluate_active_alerts(metrics)
        return render_dashboard_update_payload(
            metrics,
            filter=filter,
            filter_errors=errors,
            alerts_banner_html=render_alert_banner_html(alerts, filtered=not filter.is_identity),
            generated_at=generated_at,
        )

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

    def render_alerts_data_payload(query: str) -> dict[str, object]:
        filter, _errors = parse_filter_from_query(query)
        metrics, generated_at = load_dashboard_metrics(filter)
        alerts = evaluate_active_alerts(metrics)
        return render_alerts_payload(alerts, cfg=alert_config.get(), generated_at=generated_at)

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

    def render_session_data_page(
        session_id: str, query: str
    ) -> tuple[HTTPStatus, dict[str, object] | str]:
        _ = query
        if not is_session(repo_root, session_id):
            return HTTPStatus.NOT_FOUND, render_session_not_found_html(session_id)
        metrics, integrity, objective, generated_at = load_session_view_parts(session_id)
        return HTTPStatus.OK, render_session_detail_payload(
            session_id=session_id,
            metrics=metrics,
            integrity=integrity,
            objective=objective,
            generated_at=generated_at,
        )

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
        session_id: str, turn_number: int, query: str
    ) -> tuple[HTTPStatus, dict[str, object] | str]:
        _ = query
        if (
            not is_session(repo_root, session_id)
            or not turn_dir(repo_root, session_id, turn_number).exists()
        ):
            return HTTPStatus.NOT_FOUND, render_turn_not_found_html(session_id, turn_number)
        metrics = extract_turn_metrics(repo_root, session_id, turn_number)
        artifacts = load_turn_artifacts(repo_root, session_id, turn_number)
        return HTTPStatus.OK, render_turn_detail_payload(
            artifacts=artifacts,
            metrics=metrics,
            session_id=session_id,
            generated_at=datetime.now(UTC),
        )

    Handler = _make_handler(
        prom_cache,
        render_html,
        render_dashboard_payload,
        render_session_page,
        render_session_data_page,
        render_turn_page,
        render_turn_data_page,
        render_alerts_html_page,
        render_alerts_data_payload,
        dashboard_enabled=dashboard_enabled,
        access_log=access_log,
        telemetry=metrics_telemetry,
        sse_stop_event=sse_stop_event,
        sse_tick_seconds=dashboard_refresh_interval,
        sse_heartbeat_seconds=sse_heartbeat_interval,
        sse_connection_cap=sse_connection_cap,
    )
    factory = server_factory or ThreadingHTTPServer
    server = factory((host, port), Handler)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sse_stop_event.set()
        server.shutdown()
        server.server_close()
        return 130
    sse_stop_event.set()
    server.server_close()
    return 0


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


class DashboardTelemetry:
    """Self-instrumentation for the dashboard HTTP surface.

    Counters and timing samples render into ``/metrics`` so an operator
    Grafana already pointed at this exporter can graph dashboard health
    without any extra wiring.
    """

    __slots__ = ("_lock", "_requests", "_durations", "_sse_connections")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: dict[tuple[str, int], int] = {}
        self._durations: dict[str, tuple[int, float]] = {}  # path → (count, sum_ms)
        self._sse_connections = 0

    def record_request(self, *, path: str, status: int, duration_ms: float) -> None:
        with self._lock:
            self._requests[(path, status)] = self._requests.get((path, status), 0) + 1
            count, total = self._durations.get(path, (0, 0.0))
            self._durations[path] = (count + 1, total + duration_ms)

    def sse_connected(self) -> None:
        with self._lock:
            self._sse_connections += 1

    def sse_disconnected(self) -> None:
        with self._lock:
            self._sse_connections = max(0, self._sse_connections - 1)

    def render_prometheus_lines(self) -> list[str]:
        with self._lock:
            requests = dict(self._requests)
            durations = dict(self._durations)
            sse = self._sse_connections
        lines: list[str] = []
        lines.append(
            "# HELP agent_relay_dashboard_requests_total Dashboard requests by path and status"
        )
        lines.append("# TYPE agent_relay_dashboard_requests_total counter")
        for (path, status), count in sorted(requests.items()):
            lines.append(
                f"agent_relay_dashboard_requests_total"
                f'{{path="{_esc(path)}",status="{status}"}} {count}'
            )
        lines.append(
            "# HELP agent_relay_dashboard_render_duration_ms Render time per dashboard path"
        )
        lines.append("# TYPE agent_relay_dashboard_render_duration_ms summary")
        for path, (count, total) in sorted(durations.items()):
            lines.append(
                f'agent_relay_dashboard_render_duration_ms_count{{path="{_esc(path)}"}} {count}'
            )
            lines.append(
                f"agent_relay_dashboard_render_duration_ms_sum"
                f'{{path="{_esc(path)}"}} {_fmt_float(total)}'
            )
        lines.append(
            "# HELP agent_relay_dashboard_sse_connections Active SSE connections (0 until Phase E)"
        )
        lines.append("# TYPE agent_relay_dashboard_sse_connections gauge")
        lines.append(f"agent_relay_dashboard_sse_connections {sse}")
        return lines


_PATH_TEMPLATES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^/session/[A-Za-z0-9-]+/turn/\d+/events$"), "/session/{id}/turn/{n}/events"),
    (re.compile(r"^/session/[A-Za-z0-9-]+/turn/\d+/data$"), "/session/{id}/turn/{n}/data"),
    (re.compile(r"^/session/[A-Za-z0-9-]+/turn/\d+$"), "/session/{id}/turn/{n}"),
    (re.compile(r"^/session/[A-Za-z0-9-]+/events$"), "/session/{id}/events"),
    (re.compile(r"^/session/[A-Za-z0-9-]+/data$"), "/session/{id}/data"),
    (re.compile(r"^/session/[A-Za-z0-9-]+$"), "/session/{id}"),
)
_KNOWN_PATHS = frozenset(
    {
        "/",
        "/dashboard",
        "/dashboard/data",
        "/dashboard/events",
        "/events",
        "/alerts",
        "/alerts/data",
        "/alerts/events",
        "/metrics",
    }
)


def _label_path(raw_path: str) -> str:
    """Bucket a request path into a label that won't blow Prom cardinality."""
    if raw_path in _KNOWN_PATHS:
        return raw_path
    for pattern, template in _PATH_TEMPLATES:
        if pattern.match(raw_path):
            return template
    if raw_path.startswith("/session/"):
        return "/session/{id}"
    return "other"


def _write_access_log(
    stream: IO[str], *, path: str, status: int, duration_ms: float, length: int
) -> None:
    record = {
        "ts": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "path": path,
        "status": int(status),
        "ms": round(duration_ms, 2),
        "len": int(length),
    }
    try:
        stream.write(json.dumps(record, separators=(",", ":")) + "\n")
        stream.flush()
    except Exception:  # noqa: BLE001
        pass


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
    render_dashboard_payload: Callable[[MetricsFilter, tuple[str, ...]], dict[str, object]],
    render_session_page: Callable[[str, str], tuple[HTTPStatus, str]],
    render_session_data_page: Callable[[str, str], tuple[HTTPStatus, dict[str, object] | str]],
    render_turn_page: Callable[[str, int, str], tuple[HTTPStatus, str]],
    render_turn_data_page: Callable[[str, int, str], tuple[HTTPStatus, dict[str, object] | str]],
    render_alerts_html_page: Callable[[str], str],
    render_alerts_payload: Callable[[str], dict[str, object]],
    *,
    dashboard_enabled: bool = True,
    access_log: IO[str] | None = None,
    telemetry: DashboardTelemetry | None = None,
    sse_stop_event: threading.Event | None = None,
    sse_tick_seconds: float = DEFAULT_TICK_SECONDS,
    sse_heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
    sse_connection_cap: int = _DEFAULT_SSE_CONNECTION_CAP,
) -> type[BaseHTTPRequestHandler]:
    metrics_telemetry = telemetry or DashboardTelemetry()
    stop_event = sse_stop_event or threading.Event()
    sse_lock = threading.Lock()
    sse_active_connections = 0
    sse_cap = max(1, int(sse_connection_cap))

    def _dashboard_404() -> tuple[HTTPStatus, str, str]:
        return HTTPStatus.NOT_FOUND, "dashboard disabled\n", _CONTENT_TYPE

    def _try_acquire_sse() -> bool:
        nonlocal sse_active_connections
        with sse_lock:
            if sse_active_connections >= sse_cap:
                return False
            sse_active_connections += 1
        metrics_telemetry.sse_connected()
        return True

    def _release_sse() -> None:
        nonlocal sse_active_connections
        with sse_lock:
            sse_active_connections = max(0, sse_active_connections - 1)
        metrics_telemetry.sse_disconnected()

    def _json_payload(payload: dict[str, object]) -> str:
        return json.dumps(payload, separators=(",", ":"))

    def _expect_payload(result: tuple[HTTPStatus, dict[str, object] | str]) -> dict[str, object]:
        status, payload = result
        if status != HTTPStatus.OK or not isinstance(payload, dict):
            raise RuntimeError(f"event payload unavailable: {int(status)}")
        return payload

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            raw_path, _, query = self.path.partition("?")
            started = time.monotonic()
            label = _label_path(raw_path)
            try:
                status, body_len = self._dispatch(raw_path, query)
            except Exception:  # noqa: BLE001
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal server error")
                status = HTTPStatus.INTERNAL_SERVER_ERROR
                body_len = 0
            duration_ms = (time.monotonic() - started) * 1000.0
            metrics_telemetry.record_request(
                path=label, status=int(status), duration_ms=duration_ms
            )
            if access_log is not None:
                _write_access_log(
                    access_log,
                    path=raw_path,
                    status=int(status),
                    duration_ms=duration_ms,
                    length=body_len,
                )

        def _dispatch(self, raw_path: str, query: str) -> tuple[HTTPStatus, int]:
            if raw_path == "/metrics":
                return self._send(prom_cache.get(), _CONTENT_TYPE)

            if not dashboard_enabled:
                status, payload, content_type = _dashboard_404()
                return self._send(payload, content_type, status=status)

            turn_events_match = _TURN_EVENTS_PATH.match(raw_path)
            if turn_events_match:
                session_id, turn_text = turn_events_match.groups()
                turn_number = int(turn_text)
                status, payload = render_turn_data_page(session_id, turn_number, query)
                if status != HTTPStatus.OK:
                    return self._send(str(payload), _HTML_TYPE, status=status)
                assert isinstance(payload, dict)
                return self._send_events(
                    lambda: _expect_payload(render_turn_data_page(session_id, turn_number, query))
                )

            turn_match = _TURN_PATH.match(raw_path)
            if turn_match:
                session_id, turn_text, data_suffix = turn_match.groups()
                turn_number = int(turn_text)
                if turn_number <= 0:
                    return self._send(
                        render_turn_not_found_html(session_id, turn_number),
                        _HTML_TYPE,
                        status=HTTPStatus.NOT_FOUND,
                    )
                if data_suffix:
                    status, payload = render_turn_data_page(session_id, turn_number, query)
                    if status == HTTPStatus.OK:
                        return self._send_json(payload)
                    content_type = _HTML_TYPE
                else:
                    status, payload = render_turn_page(session_id, turn_number, query)
                    content_type = _HTML_TYPE
                return self._send(str(payload), content_type, status=status)
            session_events_match = _SESSION_EVENTS_PATH.match(raw_path)
            if session_events_match:
                session_id = session_events_match.group(1)
                status, payload = render_session_data_page(session_id, query)
                if status != HTTPStatus.OK:
                    return self._send(str(payload), _HTML_TYPE, status=status)
                assert isinstance(payload, dict)
                return self._send_events(
                    lambda: _expect_payload(render_session_data_page(session_id, query))
                )
            session_match = _SESSION_PATH.match(raw_path)
            if session_match:
                session_id, data_suffix = session_match.groups()
                if data_suffix:
                    status, payload = render_session_data_page(session_id, query)
                    if status == HTTPStatus.OK:
                        return self._send_json(payload)
                    content_type = _HTML_TYPE
                else:
                    status, payload = render_session_page(session_id, query)
                    content_type = _HTML_TYPE
                return self._send(str(payload), content_type, status=status)
            if raw_path in ("/", "/dashboard"):
                filter, errors = parse_filter_from_query(query)
                return self._send(render_html(filter, errors), _HTML_TYPE)
            if raw_path == "/dashboard/data":
                filter, errors = parse_filter_from_query(query)
                return self._send_json(render_dashboard_payload(filter, errors))
            if raw_path in _DASHBOARD_EVENTS_PATHS:
                filter, errors = parse_filter_from_query(query)
                return self._send_events(lambda: render_dashboard_payload(filter, errors))
            alerts_match = _ALERTS_PATH.match(raw_path)
            if alerts_match:
                if alerts_match.group(1):
                    return self._send_json(render_alerts_payload(query))
                return self._send(render_alerts_html_page(query), _HTML_TYPE)
            if raw_path == _ALERTS_EVENTS_PATH:
                return self._send_events(lambda: render_alerts_payload(query))
            if raw_path.startswith("/session/"):
                label = raw_path.removeprefix("/session/") or "unknown"
                return self._send(
                    render_session_not_found_html(label),
                    _HTML_TYPE,
                    status=HTTPStatus.NOT_FOUND,
                )
            self.send_error(
                HTTPStatus.NOT_FOUND,
                "use /, /dashboard, /dashboard/data, /alerts, or /metrics",
            )
            return HTTPStatus.NOT_FOUND, 0

        def _send(
            self,
            payload: str,
            content_type: str,
            *,
            status: HTTPStatus = HTTPStatus.OK,
        ) -> tuple[HTTPStatus, int]:
            try:
                body = payload.encode("utf-8")
            except Exception as exc:  # noqa: BLE001
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
                return HTTPStatus.INTERNAL_SERVER_ERROR, 0
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            if content_type == _HTML_TYPE:
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Security-Policy", _CSP_DASHBOARD)
                self.send_header("X-Frame-Options", "DENY")
            elif content_type == _JSON_TYPE:
                self.send_header("Cache-Control", "no-store")
            else:
                # /metrics text — short freshness.
                self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
            return status, len(body)

        def _send_json(
            self,
            payload: dict[str, object] | str,
            *,
            status: HTTPStatus = HTTPStatus.OK,
        ) -> tuple[HTTPStatus, int]:
            text = _json_payload(payload) if isinstance(payload, dict) else payload
            return self._send(text, _JSON_TYPE, status=status)

        def _send_events(
            self,
            build_payload: Callable[[], dict[str, object]],
        ) -> tuple[HTTPStatus, int]:
            if not _try_acquire_sse():
                return self._send_json(
                    {"error": "too many connections"},
                    status=HTTPStatus.SERVICE_UNAVAILABLE,
                )

            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", _SSE_TYPE)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()

            body_len = 0
            try:
                for chunk in stream_updates(
                    build_payload=build_payload,
                    stop_event=stop_event,
                    tick_seconds=sse_tick_seconds,
                    heartbeat_seconds=sse_heartbeat_seconds,
                ):
                    try:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                        break
                    body_len += len(chunk)
            finally:
                _release_sse()
                self.close_connection = True
            return HTTPStatus.OK, body_len

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return  # silence default access log; we have our own opt-in JSONL log

    return _Handler


def _render_prometheus_with_self_metrics(
    metrics: CrossSessionMetrics,
    telemetry: DashboardTelemetry,
) -> str:
    body = render_prometheus_text(metrics).rstrip("\n")
    extras = telemetry.render_prometheus_lines()
    return body + "\n" + "\n".join(extras) + "\n"


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


__all__ = [
    "DashboardTelemetry",
    "is_loopback_bind",
    "render_prometheus_text",
    "serve_prometheus",
]
