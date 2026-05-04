"""Renderers for the metrics layer.

Stays free of any I/O — given a SessionMetrics or CrossSessionMetrics it
prints a Rich table, emits JSON, or returns a panel renderable for the
watch TUI.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from agent_relay.metrics import (
    CrossSessionMetrics,
    SessionMetrics,
    TokenUsage,
    TurnMetrics,
)


# ---------------------------------------------------------------------------
# Rich tables
# ---------------------------------------------------------------------------


def render_session_metrics(console: Console, metrics: SessionMetrics) -> None:
    header = Text()
    header.append(f"Session {metrics.session_id}", style="brand")
    header.append("  ")
    header.append(f"{metrics.current_agent} ({metrics.current_status})", style="value")
    console.print(header)

    if metrics.objective:
        console.print(f"  [muted]Objective:[/] {metrics.objective}")

    overview = Table(
        show_header=False,
        box=None,
        border_style="brand.dim",
        padding=(0, 1),
    )
    overview.add_column(style="label", no_wrap=True)
    overview.add_column(style="value")

    succ_part = (
        f"{metrics.successful_turns} ok"
        if metrics.successful_turns == metrics.turn_count
        else f"{metrics.successful_turns} ok / {metrics.turn_count - metrics.successful_turns} err"
    )
    overview.add_row("Turns", f"{metrics.turn_count}  ({succ_part})")
    overview.add_row("Tokens", _format_tokens(metrics.total_tokens))
    overview.add_row("Cost", _format_cost(metrics.total_cost_usd, metrics.cost_by_agent))
    overview.add_row(
        "Time",
        _format_duration_summary(metrics.total_duration_ms, metrics.turn_count),
    )
    if metrics.by_agent:
        overview.add_row("By agent", _format_by_agent(metrics.by_agent, metrics.cost_by_agent))
    console.print(overview)

    if metrics.turns:
        console.print()
        per_turn = Table(
            show_header=True,
            header_style="heading",
            border_style="brand.dim",
            padding=(0, 1),
            title="[heading]Per-turn[/]",
            title_style="heading",
        )
        per_turn.add_column("#", justify="right", no_wrap=True)
        per_turn.add_column("Agent", no_wrap=True)
        per_turn.add_column("Tokens in", justify="right")
        per_turn.add_column("Tokens out", justify="right")
        per_turn.add_column("Cost", justify="right")
        per_turn.add_column("Duration", justify="right")
        per_turn.add_column("Tools", justify="right")
        per_turn.add_column("Status", no_wrap=True)
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


def render_cross_session_metrics(
    console: Console, metrics: CrossSessionMetrics
) -> None:
    if metrics.session_count == 0:
        console.print("  [muted]No sessions found.[/]")
        console.print()
        return

    table = Table(
        show_header=True,
        header_style="heading",
        border_style="brand.dim",
        padding=(0, 1),
        title="[heading]Sessions[/]",
        title_style="heading",
    )
    table.add_column("Session", style="brand", no_wrap=True)
    table.add_column("Agent", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Turns", justify="right")
    table.add_column("Tokens", justify="right")
    table.add_column("Cost", justify="right")
    table.add_column("Duration", justify="right")
    for s in metrics.sessions:
        table.add_row(
            s.session_id,
            s.current_agent,
            s.current_status,
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
    totals.add_row("Sessions", str(metrics.session_count))
    totals.add_row("Tokens", _format_tokens(metrics.total_tokens))
    totals.add_row("Cost", _format_cost(metrics.total_cost_usd, metrics.cost_by_agent))
    totals.add_row("Duration", _fmt_duration_ms(metrics.total_duration_ms))
    if metrics.by_agent:
        totals.add_row(
            "By agent", _format_by_agent(metrics.by_agent, metrics.cost_by_agent)
        )
    if metrics.by_day:
        days = ", ".join(
            f"{day} ({_fmt_int(usage.total)})"
            for day, usage in sorted(metrics.by_day.items())
        )
        totals.add_row("By day", days)
    console.print(totals)
    console.print()


def render_metrics_panel(metrics: SessionMetrics) -> Panel:
    """Compact panel for the watch TUI."""
    body = Text()
    body.append("Tokens ")
    body.append(_format_tokens(metrics.total_tokens), style="value")
    body.append("    Cost ")
    body.append(_format_cost(metrics.total_cost_usd, metrics.cost_by_agent), style="value")
    body.append("\n")
    body.append(f"Turns {metrics.turn_count} ", style="value")
    if metrics.turn_count:
        err = metrics.turn_count - metrics.successful_turns
        body.append(f"({metrics.successful_turns} ok, {err} err)   ")
    avg_ms = (
        metrics.total_duration_ms // metrics.turn_count
        if metrics.turn_count
        else 0
    )
    body.append(f"Avg/turn {_fmt_duration_ms(avg_ms)}   ")
    body.append(f"Total {_fmt_duration_ms(metrics.total_duration_ms)}")
    return Panel(body, title="[heading]metrics so far[/]", border_style="brand.dim")


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
    cost = (
        f"${metrics.total_cost_usd:.4f}"
        if metrics.total_cost_usd is not None
        else "-"
    )
    sys.stdout.write(
        f"{metrics.session_id}\t{metrics.current_agent}\t{metrics.turn_count}\t"
        f"{metrics.total_tokens.total or 0}\t{cost}\n"
    )
    sys.stdout.flush()


def emit_cross_session_metrics_quiet(metrics: CrossSessionMetrics) -> None:
    for s in metrics.sessions:
        emit_session_metrics_quiet(s)


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


def _format_cost(
    total: float | None, by_agent: dict[str, float] | None = None
) -> str:
    if total is None:
        return "-"
    if not by_agent or len(by_agent) <= 1:
        return f"${total:.4f}"
    breakdown = ", ".join(
        f"{agent} ${cost:.4f}" for agent, cost in sorted(by_agent.items())
    )
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


def _fmt_int(value: int | None) -> str:
    if value is None:
        return "-"
    return f"{value:,}"


def _fmt_cost(value: float | None) -> str:
    if value is None:
        return "-"
    return f"${value:.4f}"


def _fmt_duration_ms(ms: int | None) -> str:
    if ms is None or ms <= 0:
        return "0s"
    seconds, millis = divmod(ms, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{seconds:02d}s"
    if minutes:
        return f"{minutes}m{seconds:02d}s"
    if seconds:
        return f"{seconds}.{millis // 100}s"
    return f"{ms}ms"


def _status_text(status: str | None, succeeded: bool) -> str:
    if status:
        return status
    return "ok" if succeeded else "err"


__all__ = [
    "render_session_metrics",
    "render_cross_session_metrics",
    "render_metrics_panel",
    "emit_session_metrics_json",
    "emit_cross_session_metrics_json",
    "emit_session_metrics_quiet",
    "emit_cross_session_metrics_quiet",
    "metrics_to_jsonl_line",
]
