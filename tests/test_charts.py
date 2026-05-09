"""Tests for SVG chart primitives and ``metrics.bucketize``."""

from __future__ import annotations

from unittest import TestCase

from agent_relay.charts import (
    area_chart,
    bar_chart,
    empty_chart,
    sparkline,
    stacked_bar_chart,
)
from agent_relay.metrics import (
    CrossSessionMetrics,
    SessionMetrics,
    TokenUsage,
    TurnMetrics,
    bucketize,
)


def _turn(
    *,
    session_id: str,
    n: int,
    started_at: str,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost: float | None = None,
    duration_ms: int = 0,
    agent: str = "claude",
) -> TurnMetrics:
    return TurnMetrics(
        session_id=session_id,
        turn_number=n,
        agent=agent,
        model=None,
        started_at=started_at,
        finished_at=started_at,
        duration_ms=duration_ms,
        api_duration_ms=None,
        tokens=TokenUsage(input=tokens_in, output=tokens_out),
        cost_usd=cost,
        tool_calls=0,
        status="ok",
        succeeded=True,
    )


def _session(*, session_id: str, turns: tuple[TurnMetrics, ...]) -> SessionMetrics:
    total = TokenUsage()
    cost: float | None = None
    duration = 0
    for t in turns:
        total = total + t.tokens
        duration += t.duration_ms or 0
        if t.cost_usd is not None:
            cost = (cost or 0.0) + t.cost_usd
    return SessionMetrics(
        session_id=session_id,
        current_agent="claude",
        current_status="active",
        objective=None,
        started_at=turns[0].started_at if turns else None,
        updated_at=turns[-1].started_at if turns else None,
        turn_count=len(turns),
        successful_turns=len(turns),
        total_tokens=total,
        total_cost_usd=cost,
        total_duration_ms=duration,
        turns=turns,
    )


class SparklineTests(TestCase):
    def test_empty_returns_placeholder(self) -> None:
        svg = sparkline([])
        self.assertIn("chart-empty", svg)
        self.assertIn("no data", svg)

    def test_single_value_renders_dot(self) -> None:
        svg = sparkline([5.0])
        self.assertIn("<svg", svg)
        self.assertIn("<circle", svg)
        # No polyline for a single point.
        self.assertNotIn("<polyline", svg)

    def test_multiple_values_produce_polyline(self) -> None:
        svg = sparkline([1, 2, 3, 4])
        self.assertIn("<polyline", svg)
        # 4 points × "x,y " coords.
        self.assertEqual(svg.count(","), svg.count(",", svg.find("<polyline")))

    def test_fill_param_adds_polygon(self) -> None:
        svg = sparkline([1, 2, 3], fill="var(--brand-glow)")
        self.assertIn("<polygon", svg)
        self.assertIn("var(--brand-glow)", svg)

    def test_uses_css_var_stroke(self) -> None:
        svg = sparkline([1, 2, 3], stroke="var(--signal)")
        self.assertIn('stroke="var(--signal)"', svg)


class BarChartTests(TestCase):
    def test_empty_returns_placeholder(self) -> None:
        svg = bar_chart([], title="tokens / day")
        self.assertIn("chart-empty", svg)
        self.assertIn("tokens / day", svg)

    def test_renders_one_rect_per_value(self) -> None:
        svg = bar_chart([1, 2, 3])
        self.assertEqual(svg.count("<rect"), 3)

    def test_zero_values_get_axis_nub(self) -> None:
        svg = bar_chart([0, 0, 0])
        # All zero → still draws three thin rects so the axis is legible.
        self.assertEqual(svg.count("<rect"), 3)

    def test_label_mismatch_raises(self) -> None:
        with self.assertRaises(ValueError):
            bar_chart([1, 2, 3], labels=("a", "b"))

    def test_labels_become_titles(self) -> None:
        svg = bar_chart([5], labels=("monday",))
        self.assertIn("<title>monday: 5</title>", svg)


class AreaChartTests(TestCase):
    def test_renders_polygon_under_line(self) -> None:
        svg = area_chart([1, 2, 1])
        self.assertIn("<polygon", svg)
        # No last-dot marker for area variant.
        self.assertNotIn("<circle", svg)


