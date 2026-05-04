"""Renderers for ``agent-relay watch`` — TUI, JSONL stream, and quiet mode.

Pure presentation. All session knowledge lives in :mod:`agent_relay.watch`;
this module just consumes ``WatchEvent`` items and decides how to display
them.
"""
from __future__ import annotations

import json
import sys
from collections import deque
from typing import Any

from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from agent_relay.ui import AGENT_SYMBOLS, agent_badge, status_badge
from agent_relay.watch import WatchEvent, WatchSource

_RECENT_EVENT_LIMIT = 20

_KIND_STYLE = {
    "journal": "cyan",
    "workspace": "magenta",
    "turn_started": "bold green",
    "turn_completed": "bold #FFB000",
    "output_chunk": "white",
    "status_change": "bold yellow",
    "heartbeat": "dim",
}


# ---------------------------------------------------------------------------
# Rendering primitives
# ---------------------------------------------------------------------------


def watch_event_to_compact_line(event: WatchEvent) -> Text:
    """One-row Rich rendering of a WatchEvent. Used by both the TUI and
    the quiet-mode renderer (after stripping styles)."""
    text = Text()
    short_ts = (event.timestamp[11:19] if len(event.timestamp) >= 19 else event.timestamp)
    text.append(f"{short_ts}  ", style="dim")
    text.append(f"{event.kind:<16}", style=_KIND_STYLE.get(event.kind, "white"))
    text.append("  ")
    text.append(_summarize_payload(event), style="value")
    return text


def _summarize_payload(event: WatchEvent) -> str:
    p = event.payload
    if event.kind == "journal":
        seq = p.get("sequence")
        et = p.get("event_type", "?")
        return f"#{seq:>4} {et}"
    if event.kind == "workspace":
        return f"slot {p.get('slot')} {p.get('agent', '?')} — {p.get('entry_type', '?')}: {p.get('summary', '')[:80]}"
    if event.kind == "turn_started":
        return f"turn {p.get('turn_number')}"
    if event.kind == "turn_completed":
        state = p.get("state") or {}
        status = state.get("status") or "?"
        summary = state.get("summary", "")[:80]
        return f"turn {p.get('turn_number')} status={status} — {summary}"
    if event.kind == "output_chunk":
        sub = p.get("event_subtype") or ""
        line = p.get("line", "")
        if len(line) > 120:
            line = line[:117] + "..."
        return f"[{sub}] {line}" if sub else line
    if event.kind == "status_change":
        return f"{p.get('from_status')} -> {p.get('to_status')}"
    if event.kind == "heartbeat":
        return f"status={p.get('current_status')} turn={p.get('current_turn')}"
    return json.dumps(p, default=str)[:120]


# ---------------------------------------------------------------------------
# JSONL streaming mode
# ---------------------------------------------------------------------------


def stream_json_events(source: WatchSource) -> int:
    """Emit one compact JSON object per line for each WatchEvent."""
    try:
        for event in source.iter_events():
            sys.stdout.write(json.dumps(event.to_dict(), default=str) + "\n")
            sys.stdout.flush()
    except KeyboardInterrupt:
        return 130
    return 0


# ---------------------------------------------------------------------------
# Quiet (one line per event) mode
# ---------------------------------------------------------------------------


def stream_quiet_lines(source: WatchSource) -> int:
    try:
        for event in source.iter_events():
            text = watch_event_to_compact_line(event)
            sys.stdout.write(text.plain + "\n")
            sys.stdout.flush()
    except KeyboardInterrupt:
        return 130
    return 0


# ---------------------------------------------------------------------------
# Live TUI mode
# ---------------------------------------------------------------------------


