"""Session detail and turn drill-down HTML for ``metrics-serve``."""

from __future__ import annotations

import json
from datetime import datetime
from html import escape
from typing import Any
from urllib.parse import quote

from agent_relay.dashboard import (
    _STATUS_COLOR,
    _STATUS_GLYPH,
    DASHBOARD_REFRESH_SECONDS,
    _html_head,
    _normalize_generated_at,
    _render_footer,
    _render_region,
)
from agent_relay.formatting import fmt_cost, fmt_duration_ms, fmt_int
from agent_relay.integrity import SessionIntegrityReport
from agent_relay.metrics import SessionMetrics, TurnMetrics
from agent_relay.turn_artifacts import TurnArtifacts


def render_session_detail_html(
    *,
    session_id: str,
    metrics: SessionMetrics,
    integrity: SessionIntegrityReport,
    objective: str | None,
    available_filter_query: str = "",
    generated_at: datetime | str | None = None,
) -> str:
    generated = _normalize_generated_at(generated_at)
    regions = _session_regions(
        session_id=session_id,
        metrics=metrics,
        integrity=integrity,
        objective=objective,
        generated_at=generated,
        available_filter_query=available_filter_query,
    )
    body = _session_body(
        session_id=session_id,
        regions=regions,
        available_filter_query=available_filter_query,
    )
    endpoint = f"/session/{quote(session_id, safe='')}/data"
    return _document(
        title=f"agent-relay · session {session_id}",
        body=body,
        refresh_endpoint=endpoint,
    )


def render_session_detail_payload(
    *,
    session_id: str,
    metrics: SessionMetrics,
    integrity: SessionIntegrityReport,
    objective: str | None,
    generated_at: datetime | str | None = None,
) -> dict[str, object]:
    generated = _normalize_generated_at(generated_at)
    return {
        "generatedAt": generated["iso"],
        "renderedAt": generated["label"],
        "regions": _session_regions(
            session_id=session_id,
            metrics=metrics,
            integrity=integrity,
            objective=objective,
            generated_at=generated,
        ),
    }


def render_turn_detail_html(
    *,
    artifacts: TurnArtifacts,
    metrics: TurnMetrics,
    session_id: str,
    available_filter_query: str = "",
    generated_at: datetime | str | None = None,
) -> str:
    generated = _normalize_generated_at(generated_at)
    regions = _turn_regions(
        artifacts=artifacts,
        metrics=metrics,
        session_id=session_id,
        generated_at=generated,
    )
    body = _turn_body(
        session_id=session_id,
        turn_number=artifacts.turn_number,
        regions=regions,
        available_filter_query=available_filter_query,
    )
    endpoint = f"/session/{quote(session_id, safe='')}/turn/{artifacts.turn_number}/data"
    return _document(
        title=f"agent-relay · session {session_id} · turn {artifacts.turn_number}",
        body=body,
        refresh_endpoint=endpoint,
    )


def render_turn_detail_payload(
    *,
    artifacts: TurnArtifacts,
    metrics: TurnMetrics,
    session_id: str,
    generated_at: datetime | str | None = None,
) -> dict[str, object]:
    generated = _normalize_generated_at(generated_at)
    return {
        "generatedAt": generated["iso"],
        "renderedAt": generated["label"],
        "regions": _turn_regions(
            artifacts=artifacts,
            metrics=metrics,
            session_id=session_id,
            generated_at=generated,
        ),
    }


def render_session_not_found_html(session_id: str) -> str:
    return _not_found_document(
        title="agent-relay · session not found",
        heading="session not found",
        message=f"no relay session matched {session_id}",
    )


def render_turn_not_found_html(session_id: str, turn_number: int) -> str:
    return _not_found_document(
        title="agent-relay · turn not found",
        heading="turn not found",
        message=f"session {session_id} has no turn {turn_number}",
    )


