"""HTML dashboard for ``agent-relay metrics-serve``.

A single self-contained page rendered from :class:`CrossSessionMetrics`
that mirrors the design system (``Agent Relay Design System/``):

* JetBrains Mono everywhere, Inter only for page chrome.
* Two-accent palette: amber ``--brand`` and green ``--signal``.
* Hairline borders, no gradients, no shadows except a faint focus ring.
* Auto-refreshes every 5s via ``<meta http-equiv="refresh">``.

The renderer is intentionally I/O free — it takes a metrics snapshot and
returns a string. The Prometheus HTTP server in
:mod:`agent_relay.exporters.prometheus` mounts it under ``/`` and
``/dashboard``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from html import escape

from agent_relay import tokens as T
from agent_relay.formatting import fmt_cost, fmt_duration_ms, fmt_int
from agent_relay.metrics import CrossSessionMetrics, SessionMetrics, TokenUsage

DASHBOARD_REFRESH_SECONDS = 5

# Status verbs → color role. Mirrors the Rich ``status.*`` theme entries
# but expressed in CSS variables so the HTML stays self-contained.
_STATUS_COLOR = {
    "active": "var(--success)",
    "paused": "var(--warning)",
    "blocked": "var(--error)",
    "completed": "var(--brand)",
    "done": "var(--brand)",
    "handoff_prepared": "var(--agent-codex)",
    "ready_for_handoff": "var(--agent-codex)",
    "launching": "#c97cd6",
    "launch_failed": "var(--error)",
    "awaiting_resume": "var(--brand)",
    "degraded": "var(--warning)",
    "corrupt": "var(--error)",
    "ready": "var(--fg-3)",
    "succeeded": "var(--success)",
    "failed": "var(--error)",
    "interrupted": "var(--warning)",
    "ok": "var(--success)",
}

_STATUS_GLYPH = {
    "active": "●",
    "paused": "◉",
    "blocked": "✖",
    "completed": "✔",
    "done": "✔",
    "handoff_prepared": "⇄",
    "ready_for_handoff": "⇄",
    "launching": "◎",
    "launch_failed": "✖",
    "awaiting_resume": "◌",
    "degraded": "◌",
    "corrupt": "✖",
    "ready": "◌",
    "succeeded": "✔",
    "failed": "✖",
    "interrupted": "◌",
    "ok": "✔",
}


def render_dashboard_html(metrics: CrossSessionMetrics, *, generated_at: str | None = None) -> str:
    """Return a complete HTML document for the metrics dashboard."""
    ts = generated_at or datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    body = _render_body(metrics, generated_at=ts)
    return f"<!doctype html>\n{_HTML_HEAD}\n<body>\n{body}\n</body>\n</html>\n"


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def _render_body(metrics: CrossSessionMetrics, *, generated_at: str) -> str:
    return "\n".join(
        [
            '<main class="page">',
            _render_header(generated_at),
            _render_totals(metrics),
            _render_by_agent(metrics),
            _render_sessions(metrics),
            _render_by_day(metrics),
            _render_footer(),
            "</main>",
        ]
    )


def _render_header(generated_at: str) -> str:
    return f"""\
<header class="topbar">
  <div class="brand-mark">
    <span class="led">●</span>
    <span class="brand-name">Agent Relay</span>
    <span class="muted">metrics</span>
  </div>
  <div class="muted small">refreshed {escape(generated_at)} · auto-refresh {DASHBOARD_REFRESH_SECONDS}s</div>
</header>"""


def _render_totals(metrics: CrossSessionMetrics) -> str:
    cells = [
        ("sessions", str(metrics.session_count)),
        ("tokens", _format_total_tokens(metrics.total_tokens)),
        ("cost", fmt_cost(metrics.total_cost_usd)),
        ("duration", fmt_duration_ms(metrics.total_duration_ms)),
    ]
    items = "\n".join(
        f'  <div class="metric"><span class="label">{escape(label)}</span>'
        f'<span class="value">{escape(value)}</span></div>'
        for label, value in cells
    )
    return f"""\
<section class="card totals">
  <h4>totals</h4>
  <div class="metric-grid">
{items}
  </div>
