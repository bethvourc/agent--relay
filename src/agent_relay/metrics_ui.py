"""Renderers for the metrics layer.

Stays free of any I/O — given a SessionMetrics or CrossSessionMetrics it
prints a Rich table, emits JSON, or returns a panel renderable for the
watch TUI.
"""

from __future__ import annotations

import json
import sys

from rich.console import Console
from rich.markup import escape as rich_escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from agent_relay.alerts import Alert, AlertConfig
from agent_relay.dashboard_alerts import (
    _RULE_LABEL,
    _SEVERITY_GLYPH,
    _format_rule_value,
    _format_severity_counts,
)
from agent_relay.formatting import fmt_cost, fmt_duration_ms, fmt_int
from agent_relay.metrics import (
    CrossSessionMetrics,
    SessionMetrics,
    TokenUsage,
    TurnMetrics,
)
from agent_relay.ui import STATUS_SYMBOLS, status_badge

# ---------------------------------------------------------------------------
# Rich tables
# ---------------------------------------------------------------------------


def render_session_metrics(console: Console, metrics: SessionMetrics) -> None:
    header = Text()
    header.append("session ", style="muted")
    header.append(metrics.session_id, style="brand")
    header.append("  ")
    header.append(metrics.current_agent, style="value")
    header.append("  ")
    if metrics.current_status in STATUS_SYMBOLS:
        header.append_text(status_badge(metrics.current_status))
    else:
        header.append(metrics.current_status, style="value")
    console.print(header)

    if metrics.objective:
        console.print(f"  [muted]objective:[/] {metrics.objective}")

    overview = Table(
        show_header=False,
        box=None,
        border_style="surface.rule",
        padding=(0, 1),
    )
    overview.add_column(style="label", no_wrap=True)
    overview.add_column(style="value")

    succ_part = (
        f"{metrics.successful_turns} ok"
        if metrics.successful_turns == metrics.turn_count
        else f"{metrics.successful_turns} ok / {metrics.turn_count - metrics.successful_turns} err"
    )
    overview.add_row("turns", f"{metrics.turn_count}  ({succ_part})")
    overview.add_row("tokens", _format_tokens(metrics.total_tokens))
    overview.add_row("cost", _format_cost(metrics.total_cost_usd, metrics.cost_by_agent))
    overview.add_row(
        "time",
        _format_duration_summary(metrics.total_duration_ms, metrics.turn_count),
    )
    if metrics.by_agent:
        overview.add_row("by agent", _format_by_agent(metrics.by_agent, metrics.cost_by_agent))
    console.print(overview)

    if metrics.turns:
        console.print()
        per_turn = Table(
            show_header=True,
            header_style="heading",
            border_style="surface.rule",
            padding=(0, 1),
            title="[heading]per-turn[/]",
            title_style="heading",
        )
        per_turn.add_column("#", justify="right", no_wrap=True)
        per_turn.add_column("agent", no_wrap=True)
        per_turn.add_column("tokens in", justify="right")
        per_turn.add_column("tokens out", justify="right")
        per_turn.add_column("cost", justify="right")
        per_turn.add_column("duration", justify="right")
        per_turn.add_column("tools", justify="right")
        per_turn.add_column("status", no_wrap=True)
        for t in metrics.turns:
            per_turn.add_row(
                str(t.turn_number),
                t.agent,
                _fmt_int(t.tokens.input),
                _fmt_int(t.tokens.output),
                _fmt_cost(t.cost_usd),
                _fmt_duration_ms(t.duration_ms),
                str(t.tool_calls),
                _status_text(t.status, t.succeeded),
            )
        console.print(per_turn)
    console.print()


