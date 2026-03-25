from __future__ import annotations

import json
import sys
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from rich.theme import Theme
from rich.tree import Tree

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
        "status.launching": "bold magenta",
        "status.launch_failed": "bold red",
        "status.ready": "bold dim white",
        "status.succeeded": "bold green",
        "status.failed": "bold red",
        "status.not_run": "dim",
        "agent.claude": "bold #FFB000",
        "agent.codex": "bold cyan",
        "muted": "dim",
    }
)

STATUS_SYMBOLS = {
    "active": "●",
    "paused": "◉",
    "blocked": "✖",
    "completed": "✔",
    "handoff_prepared": "⇄",
    "launching": "◎",
    "launch_failed": "✖",
    "ready": "◌",
    "succeeded": "✔",
    "failed": "✖",
    "not_run": "·",
}

AGENT_SYMBOLS = {
    "claude": "◆",
    "codex": "◇",
}

BANNER_LINES = [
    "[brand]  ╔═══════════════════════════════════════╗[/]",
    "[brand]  ║[/]  [bold white]A G E N T[/]   [brand]R E L A Y[/]            [brand]║[/]",
    "[brand]  ║[/]  [dim]local-first agent handoff cli[/]        [brand]║[/]",
    "[brand]  ╚═══════════════════════════════════════╝[/]",
]

BANNER_COMPACT = "[brand]▸ AGENT RELAY[/]  [dim]·  local-first agent handoff cli[/]"


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
        for line in BANNER_LINES:
            console.print(line)
    console.print()


STATUS_LABELS = {
    "active": "active",
    "paused": "paused",
    "blocked": "blocked",
    "completed": "done",
    "handoff_prepared": "handoff",
    "launching": "launching",
    "launch_failed": "failed",
    "ready": "ready",
    "succeeded": "ok",
    "failed": "failed",
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
    ))


def render_launch_preview(
    console: Console,
    to_agent: str,
    resume_path: str,
    launch_command: str,
    launch_instructions: str,
) -> None:
    if is_compact(console):
        console.print(f"[brand]Launch preview[/]  [label]target:[/] {agent_badge(to_agent)}", highlight=False)
        console.print(f"  [label]Resume:[/]       [path]{resume_path}[/]", highlight=False)
        console.print(f"  [label]Command:[/]      [muted]{launch_command}[/]", highlight=False)
        console.print(f"  [label]Instructions:[/] [value]{launch_instructions}[/]", highlight=False)
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

    console.print(Panel(
        content,
        border_style="brand.dim",
        title="[brand.dim]launch[/]",
        title_align="left",
        padding=(1, 2),
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
    ))

    # Objective
    console.print(f"\n  [label]Objective[/]    [value]{objective}[/]")
    console.print(f"  [label]Workstream[/]   [value]{session_dict.get('workstream_kind', '?')}[/]")
    console.print(f"  [label]Next action[/]  [value]{session_dict.get('next_action') or 'None'}[/]")
    console.print(f"  [label]Created[/]      [muted]{session_dict.get('created_at', '?')}[/]")
    console.print(f"  [label]Updated[/]      [muted]{session_dict.get('updated_at', '?')}[/]")

    # Decisions / Blockers
    decisions = session_dict.get("decisions", [])
    blockers = session_dict.get("blockers", [])

    if decisions or blockers:
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

    for d in session_dict.get("decisions", []):
        console.print(f"  [brand]▸[/] {d}", highlight=False)
    for b in session_dict.get("blockers", []):
        console.print(f"  [error]▸[/] {b}", highlight=False)
    for f in session_dict.get("touched_files", []):
        console.print(f"  [path]{f}[/]", highlight=False)


def render_dashboard(console: Console, sessions: list[dict[str, Any]]) -> None:
    render_banner(console)

    if not sessions:
        console.print("  [muted]No sessions found.[/]")
        console.print("  [label]Start one with:[/]  [brand]agent-relay start --agent claude --task \"...\"[/]")
        console.print()
        return

    if is_compact(console):
        for s in sessions:
            sid = s.get("session_id", "?")
            agent = s.get("current_agent", "?")
            status = s.get("current_status", "?")
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
        table.add_row(
            s.get("session_id", "?"),
            str(agent_badge(s.get("current_agent", "?"), short=True)),
            str(status_badge(s.get("current_status", "?"))),
            obj,
            updated_short,
        )

    console.print(table)
    console.print()


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
    ))
