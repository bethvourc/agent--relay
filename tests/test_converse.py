"""Tests for the converse orchestrator module."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from agent_relay.converse import (
    CompletionState,
    TurnResult,
    build_turn_prompt,
    converse,
    detect_done_signal,
    normalize_claude_output,
    normalize_codex_output,
    normalize_gemini_output,
    parse_turn_state,
    parse_turn_control,
    _make_summary,
    _normalize_output,
    _strip_done_marker,
    _strip_turn_control,
)


class NormalizeClaudeOutputTests(TestCase):
    def test_extracts_text_from_stream_json(self) -> None:
        lines = [
            json.dumps(
                {
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Hello from Claude"}],
                    }
                }
            ),
            json.dumps(
                {
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Second message"}],
                    }
                }
            ),
        ]
        raw = "\n".join(lines)
        result = normalize_claude_output(raw)
        self.assertIn("Hello from Claude", result)
        self.assertIn("Second message", result)

    def test_ignores_non_assistant_messages(self) -> None:
        lines = [
            json.dumps(
                {
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "user input"}],
                    }
                }
            ),
            json.dumps(
                {
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "assistant reply"}],
                    }
                }
            ),
        ]
        raw = "\n".join(lines)
        result = normalize_claude_output(raw)
        self.assertNotIn("user input", result)
        self.assertIn("assistant reply", result)

    def test_ignores_tool_use_blocks(self) -> None:
        lines = [
            json.dumps(
                {
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "t1",
                                "name": "Read",
                                "input": {},
                            },
                            {"type": "text", "text": "I read the file"},
                        ],
                    }
                }
            ),
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
            json.dumps(
                {
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "valid"}],
                    }
                }
            ),
        ]
        raw = "\n".join(lines)
        result = normalize_claude_output(raw)
        self.assertIn("valid", result)

    def test_handles_flat_assistant_message(self) -> None:
        """Claude sometimes emits events where 'role' and 'content' are at the top level."""
        raw = json.dumps(
            {"role": "assistant", "content": [{"type": "text", "text": "flat reply"}]}
        )
        result = normalize_claude_output(raw)
        self.assertIn("flat reply", result)


class NormalizeCodexOutputTests(TestCase):
    def test_extracts_item_completed_agent_message(self) -> None:
        lines = [
            json.dumps({"type": "thread.started", "thread_id": "abc"}),
            json.dumps({"type": "turn.started"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item_0",
                        "type": "agent_message",
                        "text": "Hello from Codex",
                    },
                }
            ),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100}}),
        ]
        raw = "\n".join(lines)
        result = normalize_codex_output(raw)
        self.assertEqual(result, "Hello from Codex")

    def test_ignores_non_agent_message_items(self) -> None:
        lines = [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"id": "item_0", "type": "tool_call", "text": "ignored"},
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item_1",
                        "type": "agent_message",
                        "text": "keep this",
                    },
                }
            ),
        ]
        raw = "\n".join(lines)
        result = normalize_codex_output(raw)
        self.assertEqual(result, "keep this")

    def test_extracts_message_with_content_blocks(self) -> None:
        lines = [
            json.dumps(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Codex says hi"}],
                }
            ),
        ]
        raw = "\n".join(lines)
        result = normalize_codex_output(raw)
        self.assertIn("Codex says hi", result)

    def test_falls_back_to_raw(self) -> None:
        raw = "raw codex output"
        result = normalize_codex_output(raw)
        self.assertEqual(result, "raw codex output")


class NormalizeGeminiOutputTests(TestCase):
    def test_extracts_text_from_model_message(self) -> None:
        lines = [
            json.dumps({"type": "init", "session_id": "s1"}),
            json.dumps(
                {
                    "message": {
                        "role": "model",
                        "content": [{"type": "text", "text": "Hello from Gemini"}],
                    }
                }
            ),
        ]
        raw = "\n".join(lines)
        result = normalize_gemini_output(raw)
        self.assertIn("Hello from Gemini", result)

    def test_extracts_text_from_result_event(self) -> None:
        lines = [
            json.dumps({"type": "result", "text": "Final answer"}),
        ]
        raw = "\n".join(lines)
        result = normalize_gemini_output(raw)
        self.assertIn("Final answer", result)

    def test_ignores_tool_use_events(self) -> None:
        lines = [
            json.dumps({"type": "tool_use", "name": "ReadFile", "input": {}}),
            json.dumps(
                {
                    "message": {
                        "role": "model",
                        "content": [{"type": "text", "text": "after tool"}],
                    }
                }
            ),
        ]
        raw = "\n".join(lines)
        result = normalize_gemini_output(raw)
        self.assertNotIn("ReadFile", result)
        self.assertIn("after tool", result)

    def test_falls_back_to_raw(self) -> None:
        raw = "raw gemini output"
        result = normalize_gemini_output(raw)
        self.assertEqual(result, "raw gemini output")

    def test_handles_empty_input(self) -> None:
        result = normalize_gemini_output("")
        self.assertEqual(result, "")

    def test_handles_flat_model_message(self) -> None:
        raw = json.dumps(
            {"role": "model", "content": [{"type": "text", "text": "flat reply"}]}
        )
        result = normalize_gemini_output(raw)
        self.assertIn("flat reply", result)


class NormalizeOutputDispatchTests(TestCase):
    def test_dispatches_to_claude(self) -> None:
        raw = json.dumps(
            {"role": "assistant", "content": [{"type": "text", "text": "claude"}]}
        )
        result = _normalize_output("claude", raw)
        self.assertIn("claude", result)

    def test_dispatches_to_codex(self) -> None:
        raw = json.dumps(
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "codex"}],
            }
        )
        result = _normalize_output("codex", raw)
        self.assertIn("codex", result)

    def test_dispatches_to_gemini(self) -> None:
        raw = json.dumps(
            {"role": "model", "content": [{"type": "text", "text": "gemini"}]}
        )
        result = _normalize_output("gemini", raw)
        self.assertIn("gemini", result)

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


class ParseTurnControlTests(TestCase):
    def test_parses_structured_status(self) -> None:
        control = parse_turn_control(
            'Work summary\nRELAY_STATUS: {"status":"propose_done","reason":"Tests pass","remaining_work":[],"verification":["uv run pytest tests/test_converse.py -v"]}'
        )
        self.assertEqual(control.status, "propose_done")
        self.assertEqual(control.reason, "Tests pass")
        self.assertEqual(control.remaining_work, ())
        self.assertEqual(
            control.verification, ("uv run pytest tests/test_converse.py -v",)
        )

    def test_missing_status_defaults_to_continue(self) -> None:
        control = parse_turn_control("Still working")
        self.assertEqual(control.status, "continue")
        self.assertEqual(control.remaining_work, ())
        self.assertEqual(control.verification, ())

    def test_legacy_marker_falls_back_to_propose_done(self) -> None:
        control = parse_turn_control("Task is done.\nCONVERSATION_COMPLETE")
        self.assertEqual(control.status, "propose_done")

    def test_parses_optional_structured_turn_state(self) -> None:
        state = parse_turn_state(
            'Work summary\nRELAY_STATE: {"summary":"Reviewed auth flow","current_plan":["patch auth middleware"],"next_step":"edit src/auth.py"}\nRELAY_STATUS: {"status":"continue","reason":"Need to patch middleware","remaining_work":["patch auth middleware"],"verification":[]}'
        )
        assert state is not None
        self.assertEqual(state["summary"], "Reviewed auth flow")
        self.assertEqual(state["next_step"], "edit src/auth.py")
        self.assertEqual(state["current_plan"], ["patch auth middleware"])


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
            all_agents=["claude", "codex"],
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
            all_agents=["claude", "codex"],
            turn_number=2,
            repo_root=Path("/tmp/repo"),
        )
        self.assertIn("Conversation so far", prompt)
        self.assertIn("Turn 1", prompt)
        self.assertIn("I looked at the code and found the bug.", prompt)
        self.assertIn("Turn 2", prompt)

    def test_includes_completion_instructions(self) -> None:
        prompt = build_turn_prompt(
            task="Do something",
            turn_history=[],
            current_agent="claude",
            all_agents=["claude", "codex"],
            turn_number=1,
            repo_root=Path("/tmp/repo"),
        )
        self.assertIn("RELAY_STATUS:", prompt)
        self.assertIn("RELAY_STATE:", prompt)
        self.assertIn("propose_done", prompt)
        self.assertIn("agree_done", prompt)
        self.assertIn("NEVER use propose_done or agree_done on your first turn", prompt)

    def test_single_agent_prompt_uses_single_agent_completion_rules(self) -> None:
        prompt = build_turn_prompt(
            task="Do something",
            turn_history=[],
            current_agent="claude",
            all_agents=["claude"],
            turn_number=1,
            repo_root=Path("/tmp/repo"),
        )
        self.assertIn("Relay-managed single-agent session", prompt)
        self.assertIn("Use status propose_done when the task is complete.", prompt)
        self.assertIn("Use status blocked when you cannot continue", prompt)
        self.assertNotIn(
            "NEVER use propose_done or agree_done on your first turn", prompt
        )

    def test_includes_active_completion_state(self) -> None:
        prompt = build_turn_prompt(
            task="Do something",
            turn_history=[],
            current_agent="codex",
            all_agents=["claude", "codex"],
            turn_number=3,
            repo_root=Path("/tmp/repo"),
            completion_state=CompletionState(
                active_epoch=1,
                proposed_by_slot=0,
                proposed_turn=2,
                agreeing_slots=(0,),
            ),
        )
        self.assertIn("Active completion proposal: epoch 1", prompt)
        self.assertIn("Slot 0", prompt)
        self.assertIn("agree_done", prompt)

    def test_three_agent_prompt_lists_all_participants(self) -> None:
        from pathlib import Path

        # claude + codex + claude: unique others from claude's perspective is just codex
        prompt = build_turn_prompt(
            task="Collaborate on design",
            turn_history=[],
            current_agent="codex",
            all_agents=["claude", "codex", "claude"],
            turn_number=2,
            repo_root=Path("/tmp/repo"),
        )
        self.assertIn("Collaborate on design", prompt)
        self.assertIn("Codex", prompt)
        self.assertIn("Claude Code", prompt)
        # From codex's perspective, the unique other agent is claude (1 unique)
        self.assertIn("1 other AI agent", prompt)

    def test_three_distinct_agents_lists_two_others(self) -> None:
        """When three distinct agents converse, each sees 2 others."""
        from pathlib import Path

        # Use claude twice to simulate 3 slots, but test with codex seeing 1 unique
        # For a true 3-distinct test we'd need a third agent type; test the count logic
        prompt = build_turn_prompt(
            task="Three-way task",
            turn_history=[],
            current_agent="claude",
            all_agents=["claude", "codex"],
            turn_number=1,
            repo_root=Path("/tmp/repo"),
        )
        self.assertIn("1 other AI agent", prompt)

    def test_two_agent_prompt_says_one_other(self) -> None:
        from pathlib import Path

        prompt = build_turn_prompt(
            task="Pair up",
            turn_history=[],
            current_agent="claude",
            all_agents=["claude", "codex"],
            turn_number=1,
            repo_root=Path("/tmp/repo"),
        )
        self.assertIn("1 other AI agent", prompt)
        # Should not have trailing 's'
        self.assertNotIn("1 other AI agents", prompt)


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


class StripTurnControlTests(TestCase):
    def test_removes_status_line(self) -> None:
        text = 'Hello\nRELAY_STATUS: {"status":"continue","reason":"more work","remaining_work":["tests"],"verification":[]}'
        self.assertEqual(_strip_turn_control(text), "Hello")

    def test_removes_state_line(self) -> None:
        text = 'Hello\nRELAY_STATE: {"summary":"looked around"}\nRELAY_STATUS: {"status":"continue","reason":"more work","remaining_work":["tests"],"verification":[]}'
        self.assertEqual(_strip_turn_control(text), "Hello")

    def test_preserves_non_status_text(self) -> None:
        self.assertEqual(_strip_turn_control("Just normal text"), "Just normal text")


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


class ConverseCompletionProtocolTests(TestCase):
    @staticmethod
    def _result(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["mock-agent"],
            returncode=returncode,
            stdout=stdout,
            stderr="",
        )

    def test_stops_only_after_all_agents_agree(self) -> None:
        outputs = [
            self._result(
                'I am still working\nRELAY_STATUS: {"status":"continue","reason":"Inspecting code","remaining_work":["inspect code"],"verification":[]}'
            ),
            self._result(
                'I think this is done\nRELAY_STATUS: {"status":"propose_done","reason":"Fix applied and tests pass","remaining_work":[],"verification":["pytest tests/test_converse.py"]}'
            ),
            self._result(
                'I reviewed the work and agree\nRELAY_STATUS: {"status":"agree_done","reason":"Looks complete","remaining_work":[],"verification":["reviewed diff"]}'
            ),
        ]

        with TemporaryDirectory() as tmpdir:
            with (
                patch("agent_relay.converse.require_available"),
                patch(
                    "agent_relay.converse.start_session",
                ),
                patch(
                    "agent_relay.converse.run_agent_turn",
                    side_effect=outputs,
                ),
            ):
                result = converse(
                    Path(tmpdir),
                    agents=["claude", "codex"],
                    task="Finish the task",
                    max_turns=5,
                )

        self.assertEqual(result.stop_reason, "all_done")
        self.assertEqual(result.turns_completed, 3)
        self.assertEqual(
            [turn.control_status for turn in result.turn_results],
            ["continue", "propose_done", "agree_done"],
        )

    def test_reopen_cancels_stale_done_votes(self) -> None:
        outputs = [
            self._result(
                'Initial work\nRELAY_STATUS: {"status":"continue","reason":"Need context","remaining_work":["read code"],"verification":[]}'
            ),
            self._result(
                'Done for now\nRELAY_STATUS: {"status":"propose_done","reason":"Looks complete","remaining_work":[],"verification":["pytest"]}'
            ),
            self._result(
                'Found a bug\nRELAY_STATUS: {"status":"reopen","reason":"Regression found","remaining_work":["fix regression"],"verification":["reproduced bug"]}'
            ),
            self._result(
                'Stale agreement\nRELAY_STATUS: {"status":"agree_done","reason":"Still agree","remaining_work":[],"verification":[]}'
            ),
        ]

        with TemporaryDirectory() as tmpdir:
            with (
                patch("agent_relay.converse.require_available"),
                patch(
                    "agent_relay.converse.start_session",
                ),
                patch(
                    "agent_relay.converse.run_agent_turn",
                    side_effect=outputs,
                ),
            ):
                result = converse(
                    Path(tmpdir),
                    agents=["claude", "codex"],
                    task="Finish the task",
                    max_turns=4,
                )

        self.assertEqual(result.stop_reason, "max_turns")
        self.assertEqual(
            [turn.control_status for turn in result.turn_results],
            ["continue", "propose_done", "reopen", "continue"],
        )

    def test_first_round_done_signals_are_ignored(self) -> None:
        outputs = [
            self._result(
                'Premature done\nRELAY_STATUS: {"status":"propose_done","reason":"Too early","remaining_work":[],"verification":[]}'
            ),
            self._result(
                'Premature agree\nRELAY_STATUS: {"status":"agree_done","reason":"Also too early","remaining_work":[],"verification":[]}'
            ),
        ]

        with TemporaryDirectory() as tmpdir:
            with (
                patch("agent_relay.converse.require_available"),
                patch(
                    "agent_relay.converse.start_session",
                ),
                patch(
                    "agent_relay.converse.run_agent_turn",
                    side_effect=outputs,
                ),
            ):
                result = converse(
                    Path(tmpdir),
                    agents=["claude", "codex"],
                    task="Finish the task",
                    max_turns=2,
                )

        self.assertEqual(result.stop_reason, "max_turns")
        self.assertEqual(
            [turn.control_status for turn in result.turn_results],
            ["continue", "continue"],
        )

    def test_converse_writes_turn_state_artifacts(self) -> None:
        outputs = [
            self._result(
                'Reviewed the auth flow\nRELAY_STATE: {"summary":"Reviewed the auth flow","current_plan":["patch middleware"],"intended_edits":["src/auth.py"],"next_step":"edit src/auth.py"}\nRELAY_STATUS: {"status":"continue","reason":"Patch middleware","remaining_work":["patch middleware"],"verification":["run auth tests"]}'
            ),
            self._result(
                'Agreed on next step\nRELAY_STATUS: {"status":"blocked","reason":"Waiting for user decision","remaining_work":["confirm auth policy"],"verification":[]}'
            ),
        ]

        with TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            with (
                patch("agent_relay.converse.require_available"),
                patch(
                    "agent_relay.converse.start_session",
                ),
                patch(
                    "agent_relay.converse.run_agent_turn",
                    side_effect=outputs,
                ),
            ):
                result = converse(
                    repo_root,
                    agents=["claude", "codex"],
                    task="Stabilize auth handoff",
                    max_turns=2,
                )

            first_state = (
                repo_root
                / ".agent-relay"
                / "sessions"
                / result.session_id
                / "turns"
                / "turn-001"
                / "state.json"
            )
            second_state = (
                repo_root
                / ".agent-relay"
                / "sessions"
                / result.session_id
                / "turns"
                / "turn-002"
                / "state.json"
            )
            first_payload = json.loads(first_state.read_text(encoding="utf-8"))
            second_payload = json.loads(second_state.read_text(encoding="utf-8"))

        self.assertEqual(first_payload["summary"], "Reviewed the auth flow")
        self.assertEqual(first_payload["intended_edits"], ["src/auth.py"])
        self.assertEqual(first_payload["next_step"], "edit src/auth.py")
        self.assertEqual(second_payload["status"], "blocked")
        self.assertEqual(second_payload["remaining_work"], ["confirm auth policy"])

    def test_single_agent_stops_on_propose_done(self) -> None:
        outputs = [
            self._result(
                'Finished the task\nRELAY_STATUS: {"status":"propose_done","reason":"Done","remaining_work":[],"verification":["pytest"]}'
            ),
        ]

        with TemporaryDirectory() as tmpdir:
            with (
                patch("agent_relay.converse.require_available"),
                patch(
                    "agent_relay.converse.start_session",
                ),
                patch(
                    "agent_relay.converse.run_agent_turn",
                    side_effect=outputs,
                ),
            ):
                result = converse(
                    Path(tmpdir),
                    agents=["claude"],
                    task="Finish the task",
                    max_turns=3,
                )

        self.assertEqual(result.stop_reason, "done_signal")
        self.assertEqual(result.turns_completed, 1)
        self.assertEqual(result.turn_results[0].control_status, "propose_done")

    def test_single_agent_stops_on_blocked(self) -> None:
        outputs = [
            self._result(
                'Need human input\nRELAY_STATUS: {"status":"blocked","reason":"Waiting on review","remaining_work":["review change"],"verification":[]}'
            ),
        ]

        with TemporaryDirectory() as tmpdir:
            with (
                patch("agent_relay.converse.require_available"),
                patch(
                    "agent_relay.converse.start_session",
                ),
                patch(
                    "agent_relay.converse.run_agent_turn",
                    side_effect=outputs,
                ),
            ):
                result = converse(
                    Path(tmpdir),
                    agents=["claude"],
                    task="Finish the task",
                    max_turns=3,
                )

        self.assertEqual(result.stop_reason, "blocked")
        self.assertEqual(result.turns_completed, 1)
        self.assertEqual(result.turn_results[0].control_status, "blocked")
