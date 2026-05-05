"""Threshold-based alerts on metrics.

Configured via ``.agent-relay/config/alerts.toml`` (all keys optional —
nothing fires when no config file is present, so users opt in).

Alert rules:
* ``cost_per_turn_usd`` — fires per-turn when ``cost_usd`` exceeds the
  threshold.
* ``cost_per_session_usd`` — fires per-session when running cost exceeds
  the threshold.
* ``duration_per_turn_ms`` — fires per-turn for slow turns.
* ``tokens_per_turn`` — fires per-turn for token-heavy turns.
* ``error_rate_threshold`` — fires per-session once turn_count >= 5 and
  failure ratio exceeds the threshold (0..1).

Each fired alert is emitted to stderr (human-readable) AND as a JSON
line on stdout with kind ``metrics.alert``, so alerts ride the same
JSONL/webhook channel the tail exporter uses.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from agent_relay.layout import relay_root
from agent_relay.metrics import SessionMetrics, TurnMetrics


_CONFIG_RELATIVE_PATH = Path("config") / "alerts.toml"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AlertConfig:
    cost_per_turn_usd: float | None = None
    cost_per_session_usd: float | None = None
    duration_per_turn_ms: int | None = None
    tokens_per_turn: int | None = None
    error_rate_threshold: float | None = None
    error_rate_min_turns: int = 5

    @property
    def is_empty(self) -> bool:
        return all(
            v is None
            for v in (
                self.cost_per_turn_usd,
                self.cost_per_session_usd,
                self.duration_per_turn_ms,
                self.tokens_per_turn,
                self.error_rate_threshold,
            )
        )


@dataclass(frozen=True, slots=True)
class Alert:
    rule: str
    severity: str
    session_id: str
    turn_number: int | None
    threshold: float | int
    observed: float | int
    message: str
    timestamp: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "session_id": self.session_id,
            "turn_number": self.turn_number,
            "threshold": self.threshold,
            "observed": self.observed,
            "message": self.message,
            "timestamp": self.timestamp,
        }

    def to_jsonl_line(self) -> str:
        return json.dumps({"kind": "metrics.alert", **self.to_dict()})


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def alerts_config_path(repo_root: Path) -> Path:
    return relay_root(repo_root) / _CONFIG_RELATIVE_PATH


def load_alert_config(repo_root: Path) -> AlertConfig:
    """Load ``alerts.toml`` if present, else return an all-None default."""
    path = alerts_config_path(repo_root)
    if not path.exists():
        return AlertConfig()
    return _load_alert_config_from_path(path)


def _load_alert_config_from_path(path: Path) -> AlertConfig:
    import tomllib

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return AlertConfig()
    return AlertConfig(
        cost_per_turn_usd=_opt_float(data.get("cost_per_turn_usd")),
        cost_per_session_usd=_opt_float(data.get("cost_per_session_usd")),
        duration_per_turn_ms=_opt_int(data.get("duration_per_turn_ms")),
        tokens_per_turn=_opt_int(data.get("tokens_per_turn")),
        error_rate_threshold=_opt_float(data.get("error_rate_threshold")),
        error_rate_min_turns=_opt_int(data.get("error_rate_min_turns")) or 5,
    )


def _opt_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _opt_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


# ---------------------------------------------------------------------------
# Evaluators
# ---------------------------------------------------------------------------


def evaluate_turn(
    turn: TurnMetrics, session: SessionMetrics, cfg: AlertConfig
) -> list[Alert]:
    if cfg.is_empty:
        return []

    out: list[Alert] = []
    ts = _now_iso()

    if cfg.cost_per_turn_usd is not None and turn.cost_usd is not None:
        if turn.cost_usd > cfg.cost_per_turn_usd:
            out.append(
                Alert(
                    rule="cost_per_turn",
                    severity=_severity_ratio(turn.cost_usd, cfg.cost_per_turn_usd),
                    session_id=session.session_id,
                    turn_number=turn.turn_number,
                    threshold=cfg.cost_per_turn_usd,
                    observed=round(turn.cost_usd, 6),
                    message=(
                        f"turn {turn.turn_number} cost ${turn.cost_usd:.4f} "
                        f"exceeds threshold ${cfg.cost_per_turn_usd:.4f}"
                    ),
                    timestamp=ts,
                )
            )

    if cfg.duration_per_turn_ms is not None and turn.duration_ms is not None:
        if turn.duration_ms > cfg.duration_per_turn_ms:
            out.append(
                Alert(
                    rule="duration_per_turn",
                    severity=_severity_ratio(
                        turn.duration_ms, cfg.duration_per_turn_ms
                    ),
                    session_id=session.session_id,
                    turn_number=turn.turn_number,
                    threshold=cfg.duration_per_turn_ms,
                    observed=turn.duration_ms,
                    message=(
                        f"turn {turn.turn_number} took {turn.duration_ms}ms "
                        f"(threshold {cfg.duration_per_turn_ms}ms)"
                    ),
                    timestamp=ts,
                )
            )

    if cfg.tokens_per_turn is not None and turn.tokens.total is not None:
        total = turn.tokens.total
        if total > cfg.tokens_per_turn:
            out.append(
                Alert(
                    rule="tokens_per_turn",
                    severity=_severity_ratio(total, cfg.tokens_per_turn),
                    session_id=session.session_id,
                    turn_number=turn.turn_number,
                    threshold=cfg.tokens_per_turn,
                    observed=total,
                    message=(
                        f"turn {turn.turn_number} used {total:,} tokens "
                        f"(threshold {cfg.tokens_per_turn:,})"
                    ),
                    timestamp=ts,
                )
            )

    out.extend(evaluate_session(session, cfg))
    return out


def evaluate_session(session: SessionMetrics, cfg: AlertConfig) -> list[Alert]:
    if cfg.is_empty:
        return []

    out: list[Alert] = []
    ts = _now_iso()

    if cfg.cost_per_session_usd is not None and session.total_cost_usd is not None:
        if session.total_cost_usd > cfg.cost_per_session_usd:
            out.append(
                Alert(
                    rule="cost_per_session",
                    severity=_severity_ratio(
                        session.total_cost_usd, cfg.cost_per_session_usd
                    ),
                    session_id=session.session_id,
                    turn_number=None,
                    threshold=cfg.cost_per_session_usd,
                    observed=round(session.total_cost_usd, 6),
                    message=(
                        f"session cost ${session.total_cost_usd:.4f} exceeds "
                        f"threshold ${cfg.cost_per_session_usd:.4f}"
                    ),
                    timestamp=ts,
                )
            )

    if (
        cfg.error_rate_threshold is not None
        and session.turn_count >= cfg.error_rate_min_turns
    ):
        failures = session.turn_count - session.successful_turns
        rate = failures / session.turn_count if session.turn_count else 0.0
        if rate > cfg.error_rate_threshold:
            out.append(
                Alert(
                    rule="error_rate",
                    severity=_severity_ratio(rate, cfg.error_rate_threshold),
                    session_id=session.session_id,
                    turn_number=None,
                    threshold=cfg.error_rate_threshold,
                    observed=round(rate, 4),
                    message=(
                        f"session error rate {rate:.0%} exceeds threshold "
                        f"{cfg.error_rate_threshold:.0%} ({failures}/{session.turn_count} turns failed)"
                    ),
                    timestamp=ts,
                )
            )

    return out


def _severity_ratio(observed: float, threshold: float) -> str:
    if threshold <= 0:
        return "warning"
    return "critical" if observed >= 2 * threshold else "warning"


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    )


# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------


def emit_alert(
    alert: Alert,
    *,
    output: TextIO | None = None,
    stderr: TextIO | None = None,
) -> None:
    """Write a JSONL line to ``output`` (default stdout) and a colored
    human-readable line to ``stderr``."""
    out = output if output is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr

    out.write(alert.to_jsonl_line() + "\n")
    out.flush()

    color = "\033[31m" if alert.severity == "critical" else "\033[33m"
    reset = "\033[0m"
    turn_part = f" turn {alert.turn_number}" if alert.turn_number is not None else ""
    err.write(
        f"{color}[{alert.severity}] {alert.rule}{reset} "
        f"session {alert.session_id}{turn_part}: {alert.message}\n"
    )
    err.flush()


def emit_alerts(
    alerts: list[Alert],
    *,
    output: TextIO | None = None,
    stderr: TextIO | None = None,
) -> None:
    for alert in alerts:
        emit_alert(alert, output=output, stderr=stderr)


__all__ = [
    "Alert",
    "AlertConfig",
    "alerts_config_path",
    "load_alert_config",
    "evaluate_turn",
    "evaluate_session",
    "emit_alert",
    "emit_alerts",
]