def render_cross_session_metrics(console: Console, metrics: CrossSessionMetrics) -> None:
    if metrics.session_count == 0:
        console.print("  [muted]no sessions found.[/]")
        console.print()
        return

    table = Table(
        show_header=True,
        header_style="heading",
        border_style="surface.rule",
        padding=(0, 1),
        title="[heading]sessions[/]",
        title_style="heading",
    )
    table.add_column("session", style="brand", no_wrap=True)
    table.add_column("agent", no_wrap=True)
    table.add_column("status", no_wrap=True)
    table.add_column("turns", justify="right")
    table.add_column("tokens", justify="right")
    table.add_column("cost", justify="right")
    table.add_column("duration", justify="right")
    for s in metrics.sessions:
        if s.current_status in STATUS_SYMBOLS:
            status_cell = str(status_badge(s.current_status))
        else:
            status_cell = s.current_status
        table.add_row(
            s.session_id,
            s.current_agent,
            status_cell,
            f"{s.turn_count}",
            _fmt_int(s.total_tokens.total),
            _fmt_cost(s.total_cost_usd),
            _fmt_duration_ms(s.total_duration_ms),
        )
    console.print(table)
    console.print()

    totals = Table(show_header=False, box=None, padding=(0, 1))
    totals.add_column(style="label", no_wrap=True)
    totals.add_column(style="value")
    totals.add_row("sessions", str(metrics.session_count))
    totals.add_row("tokens", _format_tokens(metrics.total_tokens))
    totals.add_row("cost", _format_cost(metrics.total_cost_usd, metrics.cost_by_agent))
    totals.add_row("duration", _fmt_duration_ms(metrics.total_duration_ms))
    if metrics.by_agent:
        totals.add_row("by agent", _format_by_agent(metrics.by_agent, metrics.cost_by_agent))
    if metrics.by_day:
        days = ", ".join(
            f"{day} ({_fmt_int(usage.total)})" for day, usage in sorted(metrics.by_day.items())
        )
        totals.add_row("by day", days)
    console.print(totals)
    console.print()


def render_metrics_panel(metrics: SessionMetrics) -> Panel:
    """Compact panel for the watch TUI."""
    body = Text()
    body.append("tokens ", style="label")
    body.append(_format_tokens(metrics.total_tokens), style="value")
    body.append("    cost ", style="label")
    body.append(_format_cost(metrics.total_cost_usd, metrics.cost_by_agent), style="value")
    body.append("\n")
    body.append("turns ", style="label")
    body.append(f"{metrics.turn_count} ", style="value")
    if metrics.turn_count:
        err = metrics.turn_count - metrics.successful_turns
        body.append(f"({metrics.successful_turns} ok, {err} err)   ", style="muted")
    avg_ms = metrics.total_duration_ms // metrics.turn_count if metrics.turn_count else 0
    body.append("avg/turn ", style="label")
    body.append(f"{_fmt_duration_ms(avg_ms)}   ", style="value")
    body.append("total ", style="label")
    body.append(_fmt_duration_ms(metrics.total_duration_ms), style="value")
    return Panel(body, title="[heading]metrics so far[/]", border_style="surface.rule")


def render_alerts_terminal(
    console: Console,
    alerts: tuple[Alert, ...],
    cfg: AlertConfig,
    config_path: object,
) -> None:
    """Render active alert firings using the same compact table grammar as metrics."""
    _ = cfg
    if not alerts:
        console.print(f"  [muted]no alerts firing.  thresholds in:[/] {config_path}")
        console.print()
        return

    header = Text()
    header.append("alerts ", style="muted")
    header.append(_format_severity_counts(alerts), style="value")
    header.append("  ")
    header.append("thresholds ", style="muted")
    header.append(str(config_path), style="label")
    console.print(header)

    table = Table(
        show_header=True,
        header_style="heading",
        border_style="surface.rule",
        padding=(0, 1),
        title="[heading]active alerts[/]",
        title_style="heading",
    )
    table.add_column("severity", no_wrap=True)
    table.add_column("rule")
    table.add_column("observed", justify="right")
    table.add_column("threshold", justify="right")
    table.add_column("session", style="brand", no_wrap=True)
    table.add_column("turn", justify="right", no_wrap=True)
    table.add_column("message")
    for alert in alerts:
        table.add_row(
            _severity_terminal_cell(alert),
            rich_escape(_RULE_LABEL.get(alert.rule, alert.rule)),
            rich_escape(_format_rule_value(alert.rule, alert.observed)),
            rich_escape(_format_rule_value(alert.rule, alert.threshold)),
            rich_escape(alert.session_id),
            "-" if alert.turn_number is None else str(alert.turn_number),
            rich_escape(alert.message),
        )
    console.print(table)
    console.print()


# ---------------------------------------------------------------------------
# JSON / quiet output
# ---------------------------------------------------------------------------


def emit_session_metrics_json(metrics: SessionMetrics) -> None:
    payload = {"command": "metrics", "session": metrics.to_dict()}
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def emit_cross_session_metrics_json(metrics: CrossSessionMetrics) -> None:
    payload = {"command": "metrics", **metrics.to_dict()}
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def metrics_to_jsonl_line(item: SessionMetrics | TurnMetrics) -> str:
    if isinstance(item, TurnMetrics):
        return json.dumps({"kind": "turn", **item.to_dict()})
    return json.dumps({"kind": "session", **item.to_dict()})