def _session_body(
    *,
    session_id: str,
    regions: dict[str, str],
    available_filter_query: str,
) -> str:
    return "\n".join(
        [
            _breadcrumb(
                [
                    (_dashboard_href(available_filter_query), "← dashboard"),
                    (None, f"session {session_id}"),
                ]
            ),
            '<main class="page">',
            _render_region("header", regions["header"]),
            _render_region("totals", regions["totals"]),
            _render_region("per-turn", regions["per-turn"]),
            _render_region("decisions", regions["decisions"]),
            _render_region("blockers", regions["blockers"]),
            _render_region("research-notes", regions["research-notes"]),
            _render_region("implementation-notes", regions["implementation-notes"]),
            _render_region("touched-files", regions["touched-files"]),
            _render_region("handoffs", regions["handoffs"]),
            _render_region("validation", regions["validation"]),
            _render_region("raw", regions["raw"]),
            _render_footer(),
            "</main>",
        ]
    )


def _turn_body(
    *,
    session_id: str,
    turn_number: int,
    regions: dict[str, str],
    available_filter_query: str,
) -> str:
    query = _query_suffix(available_filter_query)
    session_href = f"/session/{quote(session_id, safe='')}{query}"
    return "\n".join(
        [
            _breadcrumb(
                [
                    (_dashboard_href(available_filter_query), "← dashboard"),
                    (session_href, f"session {session_id}"),
                    (None, f"turn {turn_number}"),
                ]
            ),
            '<main class="page">',
            _render_region("header", regions["header"]),
            _render_region("usage", regions["usage"]),
            _render_region("prompt", regions["prompt"]),
            _render_region("output", regions["output"]),
            _render_region("tool-calls", regions["tool-calls"]),
            _render_region("state", regions["state"]),
            _render_region("raw", regions["raw"]),
            _render_footer(),
            "</main>",
        ]
    )


def _session_regions(
    *,
    session_id: str,
    metrics: SessionMetrics,
    integrity: SessionIntegrityReport,
    objective: str | None,
    generated_at: dict[str, str],
    available_filter_query: str = "",
) -> dict[str, str]:
    return {
        "header": _render_session_header(
            session_id=session_id,
            metrics=metrics,
            integrity=integrity,
            objective=objective,
            generated_at=generated_at,
        ),
        "totals": _render_session_totals(metrics),
        "per-turn": _render_per_turn(metrics, available_filter_query=available_filter_query),
        "decisions": _render_bulleted_card("decisions", integrity.decisions),
        "blockers": _render_bulleted_card("blockers", integrity.blockers),
        "research-notes": _render_bulleted_card("research notes", integrity.research_notes),
        "implementation-notes": _render_bulleted_card(
            "implementation notes", integrity.implementation_notes
        ),
        "touched-files": _render_touched_files(integrity.touched_files),
        "handoffs": _render_handoffs(integrity.handoffs),
        "validation": _render_validation(integrity.validation),
        "raw": _render_session_raw(integrity),
    }


def _turn_regions(
    *,
    artifacts: TurnArtifacts,
    metrics: TurnMetrics,
    session_id: str,
    generated_at: dict[str, str],
) -> dict[str, str]:
    return {
        "header": _render_turn_header(
            session_id=session_id,
            metrics=metrics,
            generated_at=generated_at,
        ),
        "usage": _render_turn_usage(metrics),
        "prompt": _render_text_artifact(
            title="prompt",
            text=artifacts.prompt,
            detail_id="turn-prompt",
            block_class="prompt-block",
            empty="no prompt captured for this turn.",
        ),
        "output": _render_text_artifact(
            title="output",
            text=artifacts.output_text,
            detail_id="turn-output",
            block_class="output-block",
            empty="no output captured for this turn.",
        ),
        "tool-calls": _render_tool_calls(artifacts),
        "state": _render_json_detail(
            title="state",
            detail_id="turn-state",
            value=artifacts.state,
        ),
        "raw": _render_raw_jsonl(artifacts.raw_jsonl),
    }


def _render_session_header(
    *,
    session_id: str,
    metrics: SessionMetrics,
    integrity: SessionIntegrityReport,
    objective: str | None,
    generated_at: dict[str, str],
) -> str:
    corruption = ""
    if integrity.health == "corrupt":
        corruption = """\
<div class="banner banner-warning">
  <strong>session is corrupt — best-effort render</strong>
</div>"""
    created = metrics.started_at or integrity.created_at or "-"
    updated = metrics.updated_at or integrity.updated_at or "-"
    objective_text = objective or metrics.objective or integrity.objective or "-"
    return f"""\
{corruption}
<section class="card">
  <div class="detail-header">
    <div class="detail-title">
      <div class="title-line">
        <span class="detail-id mono">{escape(session_id)}</span>
        {_agent_badge(metrics.current_agent)}
        {_status_badge(metrics.current_status)}
      </div>
      <p>{escape(objective_text)}</p>
      <div class="meta-line">
        <span>created {escape(_short_ts(created))}</span>
        <span>updated {escape(_short_ts(updated))}</span>
      </div>
    </div>
    {_refresh_controls(generated_at)}
  </div>
</section>"""


