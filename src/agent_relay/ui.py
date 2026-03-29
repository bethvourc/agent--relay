from __future__ import annotations

import json
import sys
from typing import Any

from rich import box
from rich.console import Console
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from rich.theme import Theme
from rich.tree import Tree

from agent_relay import __version__

RELAY_THEME = Theme(
    {
        "brand": "bold #FFB000",
        "brand.dim": "#B87A00",
        "heading": "bold white",
        "label": "dim white",
        "value": "white",
        "path": "dim cyan",
        "success": "bold green",
        "error": "bold red",
        "warning": "bold yellow",
        "status.active": "bold green",
        "status.paused": "bold yellow",
        "status.blocked": "bold red",
        "status.completed": "bold #FFB000",
        "status.handoff_prepared": "bold cyan",
        "status.ready_for_handoff": "bold cyan",
        "status.launching": "bold magenta",
        "status.launch_failed": "bold red",
        "status.awaiting_resume": "bold #FFB000",
        "status.degraded": "bold yellow",
        "status.corrupt": "bold red",
        "status.ready": "bold dim white",
        "status.succeeded": "bold green",
        "status.failed": "bold red",
        "status.interrupted": "bold yellow",
        "status.not_run": "dim",
        "agent.claude": "bold #FFB000",
        "agent.codex": "bold cyan",
        "muted": "dim",
        "banner.border": "#B87A00",
        "banner.accent": "bold #FFB000",
        "banner.title": "bold #FFB000",
        "banner.subtitle": "bold white",
        "banner.note": "dim white",
        "banner.prompt": "bold #FFB000",
        "banner.icon": "#B87A00",
        "banner.signal": "bold #7EE34B",
        "banner.surface": "on #121212",
    }
)

STATUS_SYMBOLS = {
    "active": "●",
    "paused": "◉",
    "blocked": "✖",
    "completed": "✔",
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
    "not_run": "·",
}

AGENT_SYMBOLS = {
    "claude": "◆",
    "codex": "◇",
}

BANNER_COMPACT = (
    f"[banner.border]▸[/] [banner.title]Agent Relay[/] "
    f"[muted]v{__version__} · local-first agent handoff cli[/]"
)
BANNER_WIDE_WIDTH = 100


def create_console(*, json_mode: bool = False, quiet: bool = False) -> Console:
    if json_mode or quiet:
        return Console(quiet=True, theme=RELAY_THEME)
    return Console(theme=RELAY_THEME)


def is_compact(console: Console) -> bool:
    return console.width < 80


def emit_json(data: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(data, indent=2) + "\n")
    sys.stdout.flush()


def emit_quiet(value: str) -> None:
    sys.stdout.write(value + "\n")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def render_banner(console: Console) -> None:
    if is_compact(console):
        console.print(BANNER_COMPACT)
    else:
        show_tips = console.width >= BANNER_WIDE_WIDTH
        layout = Table.grid(padding=(0, 1), expand=True)
        layout.add_column(width=6, vertical="top")
        layout.add_column(ratio=1, vertical="top")
        layout.add_row(
            _banner_icon(),
            _banner_body(include_tips=show_tips),
        )
        console.print(Panel(
            layout,
            border_style="banner.border",
            box=box.ROUNDED,
            padding=(1, 2),
            expand=True,
            style="banner.surface",
        ))
    console.print()


def _banner_icon() -> Text:
    signal = "bold #7EE34B"
    frame = "#B87A00"

    icon = Text()
    # Antenna
    icon.append("  ◈\n", style=signal)
    # Forehead with antenna connector
    icon.append("╭─", style=frame)
    icon.append("┴", style=signal)
    icon.append("─╮\n", style=frame)
    # Eyes
    icon.append("│", style=frame)
    icon.append("◈", style=signal)
    icon.append(" ", style="")
    icon.append("◈", style=signal)
    icon.append("│\n", style=frame)
    # Chin
    icon.append("╰───╯", style=frame)
    return icon


