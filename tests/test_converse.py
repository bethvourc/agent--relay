"""Tests for the converse orchestrator module."""
from __future__ import annotations

import json
from unittest import TestCase

from agent_relay.converse import (
    TurnResult,
    build_turn_prompt,
    detect_done_signal,
    normalize_claude_output,
    normalize_codex_output,
    _make_summary,
    _normalize_output,
    _strip_done_marker,
)


class NormalizeClaudeOutputTests(TestCase):
    def test_extracts_text_from_stream_json(self) -> None:
        lines = [
            json.dumps({"message": {"role": "assistant", "content": [{"type": "text", "text": "Hello from Claude"}]}}),
            json.dumps({"message": {"role": "assistant", "content": [{"type": "text", "text": "Second message"}]}}),
        ]
        raw = "\n".join(lines)
        result = normalize_claude_output(raw)
        self.assertIn("Hello from Claude", result)
        self.assertIn("Second message", result)

    def test_ignores_non_assistant_messages(self) -> None:
        lines = [
            json.dumps({"message": {"role": "user", "content": [{"type": "text", "text": "user input"}]}}),
            json.dumps({"message": {"role": "assistant", "content": [{"type": "text", "text": "assistant reply"}]}}),
        ]
        raw = "\n".join(lines)
        result = normalize_claude_output(raw)
        self.assertNotIn("user input", result)
        self.assertIn("assistant reply", result)

    def test_ignores_tool_use_blocks(self) -> None:
        lines = [
            json.dumps({"message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {}},
                {"type": "text", "text": "I read the file"},
            ]}}),
        ]
        raw = "\n".join(lines)
        result = normalize_claude_output(raw)
        self.assertIn("I read the file", result)

    def test_falls_back_to_raw_on_no_matches(self) -> None:
        raw = "just some raw text"
        result = normalize_claude_output(raw)
        self.assertEqual(result, "just some raw text")

    def test_handles_empty_input(self) -> None:
        result = normalize_claude_output("")
        self.assertEqual(result, "")

    def test_skips_invalid_json_lines(self) -> None:
        lines = [
            "not json",
            json.dumps({"message": {"role": "assistant", "content": [{"type": "text", "text": "valid"}]}}),
        ]
        raw = "\n".join(lines)
        result = normalize_claude_output(raw)
        self.assertIn("valid", result)

    def test_handles_flat_assistant_message(self) -> None:
        """Claude sometimes emits events where 'role' and 'content' are at the top level."""
        raw = json.dumps({"role": "assistant", "content": [{"type": "text", "text": "flat reply"}]})
        result = normalize_claude_output(raw)
        self.assertIn("flat reply", result)