def emit_session_metrics_quiet(metrics: SessionMetrics) -> None:
    cost = f"${metrics.total_cost_usd:.4f}" if metrics.total_cost_usd is not None else "-"
    sys.stdout.write(
        f"{metrics.session_id}\t{metrics.current_agent}\t{metrics.turn_count}\t"
        f"{metrics.total_tokens.total or 0}\t{cost}\n"
    )
    sys.stdout.flush()


def emit_cross_session_metrics_quiet(metrics: CrossSessionMetrics) -> None:
    for s in metrics.sessions:
        emit_session_metrics_quiet(s)


def emit_alerts_quiet(alerts: tuple[Alert, ...]) -> None:
    for alert in alerts:
        turn = "-" if alert.turn_number is None else str(alert.turn_number)
        sys.stdout.write(
            f"{alert.severity}\t{alert.rule}\t{alert.observed}\t{alert.threshold}\t"
            f"{alert.session_id}\t{turn}\n"
        )
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


def _format_tokens(tokens: TokenUsage) -> str:
    parts: list[str] = []
    if tokens.input is not None:
        parts.append(f"in {tokens.input:,}")
    if tokens.output is not None:
        parts.append(f"out {tokens.output:,}")
    if tokens.cache_read is not None:
        parts.append(f"cache_r {tokens.cache_read:,}")
    if tokens.cache_creation is not None:
        parts.append(f"cache_w {tokens.cache_creation:,}")
    return "  ".join(parts) if parts else "-"


def _format_cost(total: float | None, by_agent: dict[str, float] | None = None) -> str:
    if total is None:
        return "-"
    if not by_agent or len(by_agent) <= 1:
        return f"${total:.4f}"
    breakdown = ", ".join(f"{agent} ${cost:.4f}" for agent, cost in sorted(by_agent.items()))
    return f"${total:.4f}  ({breakdown})"


def _format_duration_summary(total_ms: int, turn_count: int) -> str:
    total = _fmt_duration_ms(total_ms)
    if turn_count == 0:
        return total
    avg = _fmt_duration_ms(total_ms // turn_count)
    return f"{total}  (avg/turn {avg})"


def _format_by_agent(
    tokens_by_agent: dict[str, TokenUsage],
    cost_by_agent: dict[str, float],
) -> str:
    parts: list[str] = []
    for agent, usage in sorted(tokens_by_agent.items()):
        cost = cost_by_agent.get(agent)
        cost_str = f" ${cost:.4f}" if cost is not None else ""
        total = usage.total or 0
        parts.append(f"{agent} {total:,}{cost_str}")
    return "   ".join(parts) if parts else "-"


def _severity_terminal_cell(alert: Alert) -> str:
    style = "error" if alert.severity == "critical" else "warning"
    glyph = _SEVERITY_GLYPH.get(alert.severity, "◌")
    return f"[{style}]{glyph} {rich_escape(alert.severity)}[/]"


# Backwards-compatible aliases for tests / external callers.
_fmt_int = fmt_int
_fmt_cost = fmt_cost
_fmt_duration_ms = fmt_duration_ms


# DS-canonical turn-status aliases. Internal control-protocol values
# like ``propose_done`` get mapped onto the fixed status vocabulary so
# the per-turn `status` column stays aligned with the rest of the UI.
_TURN_STATUS_ALIASES = {
    "propose_done": "done",
    "propose_continue": "active",
    "propose_blocked": "blocked",
    "succeeded": "ok",
}


def _status_text(status: str | None, succeeded: bool) -> str:
    if not status:
        return "ok" if succeeded else "failed"
    if status in _TURN_STATUS_ALIASES:
        return _TURN_STATUS_ALIASES[status]
    if status in STATUS_SYMBOLS:
        return status
    # Unknown internal value — fall back to the binary outcome.
    return "ok" if succeeded else "failed"


__all__ = [
    "render_session_metrics",
    "render_cross_session_metrics",
    "render_metrics_panel",
    "render_alerts_terminal",
    "emit_session_metrics_json",
    "emit_cross_session_metrics_json",
    "emit_session_metrics_quiet",
    "emit_cross_session_metrics_quiet",
    "emit_alerts_quiet",
    "metrics_to_jsonl_line",
]