def _banner_body(*, include_tips: bool) -> Text:
    body = Text()
    body.append("Agent Relay", style="banner.title")
    body.append(f" v{__version__}", style="muted")
    body.append("\n")
    body.append("Local-first agent handoff CLI", style="banner.subtitle")
    body.append("\n")
    body.append(
        "Capture context, hand off cleanly, and resume with full session state intact.",
        style="banner.note",
    )
    if include_tips:
        body.append("\n\n")
        body.append("Help:", style="banner.note")
        body.append(" ", style="banner.note")
        body.append("agent-relay --help", style="banner.prompt")
        body.append("  •  ", style="banner.border")
        body.append("Sessions:", style="banner.note")
        body.append(" ", style="banner.note")
        body.append("agent-relay dashboard", style="banner.prompt")
    return body


STATUS_LABELS = {
    "active": "active",
    "paused": "paused",
    "blocked": "blocked",
    "completed": "done",
    "handoff_prepared": "handoff",
    "ready_for_handoff": "handoff",
    "launching": "launching",
    "launch_failed": "failed",
    "awaiting_resume": "awaiting",
    "degraded": "degraded",
    "corrupt": "corrupt",
    "ready": "ready",
    "succeeded": "ok",
    "failed": "failed",
    "interrupted": "interrupted",
    "not_run": "pending",
}


def status_badge(status: str) -> Text:
    symbol = STATUS_SYMBOLS.get(status, "?")
    label = STATUS_LABELS.get(status, status)
    style_key = f"status.{status}"
    text = Text(f"{symbol} {label}", style=style_key)
    return text


AGENT_NAMES_DISPLAY = {"claude": "Claude Code", "codex": "Codex"}
AGENT_NAMES_SHORT = {"claude": "Claude", "codex": "Codex"}


def agent_badge(agent_key: str, short: bool = False) -> Text:
    symbol = AGENT_SYMBOLS.get(agent_key, "·")
    style_key = f"agent.{agent_key}"
    names = AGENT_NAMES_SHORT if short else AGENT_NAMES_DISPLAY
    name = names.get(agent_key, agent_key)
    return Text(f"{symbol} {name}", style=style_key)


def _label_value(label: str, value: str, label_style: str = "label", value_style: str = "value") -> Text:
    text = Text()
    text.append(f"  {label}: ", style=label_style)
    text.append(value, style=value_style)
    return text


# ---------------------------------------------------------------------------
# Command renderers
# ---------------------------------------------------------------------------

def render_start_success(
    console: Console,
    session_id: str,
    state_path: str,
    agent: str,
    objective: str,
) -> None:
    render_banner(console)

    if is_compact(console):
        console.print(f"[success]Session created[/]  [brand]{session_id}[/]", highlight=False)
        console.print(f"  [label]Agent:[/]     {agent_badge(agent)}", highlight=False)
        console.print(f"  [label]Objective:[/] [value]{objective}[/]", highlight=False)
        console.print(f"  [label]Path:[/]      [path]{state_path}[/]", highlight=False)
        return

    content = Text()
    content.append("Session created\n\n", style="success")
    content.append("  ID        ", style="label")
    content.append(session_id, style="brand")
    content.append("\n")
    content.append("  Agent     ", style="label")
    content.append_text(agent_badge(agent))
    content.append("\n")
    content.append("  Objective ", style="label")
    content.append(objective, style="value")
    content.append("\n")
    content.append("  Path      ", style="label")
    content.append(state_path, style="path")

    console.print(Panel(
        content,
        border_style="brand",
        title="[brand]new session[/]",
        title_align="left",
        padding=(1, 2),
        expand=False,
    ))


