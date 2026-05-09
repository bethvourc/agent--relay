"""Phase B dashboard session detail and turn drill-down tests."""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest import TestCase

from agent_relay.dashboard_session import (
    render_session_detail_html,
    render_session_not_found_html,
    render_turn_detail_html,
)
from agent_relay.integrity import SessionIntegrityReport
from agent_relay.layout import turn_dir
from agent_relay.metrics import SessionMetrics, TokenUsage, TurnMetrics
from agent_relay.storage import load_session_view
from agent_relay.turn_artifacts import ToolCall, TurnArtifacts, load_turn_artifacts
from tests._dashboard_test_helpers import (
    get_dashboard,
    start_dashboard_server,
    stop_dashboard_server,
)
from tests.session_fixtures import build_sample_session


def _write_turn_artifacts(repo: Path, sid: str, n: int = 3) -> Path:
    tdir = turn_dir(repo, sid, n)
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "prompt.md").write_text("Turn prompt content\n", encoding="utf-8")
    output_lines = [
        {"type": "text", "text": "Derived output text"},
        {"type": "tool_use", "id": "tool-1", "name": "Read", "input": {"path": "README.md"}},
        {
            "type": "tool_result",
            "tool_use_id": "tool-1",
            "content": "README contents",
            "is_error": False,
        },
        {
            "type": "result",
            "duration_ms": 42000,
            "total_cost_usd": 0.1234,
            "usage": {"input_tokens": 10, "output_tokens": 20},
        },
    ]
    (tdir / "output.jsonl").write_text(
        "\n".join(json.dumps(item) for item in output_lines) + "\n",
        encoding="utf-8",
    )
    (tdir / "state.json").write_text(
        json.dumps(
            {
                "agent_key": "claude",
                "turn_number": n,
                "status": "completed",
                "metadata": {
                    "started_at": "2026-05-09T10:00:00.000Z",
                    "finished_at": "2026-05-09T10:00:42.000Z",
                },
            }
        ),
        encoding="utf-8",
    )
    return tdir


def _turn_metrics(
    n: int = 1,
    *,
    prompt_agent: str = "claude",
    cost_usd: float | None = 0.1234,
) -> TurnMetrics:
    return TurnMetrics(
        session_id="s1",
        turn_number=n,
        agent=prompt_agent,
        model="claude-opus",
        started_at="2026-05-09T10:00:00.000Z",
        finished_at="2026-05-09T10:00:42.000Z",
        duration_ms=42000,
        api_duration_ms=31000,
        tokens=TokenUsage(input=10, output=20, cache_read=3, cache_creation=4),
        cost_usd=cost_usd,
        tool_calls=1,
        status="completed",
        succeeded=True,
    )


def _session_metrics(*turns: TurnMetrics, sid: str = "s1") -> SessionMetrics:
    total = TokenUsage()
    total_cost: float | None = None
    total_duration = 0
    for turn in turns:
        total = total + turn.tokens
        if turn.cost_usd is not None:
            total_cost = (total_cost or 0.0) + turn.cost_usd
        total_duration += turn.duration_ms or 0
    return SessionMetrics(
        session_id=sid,
        current_agent="claude",
        current_status="active",
        objective="Build session detail",
        started_at="2026-05-09T10:00:00.000Z",
        updated_at="2026-05-09T10:30:00.000Z",
        turn_count=len(turns),
        successful_turns=len([turn for turn in turns if turn.succeeded]),
        total_tokens=total,
        total_cost_usd=total_cost,
        total_duration_ms=total_duration,
        by_agent={"claude": total} if turns else {},
        cost_by_agent={"claude": total_cost} if turns and total_cost is not None else {},
        turns=turns,
    )


def _integrity_report(session_id: str = "s1", *, health: str = "healthy") -> SessionIntegrityReport:
    return SessionIntegrityReport(
        session_id=session_id,
        storage_model="journal_v2",
        repo_root="/tmp/repo",
        objective="Build session detail",
        workstream_kind="mixed",
        created_at="2026-05-09T10:00:00.000Z",
        updated_at="2026-05-09T10:30:00.000Z",
        initial_agent="claude",
        current_agent="claude",
        current_status="active" if health != "corrupt" else "corrupt",
        task_status=None,
        next_action="",
        decisions=("Use soft refresh",),
        blockers=("Need fixtures",),
        research_notes=("Mapped dashboard renderer",),
        implementation_notes=("Added detail cards",),
        touched_files=("src/agent_relay/dashboard_session.py", "tests/test_dashboard_session.py"),
        validation={"status": "passed", "summary": "dashboard tests passed"},
        latest_checkpoint_id="cp-1",
        prepared_handoff_id="ho-1",
        latest_launch_id="la-1",
        last_resume_handoff_id=None,
        handoffs=(
            {
                "from_agent": "claude",
                "to_agent": "codex",
                "reason": "Move implementation",
                "launch_status": "succeeded",
                "prepared_at": "2026-05-09T10:20:00.000Z",
            },
        ),
        checkpoint_ids=("cp-1",),
        launch_ids=("la-1",),
        health=health,
        error="journal missing" if health == "corrupt" else None,
        last_valid_event=None,
        broken_paths=("journal",) if health == "corrupt" else tuple(),
        suggested_repair=("restore journal",) if health == "corrupt" else tuple(),
        alerts=("journal missing",) if health == "corrupt" else tuple(),
    )


