"""Server-rendered SVG charts.

Pure-Python, no JS, no third-party charting library. All output uses theme
tokens via CSS variables (``var(--brand)``, ``var(--brand-dim)``, …) so the
charts inherit the dashboard's dark theme automatically.

The functions are I/O free and idempotent — same input produces byte-for-byte
identical SVG, which keeps soft-refresh diffs cheap.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from html import escape

__all__ = [
    "sparkline",
    "bar_chart",
    "area_chart",
    "stacked_bar_chart",
    "empty_chart",
]


# ---------------------------------------------------------------------------
# Numeric helpers
# ---------------------------------------------------------------------------


def _coerce_floats(values: Iterable[float | int | None]) -> list[float]:
    return [float(v) if v is not None else 0.0 for v in values]


def _fmt_num(v: float) -> str:
    """Compact float formatter — strips trailing zeros and the dot."""
    if v == int(v):
        return str(int(v))
    return f"{v:.3f}".rstrip("0").rstrip(".")


def _scale_y(value: float, *, vmin: float, vmax: float, top: float, bottom: float) -> float:
    if vmax == vmin:
        return (top + bottom) / 2.0
    norm = (value - vmin) / (vmax - vmin)
    return bottom - norm * (bottom - top)


# ---------------------------------------------------------------------------
# Public chart primitives
# ---------------------------------------------------------------------------


def empty_chart(*, width: int = 180, height: int = 40, label: str = "no data") -> str:
    """Placeholder used when a series is empty. Same viewBox as the chart it
    replaces so layout doesn't shift."""
    return (
        f'<svg class="chart chart-empty" role="img" aria-label="{escape(label)}" '
        f'viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        'preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg">'
        f'<text x="{width / 2:.1f}" y="{height / 2:.1f}" '
        'text-anchor="middle" dominant-baseline="central" '
        'fill="var(--fg-3)" font-family="var(--font-mono)" font-size="11">'
        f"{escape(label)}</text></svg>"
    )


def sparkline(
    values: Sequence[float | int | None],
    *,
    width: int = 180,
    height: int = 40,
    stroke: str = "var(--brand)",
    fill: str | None = None,
    title: str | None = None,
    show_last_dot: bool = True,
    pad: int = 3,
) -> str:
    """Single-series line chart. ``fill`` set turns it into an area chart.

    Returns a placeholder SVG when ``values`` is empty.
    """
    pts = _coerce_floats(values)
    if not pts:
        return empty_chart(width=width, height=height, label=title or "no data")

    vmin = min(pts)
    vmax = max(pts)
    top = float(pad)
    bottom = float(height - pad)
    if len(pts) == 1:
        # one-point series → flat line in the middle.
        xs = [width / 2.0]
    else:
        step = (width - 2 * pad) / (len(pts) - 1)
        xs = [pad + i * step for i in range(len(pts))]
    ys = [_scale_y(v, vmin=vmin, vmax=vmax, top=top, bottom=bottom) for v in pts]
    points = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys, strict=True))

    parts: list[str] = []
    aria = escape(title) if title else "trend"
    parts.append(
        f'<svg class="chart chart-sparkline" role="img" aria-label="{aria}" '
        f'viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        'preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg">'
    )
    if fill is not None and len(pts) > 1:
        # area beneath the line
        area_pts = points + f" {xs[-1]:.1f},{bottom:.1f} {xs[0]:.1f},{bottom:.1f}"
        parts.append(f'<polygon points="{area_pts}" fill="{escape(fill)}" stroke="none"/>')
    if len(pts) > 1:
        parts.append(
            f'<polyline points="{points}" fill="none" stroke="{escape(stroke)}" '
            'stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>'
        )
    if show_last_dot:
        parts.append(
            f'<circle cx="{xs[-1]:.1f}" cy="{ys[-1]:.1f}" r="2" '
            f'fill="{escape(stroke)}" stroke="var(--surface-2)" stroke-width="1"/>'
        )
    parts.append("</svg>")
    return "".join(parts)