def render_checkpoint_success(
    console: Console,
    session_id: str,
    checkpoint_id: str,
) -> None:
    if is_compact(console):
        console.print(f"[success]Checkpoint saved[/]  [brand]{checkpoint_id}[/]", highlight=False)
        console.print(f"  [label]Session:[/] [muted]{session_id}[/]", highlight=False)
        return

    content = Text()
    content.append("Checkpoint saved\n\n", style="success")
    content.append("  Checkpoint  ", style="label")
    content.append(checkpoint_id, style="brand")
    content.append("\n")
    content.append("  Session     ", style="label")
    content.append(session_id, style="muted")

    console.print(Panel(
        content,
        border_style="brand.dim",
        title="[brand.dim]checkpoint[/]",
        title_align="left",
        padding=(0, 2),
        expand=False,
    ))


def render_pause_success(
    console: Console,
    session_id: str,
    checkpoint_id: str,
    next_action: str,
) -> None:
    if is_compact(console):
        console.print(f"[warning]Session paused[/]  [brand]{checkpoint_id}[/]", highlight=False)
        console.print(f"  [label]Session:[/]     [muted]{session_id}[/]", highlight=False)
        console.print(f"  [label]Next action:[/] [value]{next_action or 'None recorded'}[/]", highlight=False)
        return

    content = Text()
    content.append("Session paused\n\n", style="warning")
    content.append("  Checkpoint  ", style="label")
    content.append(checkpoint_id, style="brand")
    content.append("\n")
    content.append("  Session     ", style="label")
    content.append(session_id, style="muted")
    content.append("\n")
    content.append("  Next action ", style="label")
    content.append(next_action or "None recorded", style="value")

    console.print(Panel(
        content,
        border_style="brand.dim",
        title="[brand.dim]pause[/]",
        title_align="left",
        padding=(0, 2),
        expand=False,
    ))


def render_prepare_success(
    console: Console,
    session_id: str,
    checkpoint_id: str,
    next_action: str,
) -> None:
    if is_compact(console):
        console.print(f"[brand]Prepared for handoff[/]  [brand]{checkpoint_id}[/]", highlight=False)
        console.print(f"  [label]Session:[/]     [muted]{session_id}[/]", highlight=False)
        console.print(f"  [label]Next action:[/] [value]{next_action}[/]", highlight=False)
        return

    content = Text()
    content.append("Prepared for handoff\n\n", style="heading")
    content.append("  Checkpoint  ", style="label")
    content.append(checkpoint_id, style="brand")
    content.append("\n")
    content.append("  Session     ", style="label")
    content.append(session_id, style="muted")
    content.append("\n")
    content.append("  Next action ", style="label")
    content.append(next_action, style="value")

    console.print(Panel(
        content,
        border_style="brand",
        title="[brand]prepare[/]",
        title_align="left",
        padding=(0, 2),
        expand=False,
    ))


def render_failover_success(
    console: Console,
    from_agent: str,
    to_agent: str,
    reason: str,
    resume_path: str,
    launch_command: str,
) -> None:
    if is_compact(console):
        console.print("[brand]Handoff prepared[/]", highlight=False)
        console.print(f"  {agent_badge(from_agent)} [brand]──▶[/] {agent_badge(to_agent)}", highlight=False)
        console.print(f"  [label]Reason:[/]  [value]{reason}[/]", highlight=False)
        console.print(f"  [label]Resume:[/]  [path]{resume_path}[/]", highlight=False)
        console.print(f"  [label]Launch:[/]  [muted]{launch_command}[/]", highlight=False)
        return

    arrow = Text()
    arrow.append("\n")
    arrow.append("    ", style="")
    arrow.append_text(agent_badge(from_agent))
    arrow.append("  ──▶  ", style="brand")
    arrow.append_text(agent_badge(to_agent))
    arrow.append("\n")

    content = Text()
    content.append("Handoff prepared\n", style="heading")
    content.append_text(arrow)
    content.append("\n")
    content.append("  Reason   ", style="label")
    content.append(reason, style="value")
    content.append("\n")
    content.append("  Resume   ", style="label")
    content.append(resume_path, style="path")
    content.append("\n")
    content.append("  Command  ", style="label")
    content.append(launch_command, style="muted")

    console.print(Panel(
        content,
        border_style="brand",
        title="[brand]failover[/]",
        title_align="left",
        padding=(1, 2),
        expand=False,
    ))