class NormalizeCodexOutputTests(TestCase):
    def test_extracts_item_completed_agent_message(self) -> None:
        lines = [
            json.dumps({"type": "thread.started", "thread_id": "abc"}),
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "Hello from Codex"}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100}}),
        ]
        raw = "\n".join(lines)
        result = normalize_codex_output(raw)
        self.assertEqual(result, "Hello from Codex")

    def test_ignores_non_agent_message_items(self) -> None:
        lines = [
            json.dumps({"type": "item.completed", "item": {"id": "item_0", "type": "tool_call", "text": "ignored"}}),
            json.dumps({"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": "keep this"}}),
        ]
        raw = "\n".join(lines)
        result = normalize_codex_output(raw)
        self.assertEqual(result, "keep this")

    def test_extracts_message_with_content_blocks(self) -> None:
        lines = [
            json.dumps({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Codex says hi"}]}),
        ]
        raw = "\n".join(lines)
        result = normalize_codex_output(raw)
        self.assertIn("Codex says hi", result)

    def test_falls_back_to_raw(self) -> None:
        raw = "raw codex output"
        result = normalize_codex_output(raw)
        self.assertEqual(result, "raw codex output")


class NormalizeOutputDispatchTests(TestCase):
    def test_dispatches_to_claude(self) -> None:
        raw = json.dumps({"role": "assistant", "content": [{"type": "text", "text": "claude"}]})
        result = _normalize_output("claude", raw)
        self.assertIn("claude", result)

    def test_dispatches_to_codex(self) -> None:
        raw = json.dumps({"type": "message", "role": "assistant", "content": [{"type": "text", "text": "codex"}]})
        result = _normalize_output("codex", raw)
        self.assertIn("codex", result)

    def test_unknown_agent_returns_raw(self) -> None:
        result = _normalize_output("unknown", "raw text")
        self.assertEqual(result, "raw text")


class DetectDoneSignalTests(TestCase):
    def test_detects_marker(self) -> None:
        self.assertTrue(detect_done_signal("Task is done. CONVERSATION_COMPLETE"))

    def test_case_insensitive(self) -> None:
        self.assertTrue(detect_done_signal("conversation_complete"))

    def test_no_marker(self) -> None:
        self.assertFalse(detect_done_signal("Still working on it"))

    def test_empty_string(self) -> None:
        self.assertFalse(detect_done_signal(""))


class BuildTurnPromptTests(TestCase):
    def _make_turn(self, turn_number: int, agent_key: str, text: str) -> TurnResult:
        return TurnResult(
            turn_number=turn_number,
            agent_key=agent_key,
            exit_code=0,
            raw_stdout="",
            raw_stderr="",
            text=text,
            summary=text[:50],
            done_signal=False,
            started_at="2025-01-01T00:00:00Z",
            finished_at="2025-01-01T00:01:00Z",
        )

    def test_first_turn_has_task_and_no_history(self) -> None:
        from pathlib import Path
        prompt = build_turn_prompt(
            task="Fix the tests",
            turn_history=[],
            current_agent="claude",
            other_agent="codex",
            turn_number=1,
            repo_root=Path("/tmp/repo"),
        )
        self.assertIn("Fix the tests", prompt)
        self.assertIn("Claude Code", prompt)
        self.assertIn("Codex", prompt)
        self.assertIn("Turn 1", prompt)
        self.assertNotIn("Conversation so far", prompt)

    def test_subsequent_turn_includes_history(self) -> None:
        from pathlib import Path
        history = [
            self._make_turn(1, "claude", "I looked at the code and found the bug."),
        ]
        prompt = build_turn_prompt(
            task="Fix the tests",
            turn_history=history,
            current_agent="codex",
            other_agent="claude",
            turn_number=2,
            repo_root=Path("/tmp/repo"),
        )
        self.assertIn("Conversation so far", prompt)
        self.assertIn("Turn 1", prompt)
        self.assertIn("I looked at the code and found the bug.", prompt)
        self.assertIn("Turn 2", prompt)

    def test_includes_completion_instructions(self) -> None:
        from pathlib import Path
        prompt = build_turn_prompt(
            task="Do something",
            turn_history=[],
            current_agent="claude",
            other_agent="codex",
            turn_number=1,
            repo_root=Path("/tmp/repo"),
        )
        self.assertIn("CONVERSATION_COMPLETE", prompt)


class StripDoneMarkerTests(TestCase):
    def test_strips_marker(self) -> None:
        self.assertEqual(_strip_done_marker("Hello CONVERSATION_COMPLETE"), "Hello")

    def test_strips_marker_case_insensitive(self) -> None:
        self.assertEqual(_strip_done_marker("Done conversation_complete"), "Done")

    def test_preserves_text_without_marker(self) -> None:
        self.assertEqual(_strip_done_marker("Just normal text"), "Just normal text")

    def test_strips_marker_from_middle(self) -> None:
        result = _strip_done_marker("Before CONVERSATION_COMPLETE after")
        self.assertEqual(result, "Before after")

    def test_empty_string(self) -> None:
        self.assertEqual(_strip_done_marker(""), "")


class MakeSummaryTests(TestCase):
    def test_takes_first_non_empty_line(self) -> None:
        text = "\n\nHello world\nMore text"
        self.assertEqual(_make_summary(text), "Hello world")

    def test_skips_headings(self) -> None:
        text = "# Heading\nActual content"
        self.assertEqual(_make_summary(text), "Actual content")

    def test_truncates_long_lines(self) -> None:
        text = "A" * 200
        summary = _make_summary(text, max_len=50)
        self.assertEqual(len(summary), 50)
        self.assertTrue(summary.endswith("..."))

    def test_empty_input(self) -> None:
        self.assertEqual(_make_summary(""), "(no output)")

    def test_only_headings(self) -> None:
        self.assertEqual(_make_summary("# Only a heading\n## Another"), "(no output)")
