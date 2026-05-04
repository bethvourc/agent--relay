"""Tests for the compute-on-read metrics extractor."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from agent_relay.layout import (
    derived_view_path,
    turn_dir,
    turns_dir,
)
from agent_relay.metrics import (
    CrossSessionMetrics,
    SessionMetrics,
    TokenUsage,
    TurnMetrics,
    extract_cross_session_metrics,
    extract_session_metrics,
    extract_turn_metrics,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scaffold(repo: Path, sid: str) -> Path:
    turns_dir(repo, sid).mkdir(parents=True, exist_ok=True)
    derived_view_path(repo, sid).parent.mkdir(parents=True, exist_ok=True)
    return repo


def _write_view(repo: Path, sid: str, **fields) -> None:
    base = {
        "session_id": sid,
        "current_agent": "claude",
        "current_status": "active",
        "objective": "test objective",
        "created_at": "2026-05-01T10:00:00.000Z",
        "updated_at": "2026-05-01T10:30:00.000Z",
    }
    base.update(fields)
    derived_view_path(repo, sid).write_text(json.dumps(base), encoding="utf-8")


def _write_turn(
    repo: Path,
    sid: str,
    turn_number: int,
    *,
    output_lines: list[dict] | None = None,
    state: dict | None = None,
    raw_output: str | None = None,
) -> Path:
    tdir = turn_dir(repo, sid, turn_number)
    tdir.mkdir(parents=True, exist_ok=True)
    if raw_output is not None:
        (tdir / "output.jsonl").write_text(raw_output, encoding="utf-8")
    elif output_lines is not None:
        (tdir / "output.jsonl").write_text(
            "\n".join(json.dumps(o) for o in output_lines) + "\n",
            encoding="utf-8",
        )
    if state is not None:
        (tdir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    return tdir


def _claude_turn_state(turn_number: int = 1, status: str = "continue") -> dict:
    return {
        "agent_key": "claude",
        "agent_display_name": "Claude Code",
        "turn_number": turn_number,
        "status": status,
        "metadata": {
            "started_at": "2026-05-01T10:00:00.000Z",
            "finished_at": "2026-05-01T10:00:42.000Z",
        },
    }


def _claude_result_event(**overrides) -> dict:
    base = {
        "type": "result",
        "duration_ms": 42000,
        "duration_api_ms": 31000,
        "total_cost_usd": 0.4318,
        "usage": {
            "input_tokens": 6,
            "output_tokens": 56,
            "cache_creation_input_tokens": 29229,
            "cache_read_input_tokens": 1000,
        },
        "modelUsage": {"claude-opus-4-7": {"costUSD": 0.4318}},
    }
    base.update(overrides)
    return base


def _claude_assistant_with_tool_use() -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "model": "claude-opus-4-7",
            "content": [
                {"type": "text", "text": "Reading the file."},
                {"type": "tool_use", "name": "Read", "input": {"path": "x"}},
                {"type": "tool_use", "name": "Edit", "input": {"path": "x"}},
            ],
        },
    }


# ---------------------------------------------------------------------------
# TokenUsage
# ---------------------------------------------------------------------------


class TokenUsageTests(TestCase):
    def test_total_sums_present_fields(self) -> None:
        u = TokenUsage(input=10, output=20, cache_read=5, cache_creation=2)
        self.assertEqual(u.total, 37)

    def test_total_is_none_when_all_fields_none(self) -> None:
        self.assertIsNone(TokenUsage().total)

    def test_total_handles_partial_fields(self) -> None:
        self.assertEqual(TokenUsage(input=10).total, 10)

    def test_addition_combines_optionals(self) -> None:
        a = TokenUsage(input=10, output=None, cache_read=5)
        b = TokenUsage(input=3, output=20, cache_read=None)
        c = a + b
        self.assertEqual(c.input, 13)
        self.assertEqual(c.output, 20)
        self.assertEqual(c.cache_read, 5)
        self.assertIsNone(c.cache_creation)

    def test_addition_keeps_none_when_both_none(self) -> None:
        c = TokenUsage() + TokenUsage()
        self.assertIsNone(c.input)


# ---------------------------------------------------------------------------
# extract_turn_metrics
# ---------------------------------------------------------------------------


class ExtractTurnMetricsTests(TestCase):
    def test_claude_full_extraction(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = _scaffold(Path(tmp), "s1")
            _write_view(repo, "s1")
            _write_turn(
                repo, "s1", 1,
                output_lines=[_claude_assistant_with_tool_use(), _claude_result_event()],
                state=_claude_turn_state(),
            )
            t = extract_turn_metrics(repo, "s1", 1)

        self.assertEqual(t.agent, "claude")
        self.assertEqual(t.turn_number, 1)
        self.assertEqual(t.duration_ms, 42000)
        self.assertEqual(t.api_duration_ms, 31000)
        self.assertAlmostEqual(t.cost_usd or 0.0, 0.4318)
        self.assertEqual(t.tokens.input, 6)
        self.assertEqual(t.tokens.output, 56)
        self.assertEqual(t.tokens.cache_read, 1000)
        self.assertEqual(t.tokens.cache_creation, 29229)
        self.assertEqual(t.tool_calls, 2)
        self.assertEqual(t.model, "claude-opus-4-7")
        self.assertTrue(t.succeeded)

    def test_missing_state_file_does_not_raise(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = _scaffold(Path(tmp), "s1")
            _write_view(repo, "s1")
            _write_turn(repo, "s1", 1, output_lines=[_claude_result_event()])
            t = extract_turn_metrics(repo, "s1", 1)
        self.assertEqual(t.agent, "claude")  # guessed from events
        self.assertIsNone(t.started_at)
        self.assertEqual(t.duration_ms, 42000)
        self.assertTrue(t.succeeded)  # no status → benign default

    def test_missing_output_file_returns_zero_metrics(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = _scaffold(Path(tmp), "s1")
            _write_view(repo, "s1")
            _write_turn(repo, "s1", 1, state=_claude_turn_state())
            t = extract_turn_metrics(repo, "s1", 1)
        self.assertEqual(t.tool_calls, 0)
        self.assertIsNone(t.cost_usd)
        self.assertIsNone(t.tokens.total)
        # Duration derived from state.json timestamps.
        self.assertEqual(t.duration_ms, 42000)

    def test_partial_jsonl_lines_are_skipped(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = _scaffold(Path(tmp), "s1")
            _write_view(repo, "s1")
            raw = (
                json.dumps(_claude_result_event())
                + "\n"
                + '{"type":"assistant","message":'  # truncated
            )
            _write_turn(repo, "s1", 1, raw_output=raw, state=_claude_turn_state())
            t = extract_turn_metrics(repo, "s1", 1)
        self.assertEqual(t.tokens.input, 6)

    def test_failure_status_marks_unsucceeded(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = _scaffold(Path(tmp), "s1")
            _write_view(repo, "s1")
            _write_turn(
                repo, "s1", 1,
                output_lines=[_claude_result_event()],
                state=_claude_turn_state(status="error"),
            )
            t = extract_turn_metrics(repo, "s1", 1)
        self.assertFalse(t.succeeded)

    def test_codex_extraction(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = _scaffold(Path(tmp), "s1")
            _write_view(repo, "s1", current_agent="codex")
            events = [
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "ok"},
                },
                {
                    "type": "item.completed",
                    "item": {"type": "tool_call", "name": "shell"},
                },
                {"type": "item.completed", "item": {"type": "function_call", "name": "x"}},
                {
                    "type": "result",
                    "token_usage": {"input_tokens": 100, "output_tokens": 50},
                },
            ]
            state = _claude_turn_state()
            state["agent_key"] = "codex"
            _write_turn(repo, "s1", 1, output_lines=events, state=state)
            t = extract_turn_metrics(repo, "s1", 1)
        self.assertEqual(t.agent, "codex")
        self.assertEqual(t.tool_calls, 2)
        self.assertEqual(t.tokens.input, 100)
        self.assertEqual(t.tokens.output, 50)
        self.assertIsNone(t.cost_usd)

    def test_gemini_extraction(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = _scaffold(Path(tmp), "s1")
            _write_view(repo, "s1", current_agent="gemini")
            events = [
                {
                    "message": {
                        "role": "model",
                        "model": "gemini-2.5-pro",
                        "content": [
                            {"type": "text", "text": "ok"},
                            {"type": "function_call", "name": "shell"},
                        ],
                        "usageMetadata": {
                            "promptTokenCount": 200,
                            "candidatesTokenCount": 80,
                        },
                    }
                }
            ]
            state = _claude_turn_state()
            state["agent_key"] = "gemini"
            _write_turn(repo, "s1", 1, output_lines=events, state=state)
            t = extract_turn_metrics(repo, "s1", 1)
        self.assertEqual(t.agent, "gemini")
        self.assertEqual(t.tokens.input, 200)
        self.assertEqual(t.tokens.output, 80)
        self.assertEqual(t.tool_calls, 1)
        self.assertEqual(t.model, "gemini-2.5-pro")

    def test_invalid_json_state_file_treated_as_missing(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = _scaffold(Path(tmp), "s1")
            _write_view(repo, "s1")
            tdir = turn_dir(repo, "s1", 1)
            tdir.mkdir(parents=True, exist_ok=True)
            (tdir / "state.json").write_text("{not json", encoding="utf-8")
            (tdir / "output.jsonl").write_text(
                json.dumps(_claude_result_event()) + "\n", encoding="utf-8"
            )
            t = extract_turn_metrics(repo, "s1", 1)
        self.assertIsNone(t.status)
        self.assertTrue(t.succeeded)


# ---------------------------------------------------------------------------
# extract_session_metrics
# ---------------------------------------------------------------------------


class ExtractSessionMetricsTests(TestCase):
    def test_aggregates_multiple_turns(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = _scaffold(Path(tmp), "s1")
            _write_view(repo, "s1")
            for i in (1, 2, 3):
                _write_turn(
                    repo, "s1", i,
                    output_lines=[_claude_result_event()],
                    state=_claude_turn_state(turn_number=i),
                )
            sm = extract_session_metrics(repo, "s1")
        self.assertEqual(sm.turn_count, 3)
        self.assertEqual(sm.successful_turns, 3)
        self.assertEqual(sm.total_tokens.input, 18)
        self.assertEqual(sm.total_tokens.output, 168)
        self.assertAlmostEqual(sm.total_cost_usd or 0.0, 0.4318 * 3, places=4)
        self.assertEqual(sm.total_duration_ms, 42000 * 3)
        self.assertIn("claude", sm.by_agent)
        self.assertEqual(sm.by_agent["claude"].input, 18)

    def test_empty_session_returns_zero_metrics(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = _scaffold(Path(tmp), "s1")
            _write_view(repo, "s1")
            sm = extract_session_metrics(repo, "s1")
        self.assertEqual(sm.turn_count, 0)
        self.assertEqual(sm.total_duration_ms, 0)
        self.assertIsNone(sm.total_cost_usd)
        self.assertIsNone(sm.total_tokens.total)

    def test_mixed_agent_session(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = _scaffold(Path(tmp), "s1")
            _write_view(repo, "s1")
            _write_turn(
                repo, "s1", 1,
                output_lines=[_claude_result_event()],
                state=_claude_turn_state(turn_number=1),
            )
            codex_state = _claude_turn_state(turn_number=2)
            codex_state["agent_key"] = "codex"
            _write_turn(
                repo, "s1", 2,
                output_lines=[
                    {"type": "result", "token_usage": {"input_tokens": 5, "output_tokens": 7}}
                ],
                state=codex_state,
            )
            sm = extract_session_metrics(repo, "s1")
        self.assertEqual(sm.turn_count, 2)
        self.assertEqual(sm.by_agent["claude"].input, 6)
        self.assertEqual(sm.by_agent["codex"].input, 5)
        # Cost only from claude.
        self.assertAlmostEqual(sm.total_cost_usd or 0.0, 0.4318)
        self.assertIn("claude", sm.cost_by_agent)
        self.assertNotIn("codex", sm.cost_by_agent)

    def test_failed_turns_counted_separately(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = _scaffold(Path(tmp), "s1")
            _write_view(repo, "s1")
            _write_turn(
                repo, "s1", 1,
                output_lines=[_claude_result_event()],
                state=_claude_turn_state(turn_number=1, status="continue"),
            )
            _write_turn(
                repo, "s1", 2,
                output_lines=[_claude_result_event()],
                state=_claude_turn_state(turn_number=2, status="error"),
            )
            sm = extract_session_metrics(repo, "s1")
        self.assertEqual(sm.turn_count, 2)
        self.assertEqual(sm.successful_turns, 1)


# ---------------------------------------------------------------------------
# extract_cross_session_metrics
# ---------------------------------------------------------------------------


class ExtractCrossSessionMetricsTests(TestCase):
    def test_no_sessions_returns_empty_metrics(self) -> None:
        with TemporaryDirectory() as tmp:
            cm = extract_cross_session_metrics(Path(tmp))
        self.assertEqual(cm.session_count, 0)
        self.assertEqual(cm.sessions, ())

    def test_aggregates_across_sessions(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            for sid in ("a", "b"):
                _scaffold(repo, sid)
                _write_view(repo, sid)
                _write_turn(
                    repo, sid, 1,
                    output_lines=[_claude_result_event()],
                    state=_claude_turn_state(),
                )
            with patch("agent_relay.metrics.is_session", return_value=True):
                cm = extract_cross_session_metrics(repo)
        self.assertEqual(cm.session_count, 2)
        self.assertEqual(cm.total_tokens.input, 12)
        self.assertAlmostEqual(cm.total_cost_usd or 0.0, 0.4318 * 2, places=4)
        self.assertIn("claude", cm.by_agent)

    def test_buckets_by_day(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _scaffold(repo, "a")
            _write_view(repo, "a")
            day1_state = _claude_turn_state()
            day1_state["metadata"]["started_at"] = "2026-05-01T10:00:00.000Z"
            day1_state["metadata"]["finished_at"] = "2026-05-01T10:00:42.000Z"
            day2_state = _claude_turn_state(turn_number=2)
            day2_state["metadata"]["started_at"] = "2026-05-02T11:00:00.000Z"
            day2_state["metadata"]["finished_at"] = "2026-05-02T11:00:42.000Z"
            _write_turn(repo, "a", 1, output_lines=[_claude_result_event()], state=day1_state)
            _write_turn(repo, "a", 2, output_lines=[_claude_result_event()], state=day2_state)
            with patch("agent_relay.metrics.is_session", return_value=True):
                cm = extract_cross_session_metrics(repo)
        self.assertIn("2026-05-01", cm.by_day)
        self.assertIn("2026-05-02", cm.by_day)
        self.assertEqual(cm.by_day["2026-05-01"].input, 6)
        self.assertEqual(cm.by_day["2026-05-02"].input, 6)

    def test_agent_filter_excludes_unmatched_sessions(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _scaffold(repo, "claude-only")
            _write_view(repo, "claude-only")
            _write_turn(
                repo, "claude-only", 1,
                output_lines=[_claude_result_event()],
                state=_claude_turn_state(),
            )
            _scaffold(repo, "codex-only")
            _write_view(repo, "codex-only", current_agent="codex")
            codex_state = _claude_turn_state()
            codex_state["agent_key"] = "codex"
            _write_turn(
                repo, "codex-only", 1,
                output_lines=[
                    {"type": "result", "token_usage": {"input_tokens": 9}}
                ],
                state=codex_state,
            )
            with patch("agent_relay.metrics.is_session", return_value=True):
                cm = extract_cross_session_metrics(repo, agents=["codex"])
        self.assertEqual(cm.session_count, 1)
        self.assertEqual(cm.sessions[0].session_id, "codex-only")
        self.assertNotIn("claude", cm.by_agent)

    def test_skips_dirs_that_are_not_sessions(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _scaffold(repo, "real")
            _write_view(repo, "real")
            _write_turn(
                repo, "real", 1,
                output_lines=[_claude_result_event()],
                state=_claude_turn_state(),
            )
            # Decoy non-session dir
            _scaffold(repo, "fake")
            with patch(
                "agent_relay.metrics.is_session",
                side_effect=lambda repo_root, sid: sid == "real",
            ):
                cm = extract_cross_session_metrics(repo)
        self.assertEqual(cm.session_count, 1)
        self.assertEqual(cm.sessions[0].session_id, "real")
