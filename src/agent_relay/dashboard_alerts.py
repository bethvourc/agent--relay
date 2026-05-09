"""Phase C — alert evaluation, banner, and full-page list for the dashboard.

Alerts are computed live from a metrics snapshot. We do not persist
firings — the dashboard reflects the current threshold state, not a
log. (Persistence is a v3 follow-up.)

Public surface:

* :func:`evaluate_alerts_for_view` — walk a CrossSessionMetrics through
  the user's ``alerts.toml`` config and return all currently-firing
  alerts, ordered by severity then session id.
* :func:`render_alert_banner_html` — inline strip rendered between the
  filter bar and the totals card. Hidden when no alerts.
* :func:`render_alerts_page_html` — full ``/alerts`` page.
* :func:`render_alerts_payload` — JSON shape for soft refresh.
* :class:`AlertConfigCache` — mtime-aware cache around
  :func:`alerts.load_alert_config` so the server picks up edits to
  ``alerts.toml`` without a restart.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from pathlib import Path

from agent_relay.alerts import (
    Alert,
    AlertConfig,
    alerts_config_path,
    evaluate_session,
    evaluate_turn,
    load_alert_config,
)
from agent_relay.dashboard import (
    _html_head,
    _normalize_generated_at,
    _render_region,
)
from agent_relay.formatting import fmt_cost, fmt_duration_ms, fmt_int
from agent_relay.metrics import CrossSessionMetrics

# Severity ordering — higher index = more severe. Used for banner color
# selection and list sort.
_SEVERITY_ORDER = {"warning": 1, "critical": 2}

_SEVERITY_COLOR = {
    "warning": "var(--warning)",
    "critical": "var(--error)",
}

_SEVERITY_GLYPH = {
    "warning": "◌",
    "critical": "✖",
}

# Rule → human label and unit-aware formatter for the threshold/observed
# columns. Keeps the alerts page legible without a giant switch in the
# template.
_RULE_LABEL = {
    "cost_per_turn": "cost / turn",
    "cost_per_session": "cost / session",
    "duration_per_turn": "turn duration",
    "tokens_per_turn": "tokens / turn",
    "error_rate": "error rate",
}


def _format_rule_value(rule: str, value: float | int) -> str:
    if rule.startswith("cost_"):
        return fmt_cost(float(value))
    if rule == "duration_per_turn":
        return fmt_duration_ms(int(value))
    if rule == "tokens_per_turn":
        return fmt_int(int(value))
    if rule == "error_rate":
        return f"{float(value) * 100:.0f}%"
    return str(value)


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def evaluate_alerts_for_view(metrics: CrossSessionMetrics, cfg: AlertConfig) -> tuple[Alert, ...]:
    """Walk every session + turn through the alert rules. Returns the
    fired alerts sorted by severity (most severe first), then by session
    id and turn number for stability.
    """
    if cfg.is_empty:
        return ()

    fired: list[Alert] = []
    for session in metrics.sessions:
        fired.extend(evaluate_session(session, cfg))
        for turn in session.turns:
            fired.extend(evaluate_turn(turn, session, cfg))

    fired.sort(
        key=lambda a: (
            -_SEVERITY_ORDER.get(a.severity, 0),
            a.session_id,
            a.turn_number or 0,
            a.rule,
        )
    )
    return tuple(fired)


def highest_severity(alerts: tuple[Alert, ...]) -> str | None:
    """Return ``critical`` if any alert is critical, else ``warning`` if
    any alert is warning, else ``None``."""
    if not alerts:
        return None
    return max(alerts, key=lambda a: _SEVERITY_ORDER.get(a.severity, 0)).severity


def _format_severity_counts(alerts: tuple[Alert, ...]) -> str:
    counts: dict[str, int] = {}
    for alert in alerts:
        counts[alert.severity] = counts.get(alert.severity, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: -_SEVERITY_ORDER.get(kv[0], 0))
    return ", ".join(f"{count} {severity}" for severity, count in ordered)


# ---------------------------------------------------------------------------
# Config cache (mtime-aware, threadsafe)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _CacheEntry:
    cfg: AlertConfig
    mtime: float


class AlertConfigCache:
    """Re-reads ``alerts.toml`` whenever its mtime changes. Lets users
    edit thresholds while ``metrics-serve`` is running and see the new
    rules apply on the next render."""

    def __init__(self, repo_root: Path) -> None:
        self._repo_root = repo_root
        self._lock = threading.Lock()
        self._entry: _CacheEntry | None = None

    def get(self) -> AlertConfig:
        path = alerts_config_path(self._repo_root)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        with self._lock:
            if self._entry is None or self._entry.mtime != mtime:
                self._entry = _CacheEntry(
                    cfg=load_alert_config(self._repo_root),
                    mtime=mtime,
                )
            return self._entry.cfg


# ---------------------------------------------------------------------------
# Rendering — banner
# ---------------------------------------------------------------------------


def render_alert_banner_html(
    alerts: tuple[Alert, ...],
    *,
    filtered: bool = False,
) -> str:
    """One-line summary with a link to ``/alerts``. Empty string when no
    alerts, so the surrounding region collapses cleanly."""
    if not alerts:
        return ""
    sev = highest_severity(alerts) or "warning"
    glyph = _SEVERITY_GLYPH.get(sev, "◌")
    color = _SEVERITY_COLOR.get(sev, "var(--warning)")
    scope = "in current view" if filtered else "across all sessions"
    return f"""\