class _LiveState:
    """Mutable state the live renderer reflects on each refresh."""

    def __init__(self, source: WatchSource) -> None:
        self.source = source
        snap = source.snapshot()
        self.session_id = snap.session_id
        self.objective = snap.objective
        self.current_agent: str | None = snap.current_agent
        self.current_status: str | None = snap.current_status
        self.current_turn: int | None = snap.current_turn
        self.last_turn_state: dict[str, Any] | None = snap.last_turn_state
        self.recent: deque[WatchEvent] = deque(maxlen=_RECENT_EVENT_LIMIT)

    def apply(self, event: WatchEvent) -> None:
        self.recent.append(event)
        p = event.payload
        if event.kind == "status_change":
            if p.get("to_status") is not None:
                self.current_status = p.get("to_status")
            if p.get("to_agent") is not None:
                self.current_agent = p.get("to_agent")
        elif event.kind == "turn_started":
            self.current_turn = int(p.get("turn_number", self.current_turn or 0))
        elif event.kind == "turn_completed":
            state = p.get("state")
            if isinstance(state, dict):
                self.last_turn_state = state


def _render_header(state: _LiveState) -> Panel:
    title = Text()
    title.append("session ", style="dim")
    title.append(state.session_id, style="brand")
    if state.current_agent:
        title.append("  ")
        title.append_text(agent_badge(state.current_agent, short=True))
    if state.current_status:
        title.append("  ")
        title.append_text(status_badge(state.current_status))
    if state.current_turn is not None:
        title.append("  ")
        title.append(f"turn {state.current_turn}", style="value")

    body_lines: list[Text] = []
    if state.objective:
        line = Text()
        line.append("Objective: ", style="label")
        line.append(state.objective, style="value")
        body_lines.append(line)
    return Panel(
        Group(title, *body_lines) if body_lines else title,
        border_style="brand.dim",
        padding=(0, 1),
    )


def _render_recent(state: _LiveState) -> Panel:
    table = Table.grid(padding=(0, 1), expand=True)
    table.add_column(no_wrap=False)
    if not state.recent:
        table.add_row(Text("(waiting for events…)", style="dim"))
    else:
        for event in state.recent:
            table.add_row(watch_event_to_compact_line(event))
    return Panel(
        table,
        title="recent events",
        title_align="left",
        border_style="brand.dim",
        padding=(0, 1),
    )


def _render_turn_state(state: _LiveState) -> Panel:
    s = state.last_turn_state or {}
    lines: list[Text] = []
    status = s.get("status") or "—"
    summary = s.get("summary") or ""
    blockers = s.get("blockers") or []
    remaining = s.get("remaining_work") or s.get("current_plan") or []

    line = Text()
    line.append("Status: ", style="label")
    line.append(str(status), style="value")
    line.append("    Blockers: ", style="label")
    line.append(", ".join(blockers) if blockers else "none", style="value")
    lines.append(line)

    if summary:
        sl = Text()
        sl.append("Summary: ", style="label")
        sl.append(summary, style="value")
        lines.append(sl)

    if remaining:
        rl = Text()
        rl.append("Remaining: ", style="label")
        rl.append(
            "  ".join(f"[{i+1}] {item}" for i, item in enumerate(remaining[:6])),
            style="value",
        )
        lines.append(rl)

    if not lines:
        lines.append(Text("(no turn state captured yet)", style="dim"))

    return Panel(
        Group(*lines),
        title="current turn state",
        title_align="left",
        border_style="brand.dim",
        padding=(0, 1),
    )


def _build_layout(state: _LiveState) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(_render_header(state), name="header", size=4),
        Layout(_render_recent(state), name="recent", ratio=1),
        Layout(_render_turn_state(state), name="state", size=7),
    )
    return layout


def render_watch_live(console: Console, source: WatchSource) -> int:
    """Run the full-screen Rich live view until the session terminates or
    the user interrupts. Returns a CLI exit code."""
    state = _LiveState(source)
    try:
        with Live(
            _build_layout(state),
            console=console,
            refresh_per_second=4,
            screen=False,
            transient=False,
        ) as live:
            for event in source.iter_events():
                state.apply(event)
                live.update(_build_layout(state))
    except KeyboardInterrupt:
        return 130
    return 0
