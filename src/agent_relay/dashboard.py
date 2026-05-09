"""HTML dashboard for ``agent-relay metrics-serve``.

A single self-contained page rendered from :class:`CrossSessionMetrics`
that mirrors the design system (``Agent Relay Design System/``):

* JetBrains Mono everywhere, Inter only for page chrome.
* Two-accent palette: amber ``--brand`` and green ``--signal``.
* Hairline borders, no gradients, no shadows except a faint focus ring.
* Manual and opt-in live refresh patch metric regions in place.

The renderer is intentionally I/O free — it takes a metrics snapshot and
returns a string. The Prometheus HTTP server in
:mod:`agent_relay.exporters.prometheus` mounts it under ``/`` and
``/dashboard``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from html import escape
from urllib.parse import quote, urlencode

from agent_relay import tokens as T
from agent_relay.formatting import fmt_cost, fmt_duration_ms, fmt_int
from agent_relay.metrics import CrossSessionMetrics, MetricsFilter, SessionMetrics, TokenUsage

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


def render_dashboard_html(
    metrics: CrossSessionMetrics,
    *,
    filter: MetricsFilter | None = None,
    available_agents: tuple[str, ...] | None = None,
    filter_errors: tuple[str, ...] = (),
    alerts_banner_html: str = "",
    generated_at: datetime | str | None = None,
) -> str:
    """Return a complete HTML document for the metrics dashboard.

    ``filter`` echoes the current scope into the filter bar so refresh /
    share-link works. ``available_agents`` populates the agent multi-select;
    if None we derive it from ``metrics.by_agent``. ``filter_errors`` are
    user-friendly messages about query-string values that were ignored.
    ``alerts_banner_html`` is a pre-rendered HTML fragment from
    :func:`dashboard_alerts.render_alert_banner_html`; passed in this way
    so this module stays free of an alerts-evaluation dependency.
    """
    if filter is None:
        filter = MetricsFilter()
    if available_agents is None:
        available_agents = tuple(sorted(metrics.by_agent)) or ("claude", "codex", "gemini")

    generated = _normalize_generated_at(generated_at)
    body = _render_body(
        metrics,
        filter=filter,
        available_agents=available_agents,
        filter_errors=filter_errors,
        alerts_banner_html=alerts_banner_html,
        generated_at=generated,
    )
    return (
        f"<!doctype html>\n{_html_head()}\n"
        '<body data-refresh-endpoint="/dashboard/data">\n'
        f"{body}\n</body>\n</html>\n"
    )


def render_dashboard_update_payload(
    metrics: CrossSessionMetrics,
    *,
    filter: MetricsFilter | None = None,
    filter_errors: tuple[str, ...] = (),
    alerts_banner_html: str = "",
    generated_at: datetime | str | None = None,
) -> dict[str, object]:
    """Return the JSON-serializable dashboard soft-refresh payload."""
    generated = _normalize_generated_at(generated_at)
    if filter is None:
        filter = MetricsFilter()
    return {
        "generatedAt": generated["iso"],
        "renderedAt": generated["label"],
        "regions": _render_dashboard_regions(
            metrics,
            filter=filter,
            filter_errors=filter_errors,
            alerts_banner_html=alerts_banner_html,
        ),
    }


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def _render_body(
    metrics: CrossSessionMetrics,
    *,
    filter: MetricsFilter,
    available_agents: tuple[str, ...],
    filter_errors: tuple[str, ...],
    alerts_banner_html: str,
    generated_at: dict[str, str],
) -> str:
    regions = _render_dashboard_regions(
        metrics,
        filter=filter,
        filter_errors=filter_errors,
        alerts_banner_html=alerts_banner_html,
    )
    return "\n".join(
        [
            '<main class="page">',
            _render_header(generated_at),
            _render_region("filter-errors", regions["filter-errors"]),
            _render_region("alerts", regions["alerts"]),
            _render_filter_bar(filter, available_agents),
            _render_region("totals", regions["totals"]),
            _render_region("by-agent", regions["by-agent"]),
            _render_region("sessions", regions["sessions"]),
            _render_region("by-day", regions["by-day"]),
            _render_footer(),
            "</main>",
        ]
    )


def _normalize_generated_at(generated_at: datetime | str | None) -> dict[str, str]:
    if generated_at is None:
        dt = datetime.now(UTC)
        return _format_generated_at(dt)
    if isinstance(generated_at, datetime):
        dt = generated_at if generated_at.tzinfo else generated_at.replace(tzinfo=UTC)
        return _format_generated_at(dt.astimezone(UTC))
    return {"label": generated_at, "iso": ""}


def _format_generated_at(dt: datetime) -> dict[str, str]:
    return {
        "label": dt.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "iso": dt.isoformat(timespec="seconds").replace("+00:00", "Z"),
    }


def _render_dashboard_regions(
    metrics: CrossSessionMetrics,
    *,
    filter: MetricsFilter,
    filter_errors: tuple[str, ...],
    alerts_banner_html: str = "",
) -> dict[str, str]:
    query = filter_to_query_string(filter)
    filter_query = f"?{query}" if query else ""
    return {
        "filter-errors": _render_filter_errors(filter_errors),
        "alerts": alerts_banner_html,
        "totals": _render_totals(metrics),
        "by-agent": _render_by_agent(metrics),
        "sessions": _render_sessions(metrics, filter_query=filter_query),
        "by-day": _render_by_day(metrics),
    }


def _render_region(name: str, html: str) -> str:
    return f'<div data-dashboard-region="{escape(name)}">\n{html}\n</div>'


def _render_filter_errors(errors: tuple[str, ...]) -> str:
    if not errors:
        return ""
    items = "\n      ".join(f"<li>{escape(e)}</li>" for e in errors)
    return f"""\