<a class="alert-banner alert-{escape(sev)}" href="/alerts" style="--alert-color: {color};">
  <span class="alert-glyph">{glyph}</span>
  <span class="alert-count">{escape(_format_severity_counts(alerts))}</span>
  <span class="alert-scope muted small">{escape(scope)}</span>
  <span class="alert-arrow">›</span>
</a>"""


# ---------------------------------------------------------------------------
# Rendering — full /alerts page
# ---------------------------------------------------------------------------


def render_alerts_page_html(
    alerts: tuple[Alert, ...],
    cfg: AlertConfig,
    *,
    available_filter_query: str = "",
    generated_at: datetime | str | None = None,
    config_path: Path | None = None,
) -> str:
    generated = _normalize_generated_at(generated_at)
    body = _render_alerts_body(
        alerts,
        cfg,
        available_filter_query=available_filter_query,
        generated_at=generated,
        config_path=config_path,
    )
    head = _html_head(title="agent-relay · alerts", auto_refresh=False)
    return f"<!doctype html>\n{head}\n<body>\n{body}\n</body>\n</html>\n"


def _render_alerts_body(
    alerts: tuple[Alert, ...],
    cfg: AlertConfig,
    *,
    available_filter_query: str,
    generated_at: dict[str, str],
    config_path: Path | None,
) -> str:
    qs = f"?{available_filter_query}" if available_filter_query else ""
    breadcrumb = f"""\
<nav class="breadcrumb" aria-label="breadcrumb">
  <a href="/{escape(qs)}">← dashboard</a>
  <span class="sep">/</span>
  <span>alerts</span>
</nav>"""
    return "\n".join(
        [
            '<main class="page">',
            _render_alerts_header(alerts, generated_at),
            breadcrumb,
            _render_region("tuning-hint", _render_tuning_hint(alerts, cfg)),
            _render_region("alerts-list", _render_alerts_list(alerts)),
            _render_alerts_config_card(cfg, config_path),
            _render_alerts_history_card(),
            _render_alerts_footer(),
            "</main>",
        ]
    )


def _render_alerts_header(alerts: tuple[Alert, ...], generated_at: dict[str, str]) -> str:
    sev = highest_severity(alerts)
    count = len(alerts)
    badge = ""
    if sev:
        color = _SEVERITY_COLOR.get(sev, "var(--warning)")
        badge = (
            f'<span class="status" style="color: {color}">'
            f"{_SEVERITY_GLYPH.get(sev, '◌')} {escape(sev)}</span>"
        )
    return f"""\
<header class="topbar">
  <div class="brand-mark">
    <span class="led">●</span>
    <span class="brand-name">Agent Relay</span>
    <span class="muted">alerts</span>
  </div>
  <div class="header-controls">
    <span class="muted small">
      {count} active · {badge}
      · rendered {escape(generated_at["label"])}
    </span>
  </div>
</header>"""


def _render_alerts_list(alerts: tuple[Alert, ...]) -> str:
    if not alerts:
        return """\
<section class="card">
  <h4>active alerts</h4>
  <p class="muted">no alerts firing. all thresholds within configured bounds.</p>
</section>"""
    rows = "\n      ".join(_render_alert_row(a) for a in alerts)
    return f"""\
<section class="card">
  <h4>active alerts</h4>
  <table class="data alerts-table">
    <thead>
      <tr>
        <th>severity</th>
        <th>rule</th>
        <th class=num>observed</th>
        <th class=num>threshold</th>
        <th>session</th>
        <th>turn</th>
        <th>message</th>
      </tr>
    </thead>
    <tbody>
      {rows}
    </tbody>
  </table>
