"""Shared workspace activity log for agent-to-agent visibility.

A structured markdown file that records what each agent did on each turn,
giving agents visibility into others' work — our equivalent of smux's
pane interaction.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from agent_relay.agents import get_agent_display_name


@dataclass(frozen=True, slots=True)
class LogEntry:
    timestamp: str
    agent_key: str
    agent_slot: int
    entry_type: str     # "turn_complete" | "file_changed" | "signal"
    summary: str


_LOG_HEADER = "# Workspace Activity Log\n\n"


class WorkspaceLog:
    """Append-only structured markdown log for a session."""

    def __init__(self, log_path: Path) -> None:
        self._path = log_path

    @property
    def path(self) -> Path:
        return self._path

    def append(self, entry: LogEntry) -> None:
        """Append a log entry. Creates the file with header if needed."""
        is_new = not self._path.exists()
        with self._path.open("a", encoding="utf-8") as f:
            if is_new:
                f.write(_LOG_HEADER)
            agent_name = get_agent_display_name(entry.agent_key)
            type_label = entry.entry_type.replace("_", " ").title()
            f.write(f"## [{entry.timestamp}] {agent_name} (slot {entry.agent_slot}) — {type_label}\n\n")
            f.write(f"{entry.summary}\n\n")

    def read_all(self) -> list[LogEntry]:
        """Parse all log entries from the file."""
        if not self._path.exists():
            return []

        entries: list[LogEntry] = []
        text = self._path.read_text(encoding="utf-8")
        import re

        # Match ## [timestamp] AgentName (slot N) — Type\n\nSummary
        pattern = re.compile(
            r"^## \[([^\]]+)\] (.+?) \(slot (\d+)\) — (.+)$",
            re.MULTILINE,
        )

        matches = list(pattern.finditer(text))
        for i, m in enumerate(matches):
            timestamp = m.group(1)
            agent_name = m.group(2)
            slot = int(m.group(3))
            entry_type = m.group(4).lower().replace(" ", "_")

            # Extract summary: text between this heading and the next (or EOF)
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            summary = text[start:end].strip()

            # Reverse-lookup agent key from display name
            agent_key = _agent_key_from_name(agent_name)

            entries.append(LogEntry(
                timestamp=timestamp,
                agent_key=agent_key,
                agent_slot=slot,
                entry_type=entry_type,
                summary=summary,
            ))

        return entries


def _agent_key_from_name(display_name: str) -> str:
    """Best-effort reverse lookup of agent key from display name."""
    from agent_relay.agents import AGENT_REGISTRY
    for key, adapter in AGENT_REGISTRY.items():
        if adapter.display_name == display_name:
            return key
    return display_name.lower().replace(" ", "_")


def utc_timestamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