def _render_session_totals(metrics: SessionMetrics) -> str:
    errors = max(metrics.turn_count - metrics.successful_turns, 0)
    turns = (
        f"{fmt_int(metrics.turn_count)} "
        f'<span class="muted">({fmt_int(metrics.successful_turns)} ok / {fmt_int(errors)} err)</span>'
    )
    return f"""\
<section class="card totals">
  <h4>totals</h4>
  <div class="metric-grid">
    {_metric("turns", turns, raw=True)}
    {_metric("tokens", fmt_int(metrics.total_tokens.total))}
    {_metric("cost", fmt_cost(metrics.total_cost_usd))}
    {_metric("duration", fmt_duration_ms(metrics.total_duration_ms))}
  </div>
</section>"""


def _render_per_turn(metrics: SessionMetrics, *, available_filter_query: str) -> str:
    if not metrics.turns:
        return """\
<section class="card">
  <h4>per turn</h4>
  <p class="muted">no turns yet.</p>
</section>"""
    query = _query_suffix(available_filter_query)
    rows: list[str] = []
    for turn in metrics.turns:
        href = f"/session/{quote(metrics.session_id, safe='')}/turn/{turn.turn_number}{query}"
        rows.append(
            "<tr>"
            f'<td><a class="brand mono session-link" href="{escape(href)}">{turn.turn_number}</a></td>'
            f"<td>{escape(turn.agent)}</td>"
            f"<td class=num>{escape(fmt_int(turn.tokens.input))}</td>"
            f"<td class=num>{escape(fmt_int(turn.tokens.output))}</td>"
            f"<td class=num>{escape(fmt_cost(turn.cost_usd))}</td>"
            f"<td class=num>{escape(fmt_duration_ms(turn.duration_ms))}</td>"
            f"<td class=num>{escape(fmt_int(turn.tool_calls))}</td>"
            f"<td>{_status_badge(turn.status or ('ok' if turn.succeeded else 'failed'))}</td>"
            "</tr>"
        )
    body = "\n      ".join(rows)
    return f"""\
<section class="card">
  <h4>per turn</h4>
  <table class="data">
    <thead>
      <tr>
        <th>#</th><th>agent</th><th class=num>tokens in</th><th class=num>tokens out</th>
        <th class=num>cost</th><th class=num>duration</th><th class=num>tools</th><th>status</th>
      </tr>
    </thead>
    <tbody>
      {body}
    </tbody>
  </table>
</section>"""


def _render_bulleted_card(title: str, items: tuple[str, ...]) -> str:
    if not items:
        return ""
    rows = "\n".join(
        f'    <li><span class="brand">▸</span><span>{escape(item)}</span></li>' for item in items
    )
    return f"""\
<section class="card">
  <h4>{escape(title)}</h4>
  <ul class="bulleted">
{rows}
  </ul>
</section>"""


def _render_touched_files(paths: tuple[str, ...]) -> str:
    if not paths:
        return ""
    rows = "\n".join(
        f'    <li><span class="brand">▸</span><code class="path">{escape(path)}</code></li>'
        for path in sorted(paths)
    )
    return f"""\
<section class="card">
  <h4>touched files</h4>
  <ul class="bulleted">
{rows}
  </ul>
</section>"""