class StackedBarChartTests(TestCase):
    def test_empty_returns_placeholder(self) -> None:
        svg = stacked_bar_chart([])
        self.assertIn("chart-empty", svg)

    def test_renders_one_segment_per_nonzero_series(self) -> None:
        svg = stacked_bar_chart([(1, 2), (3, 0), (0, 0)])
        # Bar 0: 2 segments. Bar 1: 1 segment. Bar 2: zero-stripe (1 rect).
        self.assertEqual(svg.count("<rect"), 4)

    def test_all_zero_uses_axis_stripes(self) -> None:
        svg = stacked_bar_chart([(0, 0), (0, 0)])
        self.assertEqual(svg.count("<rect"), 2)
        self.assertIn("var(--surface-rule)", svg)

    def test_unequal_series_raises(self) -> None:
        with self.assertRaises(ValueError):
            stacked_bar_chart([(1, 2), (3,)])

    def test_too_few_fills_raises(self) -> None:
        with self.assertRaises(ValueError):
            stacked_bar_chart([(1, 2)], fills=("var(--brand)",))

    def test_uses_supplied_series_fills(self) -> None:
        svg = stacked_bar_chart(
            [(1, 1)],
            fills=("var(--brand)", "var(--brand-dim)"),
        )
        self.assertIn("var(--brand)", svg)
        self.assertIn("var(--brand-dim)", svg)


class EmptyChartTests(TestCase):
    def test_renders_label(self) -> None:
        svg = empty_chart(label="no turns yet")
        self.assertIn("no turns yet", svg)
        self.assertIn('role="img"', svg)


class BucketizeTests(TestCase):
    def test_empty_metrics_returns_empty(self) -> None:
        self.assertEqual(bucketize(CrossSessionMetrics(sessions=())), ())

    def test_buckets_per_day_with_aggregates(self) -> None:
        s = _session(
            session_id="s1",
            turns=(
                _turn(
                    session_id="s1",
                    n=1,
                    started_at="2026-05-07T09:00:00Z",
                    tokens_in=10,
                    tokens_out=5,
                    cost=0.1,
                    duration_ms=1000,
                ),
                _turn(
                    session_id="s1",
                    n=2,
                    started_at="2026-05-07T10:00:00Z",
                    tokens_in=2,
                    tokens_out=3,
                    cost=0.05,
                ),
            ),
        )
        metrics = CrossSessionMetrics(sessions=(s,))
        buckets = bucketize(metrics)
        self.assertEqual(len(buckets), 1)
        self.assertEqual(buckets[0].label, "2026-05-07")
        self.assertEqual(buckets[0].tokens, 20)
        self.assertEqual(buckets[0].turns, 2)
        self.assertAlmostEqual(buckets[0].cost or 0.0, 0.15, places=4)

    def test_zero_fills_gap_days(self) -> None:
        s = _session(
            session_id="s1",
            turns=(
                _turn(session_id="s1", n=1, started_at="2026-05-05T09:00:00Z", tokens_in=10),
                _turn(session_id="s1", n=2, started_at="2026-05-08T09:00:00Z", tokens_in=20),
            ),
        )
        buckets = bucketize(CrossSessionMetrics(sessions=(s,)))
        self.assertEqual(
            [b.label for b in buckets],
            [
                "2026-05-05",
                "2026-05-06",
                "2026-05-07",
                "2026-05-08",
            ],
        )
        self.assertEqual(buckets[1].tokens, 0)
        self.assertEqual(buckets[1].turns, 0)
        self.assertIsNone(buckets[1].cost)

    def test_limit_truncates_window(self) -> None:
        s = _session(
            session_id="s1",
            turns=(
                _turn(session_id="s1", n=1, started_at="2026-05-01T09:00:00Z"),
                _turn(session_id="s1", n=2, started_at="2026-05-10T09:00:00Z"),
            ),
        )
        buckets = bucketize(CrossSessionMetrics(sessions=(s,)), limit=3)
        self.assertEqual(len(buckets), 3)
        self.assertEqual(buckets[-1].label, "2026-05-10")

    def test_unsupported_bucket_raises(self) -> None:
        with self.assertRaises(ValueError):
            bucketize(CrossSessionMetrics(sessions=()), by="week")

    def test_idempotent_output(self) -> None:
        s = _session(
            session_id="s1",
            turns=(_turn(session_id="s1", n=1, started_at="2026-05-07T09:00:00Z", tokens_in=5),),
        )
        m = CrossSessionMetrics(sessions=(s,))
        self.assertEqual(bucketize(m), bucketize(m))