def bar_chart(
    values: Sequence[float | int | None],
    *,
    labels: Sequence[str] | None = None,
    width: int = 240,
    height: int = 80,
    fill: str = "var(--brand-dim)",
    title: str | None = None,
    pad: int = 4,
    gap: int = 1,
) -> str:
    """Vertical-bar chart. Labels render as ``<title>`` tooltips so the SVG
    stays compact; visible axis labels live in the surrounding HTML row.
    """
    bars = _coerce_floats(values)
    if not bars:
        return empty_chart(width=width, height=height, label=title or "no data")
    vmax = max(max(bars), 0.0)
    if labels is not None and len(labels) != len(bars):
        raise ValueError("labels length must match values length")

    n = len(bars)
    avail = width - 2 * pad
    bar_w = (avail - gap * (n - 1)) / n if n > 0 else avail
    base_y = float(height - pad)
    aria = escape(title) if title else "values"

    parts: list[str] = [
        f'<svg class="chart chart-bars" role="img" aria-label="{aria}" '
        f'viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        'preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg">'
    ]
    for i, v in enumerate(bars):
        x = pad + i * (bar_w + gap)
        h = (v / vmax) * (height - 2 * pad) if vmax > 0 else 0
        # Always draw a 1-px nub for zero-value bars so the axis is legible.
        h = max(h, 1.0) if v > 0 else 1.0
        y = base_y - h
        title_attr = ""
        if labels is not None:
            label = labels[i]
            title_attr = f"<title>{escape(label)}: {_fmt_num(v)}</title>"
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{h:.1f}" '
            f'fill="{escape(fill)}">{title_attr}</rect>'
        )
    parts.append("</svg>")
    return "".join(parts)


def area_chart(
    values: Sequence[float | int | None],
    *,
    width: int = 240,
    height: int = 80,
    stroke: str = "var(--brand)",
    fill: str = "var(--brand-glow)",
    title: str | None = None,
) -> str:
    """Filled-area variant of :func:`sparkline`."""
    return sparkline(
        values,
        width=width,
        height=height,
        stroke=stroke,
        fill=fill,
        title=title,
        show_last_dot=False,
    )


def stacked_bar_chart(
    stacks: Sequence[Sequence[float | int | None]],
    *,
    labels: Sequence[str] | None = None,
    series_labels: Sequence[str] | None = None,
    width: int = 240,
    height: int = 80,
    fills: Sequence[str] = ("var(--brand)", "var(--brand-dim)"),
    title: str | None = None,
    pad: int = 4,
    gap: int = 1,
) -> str:
    """Vertical stacked bars. ``stacks[i]`` is the per-series tuple for bar
    ``i`` (e.g. ``(tokens_in, tokens_out)``). All series must have equal
    length; ``fills`` provides one color per series.
    """
    if not stacks:
        return empty_chart(width=width, height=height, label=title or "no data")

    rows = [_coerce_floats(s) for s in stacks]
    series_count = len(rows[0])
    if any(len(r) != series_count for r in rows):
        raise ValueError("all stacks must have the same series count")
    if len(fills) < series_count:
        raise ValueError("not enough fills for series count")
    if labels is not None and len(labels) != len(rows):
        raise ValueError("labels length must match stacks length")

    totals = [sum(r) for r in rows]
    vmax = max(max(totals), 0.0)
    n = len(rows)
    avail = width - 2 * pad
    bar_w = (avail - gap * (n - 1)) / n if n > 0 else avail
    base_y = float(height - pad)
    plot_h = float(height - 2 * pad)
    aria = escape(title) if title else "stacked"

    parts: list[str] = [
        f'<svg class="chart chart-stack" role="img" aria-label="{aria}" '
        f'viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        'preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg">'
    ]
    for i, row in enumerate(rows):
        x = pad + i * (bar_w + gap)
        cursor = base_y
        total = totals[i]
        if total <= 0:
            parts.append(
                f'<rect x="{x:.1f}" y="{base_y - 1:.1f}" width="{bar_w:.1f}" height="1" '
                'fill="var(--surface-rule)"/>'
            )
            continue
        for series_idx, value in enumerate(row):
            if value <= 0:
                continue
            seg_h = (value / vmax) * plot_h if vmax > 0 else 0
            seg_y = cursor - seg_h
            tooltip_bits: list[str] = []
            if labels is not None:
                tooltip_bits.append(labels[i])
            if series_labels is not None and series_idx < len(series_labels):
                tooltip_bits.append(series_labels[series_idx])
            tooltip_bits.append(_fmt_num(value))
            tooltip = escape(" · ".join(tooltip_bits))
            parts.append(
                f'<rect x="{x:.1f}" y="{seg_y:.1f}" width="{bar_w:.1f}" height="{seg_h:.1f}" '
                f'fill="{escape(fills[series_idx])}"><title>{tooltip}</title></rect>'
            )
            cursor = seg_y
    parts.append("</svg>")
    return "".join(parts)