def _render_handoffs(handoffs: tuple[dict[str, object], ...]) -> str:
    if not handoffs:
        return ""
    rows: list[str] = []
    for item in handoffs:
        status = _as_str(item.get("launch_status")) or _as_str(item.get("status")) or "ready"
        rows.append(
            "<tr>"
            f"<td>{escape(_as_str(item.get('from_agent')) or '-')}</td>"
            f"<td>{escape(_as_str(item.get('to_agent')) or '-')}</td>"
            f"<td>{escape(_as_str(item.get('reason')) or '-')}</td>"
            f"<td>{_status_badge(status)}</td>"
            f'<td class="muted">{escape(_short_ts(_as_str(item.get("prepared_at")) or "-"))}</td>'
            "</tr>"
        )
    body = "\n      ".join(rows)
    return f"""\
<section class="card">
  <h4>handoffs</h4>
  <table class="data">
    <thead><tr><th>from</th><th>to</th><th>reason</th><th>status</th><th>when</th></tr></thead>
    <tbody>
      {body}
    </tbody>
  </table>
</section>"""


def _render_validation(validation: dict[str, str]) -> str:
    status = validation.get("status") or ""
    if not status or status == "not_run":
        return ""
    summary = validation.get("summary") or ""
    return f"""\
<section class="card">
  <h4>validation</h4>
  <p>{_status_badge(status)} <span>{escape(summary)}</span></p>
</section>"""


def _render_session_raw(integrity: SessionIntegrityReport) -> str:
    raw = json.dumps(integrity.to_dict(), indent=2, sort_keys=True)
    return f"""\
<section class="card">
  <details data-detail-id="session-raw">
    <summary>raw</summary>
    <pre class="mono raw-block">{escape(raw)}</pre>
  </details>
</section>"""


def _render_turn_header(
    *,
    session_id: str,
    metrics: TurnMetrics,
    generated_at: dict[str, str],
) -> str:
    status = metrics.status or ("ok" if metrics.succeeded else "failed")
    return f"""\
<section class="card">
  <div class="detail-header">
    <div class="detail-title">
      <div class="title-line">
        <span class="detail-id mono">turn {metrics.turn_number}</span>
        {_agent_badge(metrics.agent)}
        {_status_badge(status)}
      </div>
      <div class="meta-line">
        <span>started {escape(_short_ts(metrics.started_at or "-"))}</span>
        <span>finished {escape(_short_ts(metrics.finished_at or "-"))}</span>
        <span>time <span class="mono">{escape(fmt_duration_ms(metrics.duration_ms))}</span></span>
        <span>session {escape(session_id)}</span>
      </div>
    </div>
    {_refresh_controls(generated_at)}
  </div>
</section>"""


def _render_turn_usage(metrics: TurnMetrics) -> str:
    tokens = metrics.tokens
    return f"""\
<section class="card totals">
  <h4>usage</h4>
  <div class="metric-grid">
    {_metric("tokens in", fmt_int(tokens.input))}
    {_metric("tokens out", fmt_int(tokens.output))}
    {_metric("cache read", fmt_int(tokens.cache_read))}
    {_metric("cache creation", fmt_int(tokens.cache_creation))}
    {_metric("cost", fmt_cost(metrics.cost_usd))}
  </div>
</section>"""


def _render_text_artifact(
    *,
    title: str,
    text: str | None,
    detail_id: str,
    block_class: str,
    empty: str,
) -> str:
    if not text:
        return f"""\
<section class="card">
  <h4>{escape(title)}</h4>
  <p class="muted">{escape(empty)}</p>
</section>"""
    return f"""\
<section class="card">
  <details open data-detail-id="{escape(detail_id)}">
    <summary>{escape(title)}</summary>
    <pre class="mono {escape(block_class)}">{escape(text)}</pre>
  </details>
</section>"""


def _render_tool_calls(artifacts: TurnArtifacts) -> str:
    if not artifacts.tool_calls:
        return ""
    rows: list[str] = []
    for call in artifacts.tool_calls:
        ok = (
            '<span style="color: var(--success)">✔</span>'
            if not call.is_error
            else '<span style="color: var(--error)">✖</span>'
        )
        rows.append(
            "<tr>"
            f"<td>{escape(call.name)}</td>"
            f'<td><pre class="mono raw-block">{escape(call.arguments)}</pre></td>'
            f'<td><pre class="mono raw-block">{escape(call.result)}</pre></td>'
            f'<td class="num">{ok}</td>'
            "</tr>"
        )
    body = "\n      ".join(rows)
    return f"""\
<section class="card">
  <h4>tool calls</h4>
  <table class="data">
    <thead><tr><th>tool</th><th>arguments</th><th>result</th><th class=num>ok</th></tr></thead>
    <tbody>
      {body}
    </tbody>
  </table>
</section>"""