</section>"""


def _render_by_agent(metrics: CrossSessionMetrics) -> str:
    if not metrics.by_agent:
        return ""
    rows: list[str] = []
    for agent in sorted(metrics.by_agent):
        usage = metrics.by_agent[agent]
        cost = metrics.cost_by_agent.get(agent)
        rows.append(
            "<tr>"
            f"<td>{escape(agent)}</td>"
            f"<td class=num>{escape(fmt_int(usage.total or 0))}</td>"
            f"<td class=num>{escape(fmt_cost(cost))}</td>"
            "</tr>"
        )
    body = "\n      ".join(rows)
    return f"""\
<section class="card">
  <h4>by agent</h4>
  <table class="data">
    <thead><tr><th>agent</th><th class=num>tokens</th><th class=num>cost</th></tr></thead>
    <tbody>
      {body}
    </tbody>
  </table>
</section>"""


def _render_sessions(metrics: CrossSessionMetrics) -> str:
    if not metrics.sessions:
        return """\
<section class="card">
  <h4>sessions</h4>
  <p class="muted">no sessions found.</p>
</section>"""
    rows = [_render_session_row(s) for s in metrics.sessions]
    body = "\n      ".join(rows)
    return f"""\
<section class="card">
  <h4>sessions</h4>
  <table class="data">
    <thead>
      <tr>
        <th>session id</th>
        <th>agent</th>
        <th>status</th>
        <th class=num>turns</th>
        <th class=num>tokens</th>
        <th class=num>cost</th>
        <th class=num>duration</th>
        <th>updated</th>
      </tr>
    </thead>
    <tbody>
      {body}
    </tbody>
  </table>
</section>"""


def _render_session_row(s: SessionMetrics) -> str:
    status_color = _STATUS_COLOR.get(s.current_status, "var(--fg-2)")
    glyph = _STATUS_GLYPH.get(s.current_status, "·")
    updated = s.updated_at or "-"
    if updated and len(updated) >= 16:
        updated = updated[5:16].replace("T", " ")
    return (
        "<tr>"
        f'<td class="brand mono">{escape(s.session_id)}</td>'
        f"<td>{escape(s.current_agent)}</td>"
        f'<td><span class="status" style="color: {status_color}">{glyph} '
        f"{escape(s.current_status)}</span></td>"
        f"<td class=num>{s.turn_count}</td>"
        f"<td class=num>{escape(fmt_int(s.total_tokens.total or 0))}</td>"
        f"<td class=num>{escape(fmt_cost(s.total_cost_usd))}</td>"
        f"<td class=num>{escape(fmt_duration_ms(s.total_duration_ms))}</td>"
        f'<td class="muted">{escape(updated)}</td>'
        "</tr>"
    )


def _render_by_day(metrics: CrossSessionMetrics) -> str:
    if not metrics.by_day:
        return ""
    days = sorted(metrics.by_day.items())
    max_total = max((u.total or 0) for _, u in days) or 1
    rows: list[str] = []
    for day, usage in days:
        total = usage.total or 0
        pct = int(round(100 * total / max_total))
        rows.append(
            "<tr>"
            f'<td class="muted mono">{escape(day)}</td>'
            f'<td class="bar"><span class="bar-fill" style="width:{pct}%"></span></td>'
            f"<td class=num>{escape(fmt_int(total))}</td>"
            "</tr>"
        )
    body = "\n      ".join(rows)
    return f"""\
<section class="card">
  <h4>by day</h4>
  <table class="data bars">
    <tbody>
      {body}
    </tbody>
  </table>
</section>"""


def _render_footer() -> str:
    return f"""\
<footer class="muted small">
  agent-relay metrics-serve · /metrics for Prometheus · brand {escape(T.BRAND)} · signal {escape(T.SIGNAL)}
