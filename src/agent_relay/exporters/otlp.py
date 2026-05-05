"""OTLP exporter — pushes metrics to an OTLP/HTTP-JSON endpoint.

Implements just enough of the OTLP wire format
(https://opentelemetry.io/docs/specs/otlp/#otlphttp) to push counters and
gauges in JSON over HTTP. No ``opentelemetry`` dependency — stdlib only.

If a user needs protobuf or gRPC, point this at an OpenTelemetry
Collector configured with the ``otlphttp`` JSON receiver and let the
collector forward.

Two entry points:

* :func:`export_otlp` — one-shot push of a snapshot.
* :func:`serve_otlp` — runs a polling loop that re-extracts and pushes
  every ``interval_seconds`` until Ctrl-C.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib import error as urllib_error
from urllib import request as urllib_request

from agent_relay.metrics import (
    CrossSessionMetrics,
    TokenUsage,
    extract_cross_session_metrics,
)

_SCOPE_NAME = "agent-relay"
_SCOPE_VERSION = "1"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def render_otlp_payload(
    metrics: CrossSessionMetrics,
    *,
    resource_attrs: Mapping[str, str] | None = None,
    timestamp_ns: int | None = None,
) -> dict[str, Any]:
    """Render cross-session metrics as an OTLP/HTTP-JSON payload."""
    ts = timestamp_ns if timestamp_ns is not None else _now_ns()
    attrs = {"service.name": "agent-relay"}
    if resource_attrs:
        attrs.update(resource_attrs)

    sum_metrics: list[dict[str, Any]] = []
    gauge_metrics: list[dict[str, Any]] = []

    # ---- Tokens (cumulative counter) ----
    token_points: list[dict[str, Any]] = []
    for agent in sorted(metrics.by_agent):
        usage = metrics.by_agent[agent]
        for direction, value in _token_directions(usage):
            if value is None:
                continue
            token_points.append(
                _int_point(
                    ts,
                    value,
                    {"agent": agent, "direction": direction},
                )
            )
    if token_points:
        sum_metrics.append(
            _counter(
                "agent_relay.tokens",
                "Cumulative tokens by agent and direction",
                "1",
                token_points,
            )
        )

    # ---- Cost (cumulative counter, double) ----
    cost_points = [
        _double_point(ts, cost, {"agent": agent})
        for agent, cost in sorted(metrics.cost_by_agent.items())
    ]
    if cost_points:
        sum_metrics.append(
            _counter(
                "agent_relay.cost_usd",
                "Cumulative cost in USD by agent",
                "USD",
                cost_points,
                value_kind="double",
            )
        )

    # ---- Turn duration / count / outcomes ----
    duration_sum, duration_count = _per_agent_durations(metrics)
    outcomes = _per_agent_outcomes(metrics)

    duration_points = [
        _int_point(ts, ms, {"agent": agent})
        for agent, ms in sorted(duration_sum.items())
    ]
    if duration_points:
        sum_metrics.append(
            _counter(
                "agent_relay.turn_duration_ms",
                "Cumulative turn duration in milliseconds by agent",
                "ms",
                duration_points,
            )
        )

    turn_count_points: list[dict[str, Any]] = []
    for agent, results in sorted(outcomes.items()):
        for result, count in sorted(results.items()):
            turn_count_points.append(
                _int_point(ts, count, {"agent": agent, "result": result})
            )
    if turn_count_points:
        sum_metrics.append(
            _counter(
                "agent_relay.turns",
                "Cumulative turn count by agent and outcome",
                "1",
                turn_count_points,
            )
        )

    # ---- Session counts (gauges) ----
    statuses = _session_status_counts(metrics)
    active = sum(
        v for k, v in statuses.items()
        if k in ("active", "launching", "awaiting_resume", "paused")
    )
    gauge_metrics.append(
        _gauge(
            "agent_relay.session.active",
            "Number of sessions in a live status",
            "1",
            [_int_point(ts, active, {})],
        )
    )
    if statuses:
        gauge_metrics.append(
            _gauge(
                "agent_relay.sessions",
                "Sessions by current status",
                "1",
                [
                    _int_point(ts, count, {"status": status})
                    for status, count in sorted(statuses.items())
                ],
            )
        )

    return {
        "resourceMetrics": [
            {
                "resource": {
                    "attributes": [
                        _kv(k, v) for k, v in sorted(attrs.items())
                    ]
                },
                "scopeMetrics": [
                    {
                        "scope": {"name": _SCOPE_NAME, "version": _SCOPE_VERSION},
                        "metrics": sum_metrics + gauge_metrics,
                    }
                ],
            }
        ]
    }


def export_otlp(
    metrics: CrossSessionMetrics,
    *,
    endpoint: str,
    headers: Mapping[str, str] | None = None,
    timeout: float = 10.0,
    resource_attrs: Mapping[str, str] | None = None,
) -> None:
    """One-shot push of ``metrics`` to ``endpoint``. Errors → stderr."""
    payload = render_otlp_payload(metrics, resource_attrs=resource_attrs)
    body = json.dumps(payload).encode("utf-8")
    base_headers = {"Content-Type": "application/json"}
    if headers:
        base_headers.update(headers)
    req = urllib_request.Request(
        endpoint, data=body, headers=base_headers, method="POST"
    )
    try:
        with urllib_request.urlopen(req, timeout=timeout) as resp:
            if not (200 <= resp.status < 300):
                sys.stderr.write(
                    f"otlp delivery failed: HTTP {resp.status} from {endpoint}\n"
                )
    except urllib_error.HTTPError as exc:
        sys.stderr.write(
            f"otlp delivery failed: HTTP {exc.code} from {endpoint}\n"
        )
    except (urllib_error.URLError, TimeoutError, OSError) as exc:
        sys.stderr.write(f"otlp delivery failed: {exc} ({endpoint})\n")


def serve_otlp(
    repo_root: Path,
    *,
    endpoint: str,
    interval_seconds: float = 30.0,
    headers: Mapping[str, str] | None = None,
    resource_attrs: Mapping[str, str] | None = None,
    extractor: Callable[[Path], CrossSessionMetrics] | None = None,
    stop_event: threading.Event | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Push metrics to the OTLP endpoint every ``interval_seconds`` until Ctrl-C.

    ``stop_event`` and ``sleep`` exist so tests can drive the loop
    deterministically.
    """
    extract = extractor or extract_cross_session_metrics
    interval = max(0.05, float(interval_seconds))
    stop = stop_event if stop_event is not None else threading.Event()

    try:
        while not stop.is_set():
            try:
                metrics = extract(repo_root)
            except Exception as exc:  # noqa: BLE001
                sys.stderr.write(f"otlp extract failed: {exc}\n")
            else:
                export_otlp(
                    metrics,
                    endpoint=endpoint,
                    headers=headers,
                    resource_attrs=resource_attrs,
                )
            # Sleep in small slices so stop_event takes effect quickly.
            slept = 0.0
            while slept < interval and not stop.is_set():
                step = min(0.25, interval - slept)
                sleep(step)
                slept += step
    except KeyboardInterrupt:
        return 130
    return 0