def _render_json_detail(*, title: str, detail_id: str, value: dict[str, Any] | None) -> str:
    if value is None:
        return ""
    raw = json.dumps(value, indent=2, sort_keys=True)
    return f"""\
<section class="card">
  <details data-detail-id="{escape(detail_id)}">
    <summary>{escape(title)}</summary>
    <pre class="mono raw-block">{escape(raw)}</pre>
  </details>
</section>"""


def _render_raw_jsonl(lines: tuple[str, ...]) -> str:
    if not lines:
        return ""
    return f"""\
<section class="card">
  <details data-detail-id="turn-raw">
    <summary>raw</summary>
    <pre class="mono raw-block">{escape(chr(10).join(lines))}</pre>
  </details>
</section>"""


def _refresh_controls(generated_at: dict[str, str]) -> str:
    data_attr = (
        f' data-generated-at="{escape(generated_at["iso"])}"' if generated_at.get("iso") else ""
    )
    return f"""\
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
</div>"""


def _metric(label: str, value: str, *, raw: bool = False) -> str:
    rendered = value if raw else escape(value)
    return (
        f'<div class="metric"><span class="label">{escape(label)}</span>'
        f'<span class="value">{rendered}</span></div>'
    )


def _agent_badge(agent: str | None) -> str:
    agent_text = agent or "unknown"
    color = {
        "claude": "var(--agent-claude)",
        "codex": "var(--agent-codex)",
        "gemini": "var(--agent-gemini)",
    }.get(agent_text.lower(), "var(--fg-2)")
    return f'<span class="badge" style="color: {color}">{escape(agent_text)}</span>'


def _status_badge(status: str | None) -> str:
    status_text = status or "unknown"
    glyph, color = _status_marker(status_text)
    return f'<span class="badge status" style="color: {color}">{glyph} {escape(status_text)}</span>'


def _status_marker(status: str) -> tuple[str, str]:
    if status in _STATUS_GLYPH or status in _STATUS_COLOR:
        return _STATUS_GLYPH.get(status, "·"), _STATUS_COLOR.get(status, "var(--fg-2)")
    if status in {"passed", "pass", "success"}:
        return "✔", "var(--success)"
    if status in {"partial", "warning"}:
        return "◌", "var(--warning)"
    return "·", "var(--fg-2)"


def _breadcrumb(parts: list[tuple[str | None, str]]) -> str:
    rendered: list[str] = []
    for index, (href, label) in enumerate(parts):
        if index:
            rendered.append('<span class="sep">/</span>')
        if href:
            rendered.append(f'<a href="{escape(href)}">{escape(label)}</a>')
        else:
            rendered.append(f"<span>{escape(label)}</span>")
    return f'<nav class="breadcrumb">{" ".join(rendered)}</nav>'


def _dashboard_href(query: str) -> str:
    suffix = _query_suffix(query)
    return f"/{suffix}"


def _query_suffix(query: str) -> str:
    if not query:
        return ""
    return query if query.startswith("?") else f"?{query}"


def _short_ts(value: str) -> str:
    if value and len(value) >= 16 and "T" in value:
        return value[:16].replace("T", " ")
    return value


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _document(*, title: str, body: str, refresh_endpoint: str) -> str:
    return (
        f"<!doctype html>\n{_html_head(title=title)}\n"
        f'<body data-refresh-endpoint="{escape(refresh_endpoint)}">\n'
        f"{body}\n</body>\n</html>\n"
    )


def _not_found_document(*, title: str, heading: str, message: str) -> str:
    body = "\n".join(
        [
            _breadcrumb([("/", "← dashboard"), (None, heading)]),
            '<main class="page">',
            '<section class="not-found-card">',
            f"  <h4>{escape(heading)}</h4>",
            f'  <p class="muted">{escape(message)}</p>',
            "</section>",
            _render_footer(),
            "</main>",
        ]
    )
    return f"<!doctype html>\n{_html_head(title=title)}\n<body>\n{body}\n</body>\n</html>\n"


__all__ = [
    "render_session_detail_html",
    "render_session_detail_payload",
    "render_session_not_found_html",
    "render_turn_detail_html",
    "render_turn_detail_payload",
    "render_turn_not_found_html",
]