</section>"""


def _render_alert_row(alert: Alert) -> str:
    sev = alert.severity
    color = _SEVERITY_COLOR.get(sev, "var(--warning)")
    glyph = _SEVERITY_GLYPH.get(sev, "◌")
    rule_label = _RULE_LABEL.get(alert.rule, alert.rule)
    observed = _format_rule_value(alert.rule, alert.observed)
    threshold = _format_rule_value(alert.rule, alert.threshold)
    turn = "—" if alert.turn_number is None else str(alert.turn_number)
    return (
        "<tr>"
        f'<td><span class="status" style="color: {color}">{glyph} {escape(sev)}</span></td>'
        f"<td>{escape(rule_label)}</td>"
        f"<td class=num>{escape(observed)}</td>"
        f"<td class=num>{escape(threshold)}</td>"
        f'<td class="brand mono">{escape(alert.session_id)}</td>'
        f'<td class="muted mono">{turn}</td>'
        f'<td class="muted">{escape(alert.message)}</td>'
        "</tr>"
    )


def _render_alerts_config_card(cfg: AlertConfig, config_path: Path | None) -> str:
    rows: list[str] = []

    def row(label: str, value: str | None) -> None:
        rows.append(
            f'<tr><td class="muted">{escape(label)}</td>'
            f"<td>{escape(value) if value else '<span class="muted">—</span>'}</td></tr>"
        )

    row("cost / turn (USD)", fmt_cost(cfg.cost_per_turn_usd) if cfg.cost_per_turn_usd else None)
    row(
        "cost / session (USD)",
        fmt_cost(cfg.cost_per_session_usd) if cfg.cost_per_session_usd else None,
    )
    row(
        "turn duration",
        fmt_duration_ms(cfg.duration_per_turn_ms) if cfg.duration_per_turn_ms else None,
    )
    row(
        "tokens / turn",
        fmt_int(cfg.tokens_per_turn) if cfg.tokens_per_turn else None,
    )
    row(
        "error rate",
        f"{cfg.error_rate_threshold * 100:.0f}%" if cfg.error_rate_threshold else None,
    )

    body = "\n      ".join(rows)
    path_line = (
        f'<p class="muted small">config: <code class="path">{escape(str(config_path))}</code></p>'
        if config_path
        else ""
    )
    return f"""\
<section class="card">
  <h4>thresholds</h4>
  <table class="data thresholds">
    <tbody>
      {body}
    </tbody>
  </table>
  {path_line}
</section>"""


def _render_alerts_history_card() -> str:
    return """\
<section class="card">
  <h4>looking for history?</h4>
  <p class="muted">
    alerts are evaluated live and not stored locally. for trend analysis, history,
    or paging, scrape <code class="path">/metrics</code> with prometheus, or pipe
    <code class="path">agent-relay metrics-tail --webhook URL</code> to your log
    aggregator. each alert firing is emitted as
    <code class="path">{"kind": "metrics.alert", ...}</code> jsonl.
  </p>
</section>"""


def _render_tuning_hint(alerts: tuple[Alert, ...], cfg: AlertConfig) -> str:
    _ = cfg
    if len(alerts) < 10:
        return ""

    by_rule: dict[str, float | int] = {}
    for alert in alerts:
        prev = by_rule.get(alert.rule)
        if prev is None or float(alert.observed) > float(prev):
            by_rule[alert.rule] = alert.observed

    rows = "\n      ".join(
        f"<tr><td class='muted'>{escape(_RULE_LABEL.get(rule, rule))}</td>"
        f"<td class='num'>{escape(_format_rule_value(rule, value))}</td></tr>"
        for rule, value in sorted(by_rule.items())
    )
    return f"""\
<section class="card tuning-hint">
  <h4>too noisy?</h4>
  <p class="muted">
    {len(alerts)} alerts firing across {len(by_rule)} rules. consider raising your
    thresholds in <code class="path">.agent-relay/config/alerts.toml</code>. highest
    observed values from current alerts:
  </p>
  <table class="data thresholds">
    <tbody>
      {rows}
    </tbody>
  </table>
</section>"""


def _render_alerts_footer() -> str:
    return """\
<footer class="muted small">
  agent-relay metrics-serve · alerts evaluated live from the current metrics snapshot ·
  edit <code class="path">.agent-relay/config/alerts.toml</code> to tune thresholds
</footer>"""


# ---------------------------------------------------------------------------
# JSON payload (soft refresh)
# ---------------------------------------------------------------------------


def render_alerts_payload(
    alerts: tuple[Alert, ...],
    *,
    cfg: AlertConfig | None = None,
    generated_at: datetime | str | None = None,
) -> dict[str, object]:
    generated = _normalize_generated_at(generated_at or datetime.now(UTC))
    if cfg is None:
        cfg = AlertConfig()
    return {
        "generatedAt": generated["iso"],
        "renderedAt": generated["label"],
        "regions": {
            "tuning-hint": _render_tuning_hint(alerts, cfg),
            "alerts-list": _render_alerts_list(alerts),
        },
    }