class TurnArtifactsTests(TestCase):
    def test_load_turn_artifacts_returns_populated_fields_when_all_files_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _write_turn_artifacts(repo, "s1", 1)
            (tdir / "output.txt").write_text("Human output\n", encoding="utf-8")

            artifacts = load_turn_artifacts(repo, "s1", 1)

        self.assertEqual(artifacts.prompt, "Turn prompt content\n")
        self.assertEqual(artifacts.output_text, "Human output\n")
        self.assertEqual(artifacts.state["status"], "completed")
        self.assertEqual(artifacts.raw_jsonl[0], '{"type": "text", "text": "Derived output text"}')
        self.assertEqual(artifacts.tool_calls[0].name, "Read")

    def test_load_turn_artifacts_missing_files_returns_empty_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = load_turn_artifacts(Path(tmp), "s1", 1)

        self.assertIsNone(artifacts.prompt)
        self.assertIsNone(artifacts.output_text)
        self.assertIsNone(artifacts.state)
        self.assertEqual(artifacts.tool_calls, ())
        self.assertEqual(artifacts.raw_jsonl, ())

    def test_load_turn_artifacts_truncates_large_tool_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = turn_dir(repo, "s1", 1)
            tdir.mkdir(parents=True, exist_ok=True)
            huge = "x" * 100_000
            (tdir / "output.jsonl").write_text(
                json.dumps({"type": "tool_use", "id": "tool-1", "name": "Write", "input": huge})
                + "\n",
                encoding="utf-8",
            )

            artifacts = load_turn_artifacts(repo, "s1", 1)

        self.assertEqual(len(artifacts.tool_calls[0].arguments), 4001)
        self.assertTrue(artifacts.tool_calls[0].arguments.endswith("…"))

    def test_load_turn_artifacts_derives_output_text_from_jsonl_text_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = turn_dir(repo, "s1", 1)
            tdir.mkdir(parents=True, exist_ok=True)
            (tdir / "output.jsonl").write_text(
                json.dumps({"type": "text", "text": "one"})
                + "\n"
                + json.dumps({"message": {"content": [{"type": "text", "text": "two"}]}})
                + "\n",
                encoding="utf-8",
            )

            artifacts = load_turn_artifacts(repo, "s1", 1)

        self.assertEqual(artifacts.output_text, "one\ntwo")

    def test_load_turn_artifacts_pairs_tool_use_with_result_by_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = turn_dir(repo, "s1", 1)
            tdir.mkdir(parents=True, exist_ok=True)
            (tdir / "output.jsonl").write_text(
                json.dumps({"type": "tool_use", "id": "abc", "name": "Read", "input": {"p": "x"}})
                + "\n"
                + json.dumps(
                    {
                        "type": "tool_result",
                        "tool_use_id": "abc",
                        "content": "done",
                        "is_error": True,
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            artifacts = load_turn_artifacts(repo, "s1", 1)

        self.assertEqual(artifacts.tool_calls[0].result, "done")
        self.assertTrue(artifacts.tool_calls[0].is_error)


class DashboardSessionRenderTests(TestCase):
    def test_render_session_detail_contains_session_id_status_and_turn_rows(self) -> None:
        metrics = _session_metrics(_turn_metrics(1), _turn_metrics(2))
        html = render_session_detail_html(
            session_id="s1",
            metrics=metrics,
            integrity=_integrity_report(),
            objective="Build session detail",
            generated_at=datetime(2026, 5, 9, 10, 11, tzinfo=UTC),
        )

        self.assertIn("s1", html)
        self.assertIn("● active", html)
        self.assertIn("/session/s1/turn/1", html)
        self.assertIn("/session/s1/turn/2", html)

    def test_render_session_detail_includes_per_turn_charts(self) -> None:
        metrics = _session_metrics(_turn_metrics(1), _turn_metrics(2))
        html = render_session_detail_html(
            session_id="s1",
            metrics=metrics,
            integrity=_integrity_report(),
            objective="Build session detail",
        )
        self.assertIn('data-dashboard-region="charts"', html)
        self.assertIn("per-turn trends", html)
        self.assertIn("chart-stack", html)
        self.assertIn("chart-sparkline", html)
        self.assertIn('fill="var(--agent-codex)"', html)
        self.assertIn('fill="var(--brand)"', html)
        self.assertNotIn('fill="var(--brand-dim)"', html)
        self.assertNotIn("duration / turn", html)
        self.assertNotIn("duration per turn", html)

    def test_render_session_detail_cost_chart_shows_empty_state_when_cost_missing(self) -> None:
        metrics = _session_metrics(_turn_metrics(1, cost_usd=None), _turn_metrics(2, cost_usd=None))
        html = render_session_detail_html(
            session_id="s1",
            metrics=metrics,
            integrity=_integrity_report(),
            objective="Build session detail",
        )
        self.assertIn("no cost data", html)
        self.assertIn("chart-empty", html)
        self.assertIn('<span class="value mono">-</span>', html)

    def test_render_session_detail_cost_chart_plots_only_known_costs(self) -> None:
        metrics = _session_metrics(
            _turn_metrics(1, cost_usd=None),
            _turn_metrics(2, cost_usd=0.25),
            _turn_metrics(3, cost_usd=0.50),
        )
        html = render_session_detail_html(
            session_id="s1",
            metrics=metrics,
            integrity=_integrity_report(),
            objective="Build session detail",
        )
        self.assertIn("$0.7500", html)
        self.assertIn("2 cost points", html)
        self.assertNotIn("chart-empty", html)

    def test_render_session_detail_per_turn_table_marks_missing_cost_and_omits_duration(
        self,
    ) -> None:
        metrics = _session_metrics(_turn_metrics(1, cost_usd=None))
        html = render_session_detail_html(
            session_id="s1",
            metrics=metrics,
            integrity=_integrity_report(),
            objective="Build session detail",
        )
        self.assertIn('<td class=num><span class="muted">no cost data</span></td>', html)
        self.assertNotIn("<th class=num>duration</th>", html)
        self.assertNotIn("duration / turn", html)

    def test_render_session_detail_empty_turns_uses_empty_state(self) -> None:
        html = render_session_detail_html(
            session_id="s1",
            metrics=_session_metrics(),
            integrity=_integrity_report(),
            objective="Build session detail",
        )

        self.assertIn("no turns yet", html)
        self.assertNotIn("<h4>per turn</h4>\n  <table", html)

    def test_render_session_detail_corrupt_session_renders_banner(self) -> None:
        html = render_session_detail_html(
            session_id="s1",
            metrics=_session_metrics(),
            integrity=_integrity_report(health="corrupt"),
            objective="Build session detail",
        )

        self.assertIn("session is corrupt", html)
        self.assertIn("best-effort render", html)

    def test_render_session_detail_escapes_user_controlled_values(self) -> None:
        sid = "<script>alert(1)</script>"
        html = render_session_detail_html(
            session_id=sid,
            metrics=_session_metrics(sid=sid),
            integrity=_integrity_report(sid),
            objective="<b>owned</b>",
        )

        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertNotIn("<b>owned</b>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)

    def test_render_turn_detail_contains_prompt_output_and_tool_calls(self) -> None:
        artifacts = TurnArtifacts(
            session_id="s1",
            turn_number=1,
            prompt="prompt text",
            output_text="output text",
            state={"status": "completed"},
            tool_calls=(ToolCall(name="Read", arguments="{}", result="ok", is_error=False),),
            raw_jsonl=("raw",),
        )
        html = render_turn_detail_html(
            artifacts=artifacts,
            metrics=_turn_metrics(1),
            session_id="s1",
        )

        self.assertIn("prompt text", html)
        self.assertIn("output text", html)
        self.assertIn("Read", html)

    def test_render_turn_detail_escapes_prompt_and_output(self) -> None:
        artifacts = TurnArtifacts(
            session_id="s1",
            turn_number=1,
            prompt="<img src=x onerror=alert(1)>",
            output_text="<script>alert(1)</script>",
            state=None,
            tool_calls=(),
            raw_jsonl=(),
        )
        html = render_turn_detail_html(
            artifacts=artifacts,
            metrics=_turn_metrics(1),
            session_id="s1",
        )

        self.assertNotIn("<img src=x onerror=alert(1)>", html)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", html)

    def test_render_turn_detail_empty_prompt_and_output_uses_empty_states(self) -> None:
        artifacts = TurnArtifacts(
            session_id="s1",
            turn_number=1,
            prompt=None,
            output_text=None,
            state=None,
            tool_calls=(),
            raw_jsonl=(),
        )
        html = render_turn_detail_html(
            artifacts=artifacts,
            metrics=_turn_metrics(1),
            session_id="s1",
        )

        self.assertIn("no prompt captured for this turn", html)
        self.assertIn("no output captured for this turn", html)
        self.assertNotIn("<details", html)

    def test_render_session_not_found_returns_complete_html_doc(self) -> None:
        html = render_session_not_found_html("missing")

        self.assertTrue(html.startswith("<!doctype html>"))
        self.assertIn("not-found-card", html)
        self.assertIn("session not found", html)

    def test_breadcrumb_back_link_preserves_filter_query(self) -> None:
        html = render_session_detail_html(
            session_id="s1",
            metrics=_session_metrics(_turn_metrics(1)),
            integrity=_integrity_report(),
            objective="Build session detail",
            available_filter_query="since=2026-05-01&agent=claude",
        )

        self.assertIn('href="/?since=2026-05-01&amp;agent=claude"', html)


class DashboardSessionRoutingTests(TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        fixture = build_sample_session(self.repo)
        self.sid = fixture["session_id"]
        load_session_view(self.repo, self.sid)
        _write_turn_artifacts(self.repo, self.sid, 3)
        self.server, self.thread, self.port = start_dashboard_server(self, repo_root=self.repo)

    def tearDown(self) -> None:
        stop_dashboard_server(self.server, self.thread)
        self._tmp.cleanup()

    def test_get_session_known_id_returns_detail_page(self) -> None:
        status, content_type, body = get_dashboard(self.port, f"/session/{self.sid}")

        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)
        self.assertIn(self.sid, body)

    def test_get_session_unknown_returns_friendly_404(self) -> None:
        status, content_type, body = get_dashboard(self.port, "/session/unknown")

        self.assertEqual(status, 404)
        self.assertIn("text/html", content_type)
        self.assertIn("session not found", body)

    def test_get_session_weird_chars_is_rejected(self) -> None:
        status, _, _ = get_dashboard(self.port, "/session/%3Cbad%3E")

        self.assertEqual(status, 404)

    def test_get_session_path_traversal_is_rejected(self) -> None:
        status, _, _ = get_dashboard(self.port, "/session/../../../etc/passwd")

        self.assertEqual(status, 404)

    def test_get_session_data_returns_regions(self) -> None:
        status, content_type, body = get_dashboard(self.port, f"/session/{self.sid}/data")
        payload = json.loads(body)

        self.assertEqual(status, 200)
        self.assertIn("application/json", content_type)
        self.assertIn("header", payload["regions"])
        self.assertIn("totals", payload["regions"])
        self.assertIn("per-turn", payload["regions"])

    def test_get_turn_known_id_returns_detail_page(self) -> None:
        status, content_type, body = get_dashboard(self.port, f"/session/{self.sid}/turn/3")

        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)
        self.assertIn("turn 3", body)
        self.assertIn("Turn prompt content", body)

    def test_get_turn_unknown_returns_404(self) -> None:
        status, _, _ = get_dashboard(self.port, f"/session/{self.sid}/turn/9999")

        self.assertEqual(status, 404)

    def test_get_turn_negative_is_rejected(self) -> None:
        status, _, _ = get_dashboard(self.port, f"/session/{self.sid}/turn/-1")

        self.assertEqual(status, 404)

    def test_get_turn_data_returns_regions(self) -> None:
        status, content_type, body = get_dashboard(self.port, f"/session/{self.sid}/turn/3/data")
        payload = json.loads(body)

        self.assertEqual(status, 200)
        self.assertIn("application/json", content_type)
        self.assertIn("header", payload["regions"])
        self.assertIn("prompt", payload["regions"])
        self.assertIn("output", payload["regions"])
        self.assertIn("tool-calls", payload["regions"])

    def test_detail_breadcrumb_preserves_filter_query(self) -> None:
        status, _, body = get_dashboard(
            self.port,
            f"/session/{self.sid}?since=2026-05-01&agent=claude",
        )

        self.assertEqual(status, 200)
        self.assertIn('href="/?since=2026-05-01&amp;agent=claude"', body)