def render_launch_preview(
    console: Console,
    to_agent: str,
    resume_path: str,
    launch_command: str,
    launch_instructions: str,
    *,
    warning: str | None = None,
) -> None:
    if is_compact(console):
        console.print(f"[brand]Launch preview[/]  [label]target:[/] {agent_badge(to_agent)}", highlight=False)
        console.print(f"  [label]Resume:[/]       [path]{resume_path}[/]", highlight=False)
        console.print(f"  [label]Command:[/]      [muted]{launch_command}[/]", highlight=False)
        console.print(f"  [label]Instructions:[/] [value]{launch_instructions}[/]", highlight=False)
        if warning:
            console.print(f"  [warning]Warning:[/]     [value]{warning}[/]", highlight=False)
        return

    content = Text()
    content.append("Launch preview\n\n", style="heading")
    content.append("  Target        ", style="label")
    content.append_text(agent_badge(to_agent))
    content.append("\n")
    content.append("  Resume        ", style="label")
    content.append(resume_path, style="path")
    content.append("\n")
    content.append("  Command       ", style="label")
    content.append(launch_command, style="muted")
    content.append("\n")
    content.append("  Instructions  ", style="label")
    content.append(launch_instructions, style="value")
    if warning:
        content.append("\n")
        content.append("  Warning       ", style="label")
        content.append(warning, style="warning")

    console.print(Panel(
        content,
        border_style="brand.dim",
        title="[brand.dim]launch[/]",
        title_align="left",
        padding=(1, 2),
        expand=False,
    ))


def render_launch_executing(console: Console) -> Any:
    return console.status("[brand]Launching agent...[/]", spinner="dots")


def render_launch_result(console: Console, success: bool, exit_code: int) -> None:
    if success:
        console.print()
        console.print("[success]  ✔ Launch succeeded[/]", highlight=False)
    else:
        console.print()
        console.print(f"[error]  ✖ Launch failed[/]  [muted]exit code {exit_code}[/]", highlight=False)


