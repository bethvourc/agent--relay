"""Number, cost, and duration formatters used across the TUI.

Centralized so panels, tables, dashboards, and exporters render the
same atom of data identically — per the design-system rule that
numbers are always padded for alignment.
"""
from __future__ import annotations


def fmt_int(value: int | None) -> str:
    """`1,234` (comma-grouped) or `-` for unknown."""
    if value is None:
        return "-"
    return f"{value:,}"


def fmt_cost(value: float | None) -> str:
    """`$0.0042` (4 decimals) or `-` for unknown."""
    if value is None:
        return "-"
    return f"${value:.4f}"


def fmt_duration_ms(ms: int | None) -> str:
    """`45ms`, `2m13s`, `1h05m02s` — terse, padded, monotonic."""
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
