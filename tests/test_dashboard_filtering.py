"""Phase A — dashboard filter parsing, rendering, and routing."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest import TestCase

from agent_relay.dashboard import filter_to_query_string, render_dashboard_html
from agent_relay.dashboard_query import parse_filter_from_query
from agent_relay.metrics import (
    CrossSessionMetrics,
    MetricsFilter,
    SessionMetrics,
    TokenUsage,
)
from tests._dashboard_test_helpers import (
    get_dashboard,
    start_dashboard_server,
    stop_dashboard_server,
)


def _make_session(
    sid: str,
    *,
    agent: str = "claude",
    status: str = "active",
    started: str | None = "2026-05-07T10:00:00.000Z",
    objective: str = "Build the feature",
) -> SessionMetrics:
    return SessionMetrics(
        session_id=sid,
        current_agent=agent,
        current_status=status,
        objective=objective,
        started_at=started,
        updated_at=started,
        turn_count=1,
        successful_turns=1,
        total_tokens=TokenUsage(input=1, output=1),
        total_cost_usd=0.0,
        total_duration_ms=1000,
        by_agent={agent: TokenUsage(input=1, output=1)},
        cost_by_agent={agent: 0.0},
        turns=(),
    )


def _cross(*sessions: SessionMetrics) -> CrossSessionMetrics:
    return CrossSessionMetrics(
        sessions=sessions,
        by_agent={s.current_agent: TokenUsage(input=1, output=1) for s in sessions},
        cost_by_agent={s.current_agent: 0.0 for s in sessions},
        by_day={},
        total_tokens=TokenUsage(input=len(sessions), output=len(sessions)),
        total_cost_usd=0.0,
        total_duration_ms=1000 * len(sessions),
        session_count=len(sessions),
    )


# ---------------------------------------------------------------------------
# MetricsFilter
# ---------------------------------------------------------------------------


class MetricsFilterTests(TestCase):
    def test_empty_filter_is_identity(self) -> None:
        self.assertTrue(MetricsFilter().is_identity)

    def test_any_field_breaks_identity(self) -> None:
        self.assertFalse(MetricsFilter(since=datetime(2026, 1, 1, tzinfo=UTC)).is_identity)
        self.assertFalse(MetricsFilter(agents=("claude",)).is_identity)
        self.assertFalse(MetricsFilter(q="auth").is_identity)

    def test_session_id_filter(self) -> None:
        f = MetricsFilter(session_ids=("abc", "def"))
        self.assertTrue(f.matches_session(_make_session("abc")))
        self.assertFalse(f.matches_session(_make_session("xyz")))

    def test_q_matches_session_id_or_objective(self) -> None:
        f = MetricsFilter(q="auth")
        self.assertTrue(f.matches_session(_make_session("s1", objective="implement Auth flow")))
        self.assertTrue(f.matches_session(_make_session("authsess", objective="other")))
        self.assertFalse(f.matches_session(_make_session("s1", objective="ship docs")))


# ---------------------------------------------------------------------------
# Query parsing
# ---------------------------------------------------------------------------


class ParseFilterFromQueryTests(TestCase):
    def test_empty_string_returns_identity_filter(self) -> None:
        f, errors = parse_filter_from_query("")
        self.assertTrue(f.is_identity)
        self.assertEqual(errors, ())

    def test_since_until_parse_iso_date(self) -> None:
        f, errors = parse_filter_from_query("since=2026-05-01&until=2026-05-08")
        self.assertEqual(errors, ())
        assert f.since and f.until
        self.assertEqual(f.since.date().isoformat(), "2026-05-01")
        self.assertEqual(f.until.date().isoformat(), "2026-05-08")

    def test_invalid_date_records_error_does_not_raise(self) -> None:
        f, errors = parse_filter_from_query("since=garbage")
        self.assertIsNone(f.since)
        self.assertEqual(len(errors), 1)
        self.assertIn("garbage", errors[0])

    def test_repeatable_agent(self) -> None:
        f, _ = parse_filter_from_query("agent=claude&agent=codex")
        self.assertEqual(f.agents, ("claude", "codex"))

    def test_q_strips_whitespace_and_drops_empty(self) -> None:
        f, _ = parse_filter_from_query("q=%20auth%20")
        self.assertEqual(f.q, "auth")
        f2, _ = parse_filter_from_query("q=")
        self.assertIsNone(f2.q)


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


class FilterRoundTripTests(TestCase):
    def test_round_trip_preserves_all_fields(self) -> None:
        original = MetricsFilter(
            since=datetime(2026, 5, 1, tzinfo=UTC),
            until=datetime(2026, 5, 8, tzinfo=UTC),
            agents=("claude", "codex"),
            q="auth",
        )
        qs = filter_to_query_string(original)
        recovered, errors = parse_filter_from_query(qs)
        self.assertEqual(errors, ())
        self.assertEqual(recovered.since, original.since)
        self.assertEqual(recovered.until, original.until)
        self.assertEqual(recovered.agents, original.agents)
        self.assertEqual(recovered.q, original.q)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


class FilterRenderingTests(TestCase):
    def test_filter_bar_echoes_current_scope(self) -> None:
        f = MetricsFilter(
            since=datetime(2026, 5, 1, tzinfo=UTC),
            agents=("claude",),
            q="auth",
        )
        html = render_dashboard_html(_cross(_make_session("s1")), filter=f)
        self.assertIn('value="2026-05-01"', html)
        # Agent checkbox is checked.
        self.assertIn('value="claude" checked', html)
        # Search box echoes the q.
        self.assertIn('value="auth"', html)

    def test_filter_errors_render_in_warning_banner(self) -> None:
        html = render_dashboard_html(
            _cross(),
            filter=MetricsFilter(),
            filter_errors=("since='garbage' is not a valid date (YYYY-MM-DD)",),
        )
        self.assertIn("filter input ignored", html)
        self.assertIn("garbage", html)
        self.assertIn("banner-warning", html)

    def test_no_errors_no_banner(self) -> None:
        html = render_dashboard_html(_cross(), filter=MetricsFilter())
        self.assertNotIn("filter input ignored", html)

    def test_auto_refresh_is_opt_in(self) -> None:
        """The page must NOT reload on its own; live uses soft refresh."""
        html = render_dashboard_html(_cross(), filter=MetricsFilter())
        # The naive meta-refresh is gone — too disruptive for filter forms,
        # scroll position, and expanded sections.
        self.assertNotIn('http-equiv="refresh"', html)
        self.assertNotIn("window.location.reload", html)
        # The header exposes manual + opt-in live controls.
        self.assertIn("data-refresh-now", html)
        self.assertIn('name="live"', html)
        self.assertIn("data-stale", html)
        self.assertIn("fetch(refreshEndpoint()", html)
        self.assertIn("patchRegions", html)
        # The polling logic still defers reloads while the filter has focus.
        self.assertIn("inFilter", html)
        self.assertIn(".filter-bar", html)


# ---------------------------------------------------------------------------
# End-to-end routing
# ---------------------------------------------------------------------------


class FilterRoutingTests(TestCase):
    """Exercise the filter end-to-end through the live HTTP handler."""

    def setUp(self) -> None:
        self._captured_filters: list[MetricsFilter] = []

        def fake_extractor(_repo_root: Path, *, filter: MetricsFilter | None = None):
            # Record what filter the handler forwarded.
            self._captured_filters.append(filter or MetricsFilter())
            return _cross(_make_session("s1"))

        self._server, self._thread, self._port = start_dashboard_server(
            self,
            repo_root=Path("."),
            extractor=fake_extractor,
        )

    def tearDown(self) -> None:
        stop_dashboard_server(self._server, self._thread)

    def _get(self, path: str) -> tuple[int, str]:
        status, _content_type, body = get_dashboard(self._port, path)
        return status, body

    def test_query_string_is_parsed_and_forwarded_to_extractor(self) -> None:
        status, body = self._get("/?since=2026-05-01&agent=claude&q=auth")
        self.assertEqual(status, 200)
        # Identity check: only non-identity filters are passed through to the
        # extractor; the cached unfiltered snapshot path skips it. We forced
        # a non-identity filter, so it must have arrived.
        forwarded = [f for f in self._captured_filters if not f.is_identity]
        self.assertTrue(forwarded, "extractor never received a non-identity filter")
        f = forwarded[-1]
        assert f.since
        self.assertEqual(f.since.date().isoformat(), "2026-05-01")
        self.assertEqual(f.agents, ("claude",))
        self.assertEqual(f.q, "auth")
        # Filter bar reflects the inputs.
        self.assertIn('value="2026-05-01"', body)
        self.assertIn('value="auth"', body)

    def test_dashboard_data_uses_same_filter_query(self) -> None:
        status, body = self._get("/dashboard/data?since=2026-05-01&agent=claude&q=auth")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertIn("regions", payload)

        forwarded = [f for f in self._captured_filters if not f.is_identity]
        self.assertTrue(forwarded, "extractor never received a non-identity filter")
        f = forwarded[-1]
        assert f.since
        self.assertEqual(f.since.date().isoformat(), "2026-05-01")
        self.assertEqual(f.agents, ("claude",))
        self.assertEqual(f.q, "auth")

    def test_invalid_date_renders_warning_banner_not_500(self) -> None:
        status, body = self._get("/?since=garbage")
        self.assertEqual(status, 200)
        self.assertIn("filter input ignored", body)
        self.assertIn("banner-warning", body)

    def test_invalid_date_dashboard_data_includes_warning_region(self) -> None:
        status, body = self._get("/dashboard/data?since=garbage")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertIn("filter input ignored", payload["regions"]["filter-errors"])
        self.assertIn("banner-warning", payload["regions"]["filter-errors"])

    def test_unfiltered_request_uses_cached_snapshot_path(self) -> None:
        # First request seeds the cache; second hits it.
        self._get("/")
        self._get("/")
        # The extractor was called once-ish at startup (snapshot cache loader),
        # but never with a non-identity filter.
        non_identity = [f for f in self._captured_filters if not f.is_identity]
        self.assertEqual(non_identity, [])