def render_inspect(console: Console, session_dict: dict[str, Any]) -> None:
    if is_compact(console):
        _render_inspect_compact(console, session_dict)
        return

    # Header
    sid = session_dict.get("session_id", "?")
    agent = session_dict.get("current_agent", "?")
    status = session_dict.get("current_status", "?")
    objective = session_dict.get("objective", "?")

    header = Text()
    header.append(sid, style="brand")
    header.append("  ")
    header.append_text(agent_badge(agent))
    header.append("  ")
    header.append_text(status_badge(status))

    console.print(Panel(
        header,
        border_style="brand",
        title="[brand]session[/]",
        title_align="left",
        padding=(0, 2),
        expand=False,
    ))

    # Objective
    console.print(f"\n  [label]Objective[/]    [value]{objective}[/]")
    console.print(f"  [label]Workstream[/]   [value]{session_dict.get('workstream_kind', '?')}[/]")
    console.print(f"  [label]Health[/]       {status_badge(session_dict.get('health', 'healthy'))}")
    console.print(f"  [label]Next action[/]  [value]{session_dict.get('next_action') or 'None'}[/]")
    console.print(f"  [label]Created[/]      [muted]{session_dict.get('created_at', '?')}[/]")
    console.print(f"  [label]Updated[/]      [muted]{session_dict.get('updated_at', '?')}[/]")
    if session_dict.get("last_valid_event"):
        last_valid = session_dict["last_valid_event"]
        console.print(f"  [label]Last valid[/]   [muted]{last_valid.get('event_id', '?')}[/]")
    if session_dict.get("error"):
        console.print(f"  [label]Integrity[/]    [error]{session_dict['error']}[/]")

    # Decisions / Blockers
    decisions = session_dict.get("decisions", [])
    blockers = session_dict.get("blockers", [])
    research_notes = session_dict.get("research_notes", [])
    implementation_notes = session_dict.get("implementation_notes", [])

    if decisions or blockers or research_notes or implementation_notes:
        console.print()
        console.print(Rule(style="brand.dim"))

    if decisions:
        console.print("\n  [heading]Decisions[/]")
        for d in decisions:
            console.print(f"    [brand]▸[/] {d}")

    if blockers:
        console.print("\n  [heading]Blockers[/]")
        for b in blockers:
            console.print(f"    [error]▸[/] {b}")

    if research_notes:
        console.print("\n  [heading]Research Notes[/]")
        for note in research_notes:
            console.print(f"    [brand]▸[/] {note}")

    if implementation_notes:
        console.print("\n  [heading]Implementation Notes[/]")
        for note in implementation_notes:
            console.print(f"    [brand]▸[/] {note}")

    # Touched files
    touched = session_dict.get("touched_files", [])
    if touched:
        console.print()
        console.print(Rule(style="brand.dim"))
        console.print("\n  [heading]Touched files[/]")
        tree = Tree("  [muted].[/]")
        for f in touched:
            tree.add(f"[path]{f}[/]")
        console.print(tree)

    broken_paths = session_dict.get("broken_paths", [])
    suggested_repair = session_dict.get("suggested_repair", [])
    if broken_paths or suggested_repair:
        console.print()
        console.print(Rule(style="brand.dim"))
    if broken_paths:
        console.print("\n  [heading]Broken paths[/]")
        for path in broken_paths:
            console.print(f"    [error]▸[/] [path]{path}[/]")
    if suggested_repair:
        console.print("\n  [heading]Suggested repair[/]")
        for command in suggested_repair:
            console.print(f"    [brand]▸[/] [value]{command}[/]")

    # Handoffs
    handoffs = session_dict.get("handoffs", [])
    if handoffs:
        console.print()
        console.print(Rule(style="brand.dim"))
        console.print("\n  [heading]Handoff history[/]\n")
        table = Table(show_header=True, header_style="label", box=None, padding=(0, 2))
        table.add_column("From", style="value")
        table.add_column("To", style="value")
        table.add_column("Reason", style="muted")
        table.add_column("Status", style="value")
        for h in handoffs:
            table.add_row(
                str(agent_badge(h["from_agent"])),
                str(agent_badge(h["to_agent"])),
                h.get("reason", ""),
                str(status_badge(h.get("launch_status", "ready"))),
            )
        console.print(table)

    # Validation
    validation = session_dict.get("validation", {})
    v_status = validation.get("status", "not_run")
    v_summary = validation.get("summary", "")
    console.print()
    console.print(Rule(style="brand.dim"))
    console.print(f"\n  [heading]Validation[/]  {status_badge(v_status) if v_status in STATUS_SYMBOLS else v_status}")
    if v_summary:
        console.print(f"    {v_summary}")
    console.print()


def _render_inspect_compact(console: Console, session_dict: dict[str, Any]) -> None:
    sid = session_dict.get("session_id", "?")
    agent = session_dict.get("current_agent", "?")
    status = session_dict.get("current_status", "?")

    console.print(f"[brand]{sid}[/]  {agent_badge(agent)}  {status_badge(status)}", highlight=False)
    console.print(f"  [label]Objective:[/] [value]{session_dict.get('objective', '?')}[/]", highlight=False)
    console.print(f"  [label]Health:[/] {status_badge(session_dict.get('health', 'healthy'))}", highlight=False)
    if session_dict.get("error"):
        console.print(f"  [error]{session_dict['error']}[/]", highlight=False)
    for path in session_dict.get("broken_paths", []):
        console.print(f"  [error]▸[/] [path]{path}[/]", highlight=False)
    for command in session_dict.get("suggested_repair", []):
        console.print(f"  [brand]▸[/] [value]{command}[/]", highlight=False)

    for d in session_dict.get("decisions", []):
        console.print(f"  [brand]▸[/] {d}", highlight=False)
    for b in session_dict.get("blockers", []):
        console.print(f"  [error]▸[/] {b}", highlight=False)
    for note in session_dict.get("research_notes", []):
        console.print(f"  [brand]▸[/] research: {note}", highlight=False)
    for note in session_dict.get("implementation_notes", []):
        console.print(f"  [brand]▸[/] implementation: {note}", highlight=False)
    for f in session_dict.get("touched_files", []):
        console.print(f"  [path]{f}[/]", highlight=False)