<div class="banner banner-warning">
  <strong>filter input ignored</strong>
  <ul>
      {items}
  </ul>
</div>"""


def _render_filter_bar(filter: MetricsFilter, available_agents: tuple[str, ...]) -> str:
    since = filter.since.date().isoformat() if filter.since else ""
    until = filter.until.date().isoformat() if filter.until else ""
    selected_agents = set(filter.agents)
    q = filter.q or ""

    agent_options = "\n        ".join(
        f'<label class="checkbox"><input type="checkbox" name="agent" value="{escape(a)}"'
        f"{' checked' if a in selected_agents else ''}>{escape(a)}</label>"
        for a in available_agents
    )

    return f"""\
<section class="card filter-bar">
  <h4>filter</h4>
  <form method="get" action="/">
    <div class="filter-row">
      <label class="field">
        <span class="label">since</span>
        <input type="date" name="since" value="{escape(since)}">
      </label>
      <label class="field">
        <span class="label">until</span>
        <input type="date" name="until" value="{escape(until)}">
      </label>
      <fieldset class="field">
        <legend class="label">agent</legend>
        {agent_options}
      </fieldset>
      <label class="field grow">
        <span class="label">search</span>
        <input type="text" name="q" value="{escape(q)}" placeholder="session id or objective">
      </label>
      <div class="filter-actions">
        <button type="submit" class="btn-primary">apply</button>
        <a class="btn-secondary" href="/">clear</a>
      </div>
    </div>
  </form>
</section>"""


def _render_header(generated_at: dict[str, str]) -> str:
    data_attr = (
        f' data-generated-at="{escape(generated_at["iso"])}"' if generated_at.get("iso") else ""
    )
    return f"""\
<header class="topbar">
  <div class="brand-mark">
    <span class="led">●</span>
    <span class="brand-name">Agent Relay</span>
    <span class="muted">metrics</span>
  </div>
  <div class="header-controls">
    <span class="muted small">
      rendered <span data-rendered-at{data_attr}>{escape(generated_at["label"])}</span> ·
      <span data-stale>just now</span>
    </span>
    <span class="refresh-state small" data-refresh-state aria-live="polite"></span>
    <button type="button" class="btn-secondary btn-icon" data-refresh-now title="refresh data">↻ refresh</button>
    <label class="toggle" title="refresh data every {DASHBOARD_REFRESH_SECONDS}s">
      <input type="checkbox" name="live">
      <span>live</span>
    </label>
  </div>
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