</footer>"""


def _format_total_tokens(tokens: TokenUsage) -> str:
    return fmt_int(tokens.total or 0)


# ---------------------------------------------------------------------------
# Static head — tokens inlined so the page is self-contained
# ---------------------------------------------------------------------------


_HTML_HEAD = f"""\
<html lang=en data-theme=dark>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{DASHBOARD_REFRESH_SECONDS}">
<title>agent-relay · metrics</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=Inter:wght@400;500;600;700&display=swap">
<style>
:root {{
  --surface-0: {T.SURFACE_0};
  --surface-1: {T.SURFACE_1};
  --surface-2: {T.SURFACE_2};
  --surface-3: {T.SURFACE_3};
  --surface-rule: {T.SURFACE_RULE};

  --fg-1: {T.FG_1};
  --fg-2: {T.FG_2};
  --fg-3: {T.FG_3};
  --fg-4: {T.FG_4};

  --brand: {T.BRAND};
  --brand-dim: {T.BRAND_DIM};
  --brand-glow: rgba(255, 176, 0, 0.18);

  --signal: {T.SIGNAL};
  --signal-dim: {T.SIGNAL_DIM};

  --success: {T.SUCCESS};
  --error: {T.ERROR};
  --warning: {T.WARNING};
  --info: {T.INFO};

  --agent-claude: {T.AGENT_CLAUDE};
  --agent-codex: {T.AGENT_CODEX};
  --agent-gemini: {T.AGENT_GEMINI};

  --font-mono: "JetBrains Mono", "SF Mono", Menlo, Consolas, ui-monospace, monospace;
  --font-ui:   "Inter", -apple-system, "Segoe UI", system-ui, sans-serif;
}}

* {{ box-sizing: border-box; }}
html, body {{
  margin: 0;
  background: var(--surface-0);
  color: var(--fg-1);
  font-family: var(--font-mono);
  font-size: 13px;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
}}

.page {{
  max-width: 1200px;
  margin: 0 auto;
  padding: 24px 20px 48px;
  display: grid;
  gap: 16px;
}}

.topbar {{
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  border-bottom: 1px solid var(--surface-rule);
  padding-bottom: 12px;
}}
.brand-mark {{
  display: inline-flex;
  align-items: baseline;
  gap: 8px;
  font-family: var(--font-ui);
  font-size: 16px;
  font-weight: 600;
  letter-spacing: -0.01em;
}}
.brand-mark .led {{ color: var(--signal); font-size: 10px; }}
.brand-mark .brand-name {{ color: var(--brand); }}

.card {{
  background: var(--surface-2);
  border: 1px solid var(--surface-rule);
  border-radius: 4px;
  padding: 16px 20px;
}}
.card h4 {{
  margin: 0 0 12px;
  font-family: var(--font-mono);
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.04em;
  text-transform: lowercase;
  color: var(--fg-2);
}}

.metric-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 16px;
}}
.metric {{
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 8px 12px;
  background: var(--surface-1);
  border: 1px solid var(--surface-rule);
  border-radius: 2px;
}}
.metric .label {{
  font-size: 11px;
  color: var(--fg-3);
  text-transform: lowercase;
  letter-spacing: 0.04em;
}}
.metric .value {{
  font-size: 18px;
  font-weight: 700;
  color: var(--fg-1);
}}

table.data {{
  width: 100%;
  border-collapse: collapse;
  font-size: 12.5px;
}}
table.data th, table.data td {{
  padding: 6px 10px;
  border-bottom: 1px solid var(--surface-rule);
  text-align: left;
}}
table.data th {{
  color: var(--fg-2);
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: lowercase;
  background: var(--surface-1);
}}
table.data td.num, table.data th.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
table.data td.brand {{ color: var(--brand); font-weight: 600; }}
table.data td.muted {{ color: var(--fg-3); }}
table.data .mono {{ font-family: var(--font-mono); }}
table.data tbody tr:hover {{ background: var(--surface-3); }}

.status {{ font-weight: 600; }}

table.bars td.bar {{
  width: 60%;
  padding: 6px 10px;
}}
.bar-fill {{
  display: block;
  height: 8px;
  background: var(--brand-dim);
  border-radius: 1px;
}}

.muted {{ color: var(--fg-3); }}
.small {{ font-size: 11px; }}

footer {{
  border-top: 1px solid var(--surface-rule);
  padding-top: 12px;
  text-align: center;
}}

p {{ margin: 0; }}
</style>
</head>"""