def render_dashboard(console: Console, sessions: list[dict[str, Any]]) -> None:
    render_banner(console)

    if not sessions:
        console.print("  [muted]No sessions found.[/]")
        console.print("  [label]Start one with:[/]  [brand]agent-relay codex[/]  or  [brand]agent-relay claude[/]")
        console.print()
        return

    if is_compact(console):
        for s in sessions:
            sid = s.get("session_id", "?")
            agent = s.get("current_agent", "?")
            health = s.get("health", "healthy")
            status = health if health != "healthy" else s.get("current_status", "?")
            obj = s.get("objective", "")
            if len(obj) > 40:
                obj = obj[:37] + "..."
            console.print(f"[brand]{sid}[/]  {status_badge(status)}")
            console.print(f"  {agent_badge(agent)}  [muted]{obj}[/]")
        console.print()
        return

    table = Table(
        show_header=True,
        header_style="heading",
        border_style="brand.dim",
        title="[heading]Sessions[/]",
        title_style="heading",
        padding=(0, 1),
    )
    table.add_column("Session ID", style="brand", no_wrap=True)
    table.add_column("Agent", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Objective", style="value")
    table.add_column("Updated", style="muted", no_wrap=True)

    for s in sessions:
        obj = s.get("objective", "")
        max_obj = max(20, console.width - 60)
        if len(obj) > max_obj:
            obj = obj[: max_obj - 3] + "..."
        updated = s.get("updated_at", "?")
        updated_short = updated[5:16].replace("T", " ") if len(updated) >= 16 else updated
        health = s.get("health", "healthy")
        status = health if health != "healthy" else s.get("current_status", "?")
        table.add_row(
            s.get("session_id", "?"),
            str(agent_badge(s.get("current_agent", "?"), short=True)),
            str(status_badge(status)),
            obj,
            updated_short,
        )

    console.print(table)
    console.print()


def render_help(console: Console) -> None:
    render_banner(console)

    compact = is_compact(console)

    if compact:
        console.print("[heading]Usage[/]")
        console.print()
        console.print("  [brand]agent-relay <agent>[/]                   Relay to an agent (codex, claude)")
        console.print("  [brand]agent-relay codex[/]                    Relay to Codex")
        console.print("  [brand]agent-relay claude[/]                   Relay to Claude Code")
        console.print("  [brand]agent-relay claude --task \"...\"[/]      With instructions for the next agent")
        console.print("  [brand]agent-relay codex --no-launch[/]        Just create the packet")
        console.print('  [brand]agent-relay converse claude codex -t "..."[/]  Agent-to-agent conversation')
        console.print("  [brand]agent-relay status[/]                   View sessions")
        console.print("  [brand]agent-relay clean[/]                    Remove all sessions")
        console.print()
        console.print("[heading]Options[/]")
        console.print()
        console.print("  [brand]--task[/]  [brand]-t[/]  What the next agent should do (works with any agent)")
        console.print("  [brand]--from[/]       Source agent (auto-detected)")
        console.print("  [brand]--no-launch[/]  Just create the packet")
        console.print("  [brand]--yes[/]   [brand]-y[/]  Skip confirmation")
        console.print("  [brand]--json[/]       JSON output")
        console.print("  [brand]--quiet[/] [brand]-q[/]  Minimal output")
        console.print()
        return

    # Usage examples
    examples = Table(show_header=False, box=None, padding=(0, 2), pad_edge=True)
    examples.add_column("Command", style="brand", no_wrap=True)
    examples.add_column("Description", style="muted")

    examples.add_row("agent-relay <agent>", "Relay to an agent (codex, claude)")
    examples.add_row("agent-relay codex", "Relay context to Codex")
    examples.add_row("agent-relay claude", "Relay context to Claude Code")
    examples.add_row('agent-relay claude --task "..."', "With instructions for the next agent")
    examples.add_row("agent-relay codex --no-launch", "Create the packet without launching")
    examples.add_row('agent-relay converse claude codex -t "..."', "Agent-to-agent conversation")
    examples.add_row("agent-relay status", "View all relay sessions")
    examples.add_row("agent-relay clean", "Remove all sessions")

    console.print(Panel(
        examples,
        border_style="brand",
        title="[heading]usage[/]",
        title_align="left",
        padding=(1, 2),
        expand=False,
    ))

    console.print()

    # Options
    opts = Table(show_header=False, box=None, padding=(0, 2), pad_edge=True)
    opts.add_column("Flag", style="brand", no_wrap=True, min_width=14)
    opts.add_column("Description", style="value")

    opts.add_row("--task   -t", "What the next agent should do (works with any agent)")
    opts.add_row("--from", "Source agent (auto-detected if omitted)")
    opts.add_row("--no-launch", "Just create the handoff packet")
    opts.add_row("--yes    -y", "Skip confirmation prompt")
    opts.add_row("--json", "Machine-readable JSON output")
    opts.add_row("--quiet  -q", "Minimal output (just the packet path)")

    console.print(Panel(
        opts,
        border_style="brand.dim",
        title="[heading]options[/]",
        title_align="left",
        padding=(0, 2),
        expand=False,
    ))

    console.print()


def _help_row_compact(console: Console, cmd: str, desc: str, usage: str) -> None:
    console.print(f"  [brand]{cmd:12s}[/] {desc}")
    console.print(f"  {'':12s} [muted]{usage}[/]")


def render_relay_success(
    console: Console,
    from_agent: str,
    to_agent: str,
    session_id: str,
    resume_path: str,
    launch_command: str,
    *,
    created_session: bool,
    no_launch: bool,
) -> None:
    render_banner(console)

    if is_compact(console):
        console.print("[success]Relay ready[/]", highlight=False)
        console.print(f"  {agent_badge(from_agent)} [brand]──▶[/] {agent_badge(to_agent)}", highlight=False)
        console.print(f"  [label]Session:[/] [muted]{session_id}[/]", highlight=False)
        console.print(f"  [label]Packet:[/]  [path]{resume_path}[/]", highlight=False)
        if no_launch:
            console.print(f"\n  [label]Run manually:[/]  [muted]{launch_command}[/]", highlight=False)
        else:
            console.print(f"\n  [label]Launch:[/]  [muted]{launch_command}[/]", highlight=False)
        return

    arrow = Text()
    arrow.append("\n")
    arrow.append("    ", style="")
    arrow.append_text(agent_badge(from_agent))
    arrow.append("  ──▶  ", style="brand")
    arrow.append_text(agent_badge(to_agent))
    arrow.append("\n")

    content = Text()
    content.append("Relay ready\n", style="success")
    content.append_text(arrow)
    content.append("\n")
    content.append("  Session  ", style="label")
    content.append(session_id, style="muted")
    if created_session:
        content.append("  (new)", style="muted")
    content.append("\n")
    content.append("  Packet   ", style="label")
    content.append(resume_path, style="path")
    content.append("\n\n")

    if no_launch:
        content.append("  Run manually:\n", style="label")
        content.append(f"  {launch_command}", style="muted")
    else:
        content.append("  Launch:\n", style="label")
        content.append(f"  {launch_command}", style="muted")

    console.print(Panel(
        content,
        border_style="brand",
        title="[brand]relay[/]",
        title_align="left",
        padding=(1, 2),
        expand=False,
    ))


def render_relay_launching(console: Console) -> Any:
    return console.status("[brand]Launching target agent...[/]", spinner="dots")


def render_relay_launch_result(console: Console, success: bool, exit_code: int) -> None:
    if success:
        console.print()
        console.print("[success]  ✔ Agent launched successfully[/]", highlight=False)
    else:
        console.print()
        console.print(f"[error]  ✖ Agent launch failed[/]  [muted]exit code {exit_code}[/]", highlight=False)


def render_error(console: Console, message: str) -> None:
    if is_compact(console):
        console.print(f"[error]Error:[/] {message}")
        return

    console.print(Panel(
        f"[error]{message}[/]",
        border_style="error",
        title="[error]error[/]",
        title_align="left",
        padding=(0, 2),
        expand=False,
    ))


# ---------------------------------------------------------------------------
# Converse UI
# ---------------------------------------------------------------------------

def render_converse_start(
    console: Console,
    agent1: str,
    agent2: str,
    task: str,
    max_turns: int,
) -> None:
    render_banner(console)
    badge1 = agent_badge(agent1)
    badge2 = agent_badge(agent2)
    console.print(f"  {badge1} [brand]⇄[/] {badge2}  [muted]·[/]  [muted]{max_turns} turns max[/]", highlight=False)
    console.print(f"  [label]Task:[/] {task}", highlight=False)
    console.print()


def render_converse_turn_active(console: Console, agent_key: str, turn_number: int, max_turns: int) -> Any:
    """Return a console.status() context manager for the turn spinner."""
    name = AGENT_NAMES_DISPLAY.get(agent_key, agent_key)
    symbol = AGENT_SYMBOLS.get(agent_key, "·")
    return console.status(
        f"  [brand]Turn {turn_number}/{max_turns}[/]  {symbol} [agent.{agent_key}]{name}[/] is thinking...",
        spinner="dots",
    )


def render_converse_turn_done(
    console: Console,
    turn_number: int,
    agent_key: str,
    summary: str,
    exit_code: int,
    text: str = "",
) -> None:
    badge = agent_badge(agent_key, short=True)
    if exit_code == 0:
        console.print(
            f"  [success]✔[/] [brand]Turn {turn_number}[/]  {badge}",
            highlight=False,
        )
    else:
        console.print(
            f"  [error]✖[/] [brand]Turn {turn_number}[/]  {badge}  [error]exit {exit_code}[/]",
            highlight=False,
        )

    # Show the agent's actual output as rendered markdown
    if text.strip():
        md = Markdown(text.strip())
        console.print(Padding(md, (0, 0, 1, 6)))


_STOP_REASON_LABELS = {
    "max_turns": "Max turns reached",
    "done_signal": "Task completed",
    "interrupted": "Interrupted by user",
    "agent_error": "Agent exited with error",
}


def render_converse_result(
    console: Console,
    session_id: str,
    agent1: str,
    agent2: str,
    turns_completed: int,
    stop_reason: str,
) -> None:
    console.print()
    reason_label = _STOP_REASON_LABELS.get(stop_reason, stop_reason)

    if stop_reason == "done_signal":
        style = "success"
        symbol = "✔"
    elif stop_reason in ("agent_error", "interrupted"):
        style = "warning"
        symbol = "◌"
    else:
        style = "brand"
        symbol = "●"

    badge1 = agent_badge(agent1, short=True)
    badge2 = agent_badge(agent2, short=True)
    console.print(f"  [{style}]{symbol} {reason_label}[/]  [muted]·[/]  {badge1} [brand]⇄[/] {badge2}  [muted]·[/]  [muted]{turns_completed} turns[/]", highlight=False)
    console.print(f"  [label]Session:[/]  [muted]{session_id}[/]", highlight=False)
    console.print()