# ---------------------------------------------------------------------------
# OTLP shape helpers
# ---------------------------------------------------------------------------


def _kv(key: str, value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": str(value)}}


def _int_point(
    ts_ns: int, value: int, attrs: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "attributes": [_kv(k, v) for k, v in sorted(attrs.items())],
        "timeUnixNano": str(ts_ns),
        "asInt": str(int(value)),
    }


def _double_point(
    ts_ns: int, value: float, attrs: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "attributes": [_kv(k, v) for k, v in sorted(attrs.items())],
        "timeUnixNano": str(ts_ns),
        "asDouble": float(value),
    }


def _counter(
    name: str,
    description: str,
    unit: str,
    points: list[dict[str, Any]],
    *,
    value_kind: str = "int",
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "unit": unit,
        "sum": {
            "dataPoints": points,
            # 2 = AGGREGATION_TEMPORALITY_CUMULATIVE
            "aggregationTemporality": 2,
            "isMonotonic": True,
        },
    }


def _gauge(
    name: str,
    description: str,
    unit: str,
    points: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "unit": unit,
        "gauge": {"dataPoints": points},
    }


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


def _per_agent_outcomes(
    metrics: CrossSessionMetrics,
) -> dict[str, dict[str, int]]:
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


def _now_ns() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1_000_000_000)


__all__ = ["render_otlp_payload", "export_otlp", "serve_otlp"]