def _render_sessions(metrics: CrossSessionMetrics, *, filter_query: str = "") -> str:
    if not metrics.sessions:
        return """\
<section class="card">
  <h4>sessions</h4>
  <p class="muted">no sessions found.</p>
</section>"""
    rows = [_render_session_row(s, filter_query=filter_query) for s in metrics.sessions]
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


def _render_session_row(s: SessionMetrics, *, filter_query: str = "") -> str:
    status_color = _STATUS_COLOR.get(s.current_status, "var(--fg-2)")
    glyph = _STATUS_GLYPH.get(s.current_status, "·")
    updated = s.updated_at or "-"
    if updated and len(updated) >= 16:
        updated = updated[5:16].replace("T", " ")
    safe_id = escape(s.session_id)
    href = f"/session/{quote(s.session_id)}{filter_query}"
    return (
        "<tr>"
        f'<td class="brand mono"><a class="session-link" href="{escape(href)}">{safe_id}</a></td>'
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


def filter_to_query_string(filter: MetricsFilter) -> str:
    """Serialize a MetricsFilter back into a URL query string. Round-trips
    with :func:`parse_filter_from_query`."""
    pairs: list[tuple[str, str]] = []
    if filter.since:
        pairs.append(("since", filter.since.date().isoformat()))
    if filter.until:
        pairs.append(("until", filter.until.date().isoformat()))
    for agent in filter.agents:
        pairs.append(("agent", agent))
    if filter.q:
        pairs.append(("q", filter.q))
    return urlencode(pairs)


# ---------------------------------------------------------------------------
# Static head — tokens inlined so the page is self-contained
# ---------------------------------------------------------------------------


def _html_head(*, title: str = "agent-relay · metrics", auto_refresh: bool = True) -> str:
    """Compose the <html><head> block. Public-ish so other dashboard pages
    (Phase B session detail) can reuse the exact same chrome.

    ``auto_refresh`` enables the soft-refresh script for the ``live``
    checkbox + ``↻ refresh`` button. The page never refreshes on its own
    unless the user has flipped the live toggle.
    """
    return f"""\
<html lang=en data-theme=dark>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=Inter:wght@400;500;600;700&display=swap">
<style>
{dashboard_css()}
</style>
{_refresh_script() if auto_refresh else ""}
</head>"""


def _refresh_script() -> str:
    """Opt-in auto-refresh + manual refresh + 'stale Ns ago' indicator.

    Auto-refresh is *off by default* — the page is stable until the user
    ticks the ``live`` checkbox. State persists in ``localStorage`` so a
    flipped toggle survives across reloads.

    Behaviour:
      * ``↻ refresh`` button fetches fresh dashboard data and patches the
        metric regions in place.
      * ``live`` checkbox toggles the same soft refresh at
        DASHBOARD_REFRESH_SECONDS intervals; persisted in localStorage.
      * While the live timer is on, refreshes are deferred whenever focus
        is inside ``.filter-bar`` or text is selected.
      * The header's ``stale`` indicator updates client-side every second
        based on the server snapshot timestamp, so the user always sees
        how old the data is even when not auto-polling.
    """
    return f"""\
<script>
(function() {{
  var REFRESH_MS = {DASHBOARD_REFRESH_SECONDS * 1000};
  var STORAGE_KEY = 'agent-relay-dashboard-live';
  var DATA_PATH = '/dashboard/data';
  var refreshTimer = null;
  var staleTimer = null;
  var refreshing = false;
  var pendingRefresh = false;

  function inFilter() {{
    var el = document.activeElement;
    return !!(el && el.closest && el.closest('.filter-bar'));
  }}

  function hasSelection() {{
    var selection = window.getSelection ? window.getSelection() : null;
    return !!(selection && !selection.isCollapsed && String(selection).length > 0);
  }}

  function shouldDeferRefresh() {{
    return inFilter() || hasSelection();
  }}

  function refreshEndpoint() {{
    var endpoint = (document.body && document.body.getAttribute('data-refresh-endpoint')) || DATA_PATH;
    var url = new URL(endpoint, window.location.href);
    if (!url.search) url.search = window.location.search;
    return url.toString();
  }}

  function fmtAge(seconds) {{
    if (seconds < 1) return 'just now';
    if (seconds < 60) return seconds + 's ago';
    if (seconds < 3600) return Math.floor(seconds / 60) + 'm ago';
    return Math.floor(seconds / 3600) + 'h ago';
  }}

  function generatedAtMs() {{
    var renderedAt = document.querySelector('[data-rendered-at]');
    var raw = renderedAt ? renderedAt.getAttribute('data-generated-at') : '';
    var parsed = raw ? Date.parse(raw) : NaN;
    return isNaN(parsed) ? Date.now() : parsed;
  }}

  function updateStaleAge() {{
    var staleEl = document.querySelector('[data-stale]');
    if (!staleEl) return;
    var age = Math.max(0, Math.floor((Date.now() - generatedAtMs()) / 1000));
    staleEl.textContent = fmtAge(age);
  }}

  function setRefreshState(text, state) {{
    var el = document.querySelector('[data-refresh-state]');
    if (!el) return;
    el.textContent = text || '';
    if (state) el.setAttribute('data-state', state);
    else el.removeAttribute('data-state');
  }}

  function snapshotDetails() {{
    var state = {{}};
    document.querySelectorAll('details[id], details[data-detail-id]').forEach(function(el) {{
      var key = el.id || el.getAttribute('data-detail-id');
      if (key) state[key] = el.open;
    }});
    return state;
  }}

  function restoreDetails(state) {{
    document.querySelectorAll('details[id], details[data-detail-id]').forEach(function(el) {{
      var key = el.id || el.getAttribute('data-detail-id');
      if (key && Object.prototype.hasOwnProperty.call(state, key)) el.open = !!state[key];
    }});
  }}

  function patchRegions(regions) {{
    var scrollX = window.scrollX;
    var scrollY = window.scrollY;
    var detailsState = snapshotDetails();
    Object.keys(regions || {{}}).forEach(function(name) {{
      var region = document.querySelector('[data-dashboard-region="' + name + '"]');
      if (region) region.innerHTML = regions[name];
    }});
    restoreDetails(detailsState);
    window.scrollTo(scrollX, scrollY);
  }}

  function setGeneratedAt(payload) {{
    var renderedAt = document.querySelector('[data-rendered-at]');
    if (!renderedAt) return;
    if (payload.renderedAt) renderedAt.textContent = payload.renderedAt;
    if (payload.generatedAt) renderedAt.setAttribute('data-generated-at', payload.generatedAt);
    updateStaleAge();
  }}

  function refreshDashboard(options) {{
    options = options || {{}};
    if (!window.fetch) {{
      setRefreshState('refresh unavailable', 'error');
      return;
    }}
    if (refreshing) return;
    if (!options.force && shouldDeferRefresh()) {{
      pendingRefresh = true;
      setRefreshState('update ready', 'pending');
      return;
    }}

    refreshing = true;
    setRefreshState('refreshing', 'loading');
    fetch(refreshEndpoint(), {{
      cache: 'no-store',
      headers: {{'Accept': 'application/json'}}
    }})
      .then(function(response) {{
        if (!response.ok) throw new Error('refresh failed');
        return response.json();
      }})
      .then(function(payload) {{
        patchRegions(payload.regions || {{}});
        setGeneratedAt(payload);
        pendingRefresh = false;
        setRefreshState('updated', 'ok');
        window.setTimeout(function() {{
          if (!pendingRefresh) setRefreshState('', '');
        }}, 1500);
      }})
      .catch(function() {{
        setRefreshState('refresh failed', 'error');
      }})
      .then(function() {{
        refreshing = false;
      }});
  }}

  function flushPendingRefresh() {{
    if (pendingRefresh && !shouldDeferRefresh() && !document.hidden) {{
      refreshDashboard({{force: true}});
    }}
  }}

  function startPolling() {{
    stopPolling();
    refreshTimer = setInterval(function() {{
      if (document.hidden) return;
      refreshDashboard();
    }}, REFRESH_MS);
  }}

  function stopPolling() {{
    if (refreshTimer) {{ clearInterval(refreshTimer); refreshTimer = null; }}
  }}

  document.addEventListener('DOMContentLoaded', function() {{
    updateStaleAge();
    staleTimer = setInterval(updateStaleAge, 1000);

    var liveToggle = document.querySelector('input[name="live"]');
    var refreshBtn = document.querySelector('[data-refresh-now]');

    if (refreshBtn) {{
      refreshBtn.addEventListener('click', function(e) {{
        e.preventDefault();
        refreshDashboard({{force: true}});
      }});
    }}

    if (liveToggle) {{
      var saved = (function() {{
        try {{ return localStorage.getItem(STORAGE_KEY) === '1'; }}
        catch (_) {{ return false; }}
      }})();
      liveToggle.checked = saved;
      if (saved) startPolling();

      liveToggle.addEventListener('change', function() {{
        try {{ localStorage.setItem(STORAGE_KEY, liveToggle.checked ? '1' : '0'); }}
        catch (_) {{}}
        if (liveToggle.checked) startPolling(); else stopPolling();
      }});
    }}

    document.addEventListener('focusout', function() {{
      window.setTimeout(flushPendingRefresh, 0);
    }});
    document.addEventListener('selectionchange', flushPendingRefresh);
    document.addEventListener('visibilitychange', function() {{
      if (document.hidden) return;
      if (pendingRefresh) flushPendingRefresh();
      else if (liveToggle && liveToggle.checked) refreshDashboard();
    }});

    window.addEventListener('beforeunload', function() {{
      stopPolling();
      if (staleTimer) clearInterval(staleTimer);
    }});
  }});
}})();
</script>"""


def dashboard_css() -> str:
    """The full stylesheet body. Exposed so Phase B / E pages compose it once."""
    return f"""\
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
  /* Tell native widgets (date picker popup, scrollbars, autofill)
     to render their dark variants. Without this the calendar popup
     opens white-on-white on dark themes. */
  color-scheme: dark;
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
.session-link {{ color: var(--brand); text-decoration: none; }}
.session-link:hover {{ text-decoration: underline; }}
.breadcrumb {{
  max-width: 1200px;
  margin: 8px auto 16px;
  padding: 0 20px;
  font-size: 12px;
  color: var(--fg-3);
  display: flex;
  gap: 6px;
  align-items: baseline;
  flex-wrap: wrap;
}}
.breadcrumb a {{ color: var(--fg-2); text-decoration: none; }}
.breadcrumb a:hover {{ color: var(--fg-1); }}
.breadcrumb .sep {{ color: var(--fg-4); }}
.detail-header {{
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
}}
.detail-title {{
  display: flex;
  flex-direction: column;
  gap: 8px;
}}
.detail-title .title-line {{
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}}
.detail-id {{
  color: var(--brand);
  font-weight: 700;
  word-break: break-word;
}}
.badge {{
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 11px;
  font-weight: 600;
  border: 1px solid var(--surface-rule);
  border-radius: 999px;
  padding: 2px 8px;
  background: var(--surface-1);
  white-space: nowrap;
}}
.meta-line {{
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  color: var(--fg-3);
  font-size: 12px;
}}
.bulleted {{ list-style: none; padding: 0; margin: 0; }}
.bulleted li {{ display: flex; gap: 8px; padding: 4px 0; }}
.bulleted .brand {{ color: var(--brand); flex-shrink: 0; }}
.path {{ color: var(--agent-codex); font-family: var(--font-mono); }}
.prompt-block, .output-block, .raw-block {{
  background: var(--surface-1);
  border: 1px solid var(--surface-rule);
  padding: 12px;
  border-radius: 2px;
  max-height: 480px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 12.5px;
  color: var(--fg-1);
}}
details summary {{
  cursor: pointer;
  color: var(--fg-2);
  font-weight: 600;
  margin-bottom: 8px;
}}
.not-found-card {{
  background: var(--surface-2);
  border: 1px solid var(--error);
  padding: 24px;
  border-radius: 4px;
  text-align: center;
}}

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

/* Filter bar */
.filter-bar form {{ width: 100%; }}
.filter-row {{
  display: flex;
  flex-wrap: wrap;
  gap: 14px 18px;
  align-items: flex-end;
}}
.filter-row .field {{
  display: flex;
  flex-direction: column;
  gap: 5px;
  min-width: 150px;
}}
.filter-row .field.grow {{ flex: 1 1 240px; }}
.filter-row .label,
.filter-row legend.label {{
  font-size: 11px;
  color: var(--fg-2);
  text-transform: lowercase;
  letter-spacing: 0.04em;
  font-weight: 500;
}}

/* Inputs — uniform 32px height so date / search / fieldset bottom-align */
.filter-row input[type="date"],
.filter-row input[type="text"] {{
  background: var(--surface-1);
  border: 1px solid var(--surface-rule);
  color: var(--fg-1);
  font-family: var(--font-mono);
  font-size: 13px;
  padding: 6px 10px;
  height: 32px;
  border-radius: 2px;
  min-width: 150px;
}}
.filter-row input[type="date"] {{
  color-scheme: dark;
  position: relative;
  padding-right: 38px;
  background-image: url("data:image/svg+xml,%3Csvg width='18' height='18' viewBox='0 0 24 24' fill='none' xmlns='http://www.w3.org/2000/svg'%3E%3Cpath d='M8 2v4M16 2v4M3 10h18M5 5h14a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2Z' stroke='%23F4F4F0' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
  background-position: right 10px center;
  background-repeat: no-repeat;
  background-size: 18px 18px;
}}
.filter-row input::placeholder {{
  color: var(--fg-3);
  opacity: 1;  /* Firefox dims placeholders by default; override. */
}}
.filter-row input:hover {{ border-color: var(--fg-4); }}
.filter-row input:focus {{
  outline: none;
  border-color: var(--brand-dim);
  box-shadow: 0 0 0 2px var(--brand-glow);
}}

/* Native date-picker popup: request dark chrome and keep the hidden
   indicator over the same right-side slot as our white SVG icon. */
.filter-row input[type="date"]::-webkit-calendar-picker-indicator {{
  position: absolute;
  top: 0;
  right: 0;
  bottom: 0;
  opacity: 0;
  width: 38px;
  height: 100%;
  margin: 0;
  padding: 0;
  cursor: pointer;
}}

/* Agent fieldset — flush with inputs */
.filter-row fieldset.field {{
  border: 1px solid var(--surface-rule);
  border-radius: 2px;
  padding: 4px 12px 6px;
  margin: 0;
  background: var(--surface-1);
  min-height: 32px;
  flex-direction: row;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
}}
.filter-row fieldset.field legend {{
  padding: 0 6px;
  font-size: 11px;
  color: var(--fg-2);
  text-transform: lowercase;
  letter-spacing: 0.04em;
}}
.filter-row .checkbox {{
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  color: var(--fg-1);
  cursor: pointer;
  user-select: none;
}}
.filter-row .checkbox input[type="checkbox"] {{
  margin: 0;
  accent-color: var(--brand);
  cursor: pointer;
}}

.filter-actions {{
  display: flex;
  gap: 8px;
  align-items: center;
  /* Align with the bottom of the inputs above */
  margin-top: auto;
}}
.btn-primary, .btn-secondary {{
  font-family: var(--font-mono);
  font-size: 13px;
  padding: 0 16px;
  height: 32px;
  border-radius: 2px;
  cursor: pointer;
  text-decoration: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid transparent;
  transition: background 80ms ease, border-color 80ms ease, color 80ms ease;
}}
.btn-primary {{
  background: var(--brand);
  color: var(--surface-0);
  border-color: var(--brand-dim);
  font-weight: 700;
  letter-spacing: 0.02em;
}}
.btn-primary:hover {{
  background: var(--brand-dim);
  color: var(--fg-1);
}}
.btn-secondary {{
  background: var(--surface-1);
  color: var(--fg-2);
  border-color: var(--surface-rule);
}}
.btn-secondary:hover {{
  color: var(--fg-1);
  border-color: var(--fg-3);
  background: var(--surface-2);
}}

/* Banners */
.banner {{
  border: 1px solid var(--surface-rule);
  background: var(--surface-1);
  padding: 8px 16px;
  border-radius: 2px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}}
.banner ul {{ margin: 0; padding-left: 20px; }}
.banner-warning {{ border-color: var(--warning); }}
.banner-warning strong {{ color: var(--warning); text-transform: lowercase; letter-spacing: 0.04em; }}

/* Header live-update controls */
.header-controls {{
  display: flex;
  gap: 12px;
  align-items: center;
}}
.btn-icon {{
  font-size: 11px;
  padding: 4px 10px;
}}
.toggle {{
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--fg-2);
  cursor: pointer;
  user-select: none;
}}
.toggle input[type="checkbox"] {{
  margin: 0;
  accent-color: var(--brand);
  cursor: pointer;
}}
.refresh-state {{
  min-width: 7ch;
  color: var(--fg-3);
  text-align: right;
  font-variant-numeric: tabular-nums;
}}
.refresh-state[data-state="loading"] {{ color: var(--fg-2); }}
.refresh-state[data-state="ok"] {{ color: var(--signal); }}
.refresh-state[data-state="pending"] {{ color: var(--warning); }}
.refresh-state[data-state="error"] {{ color: var(--error); }}
[data-stale] {{
  color: var(--fg-3);
  font-variant-numeric: tabular-nums;
}}

/* Alerts banner — single line, semantic color, links to /alerts */
.alert-banner {{
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 14px;
  background: var(--surface-1);
  border: 1px solid var(--alert-color, var(--surface-rule));
  border-radius: 2px;
  text-decoration: none;
  color: var(--fg-1);
  font-family: var(--font-mono);
  font-size: 13px;
  transition: background 80ms ease, border-color 80ms ease;
}}
.alert-banner:hover {{ background: var(--surface-2); }}
.alert-banner .alert-glyph {{ color: var(--alert-color, var(--warning)); font-weight: 700; }}
.alert-banner .alert-count {{ color: var(--alert-color, var(--warning)); font-weight: 600; }}
.alert-banner .alert-arrow {{ margin-left: auto; color: var(--fg-3); }}
.alert-banner.alert-critical {{ --alert-color: var(--error); }}
.alert-banner.alert-warning {{ --alert-color: var(--warning); }}

/* Alerts page */
table.alerts-table tbody tr td {{ vertical-align: top; }}
table.thresholds td {{ font-size: 12.5px; }}
table.thresholds td:first-child {{ width: 200px; }}
.breadcrumb {{
  font-size: 12px;
  color: var(--fg-3);
  margin: 8px 0 16px;
  display: flex;
  gap: 6px;
  align-items: baseline;
}}
.breadcrumb a {{ color: var(--fg-2); text-decoration: none; }}
.breadcrumb a:hover {{ color: var(--fg-1); }}
.breadcrumb .sep {{ color: var(--fg-4); }}
"""
