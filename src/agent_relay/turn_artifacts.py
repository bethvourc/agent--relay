"""Best-effort readers for per-turn dashboard artifacts."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_relay.layout import turn_dir

_MAX_TOOL_FIELD_CHARS = 4000


@dataclass(frozen=True, slots=True)
class ToolCall:
    name: str
    arguments: str
    result: str
    is_error: bool


@dataclass(frozen=True, slots=True)
class TurnArtifacts:
    session_id: str
    turn_number: int
    prompt: str | None
    output_text: str | None
    state: dict[str, Any] | None
    tool_calls: tuple[ToolCall, ...]
    raw_jsonl: tuple[str, ...]


def load_turn_artifacts(repo_root: Path, session_id: str, turn_number: int) -> TurnArtifacts:
    """Load one turn's human-facing artifacts without raising on bad data."""
    directory = turn_dir(repo_root, session_id, turn_number)
    prompt = _read_text(directory / "prompt.md")
    output_text = _read_text(directory / "output.txt")
    raw_jsonl = _read_lines(directory / "output.jsonl")
    events = _parse_jsonl(raw_jsonl)
    state = _read_json_object(directory / "state.json")

    if output_text is None:
        output_text = _derive_output_text(events)

    return TurnArtifacts(
        session_id=session_id,
        turn_number=turn_number,
        prompt=prompt,
        output_text=output_text,
        state=state,
        tool_calls=_extract_tool_calls(events),
        raw_jsonl=raw_jsonl,
    )


def _read_text(path: Path) -> str | None:
    try:
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _read_lines(path: Path) -> tuple[str, ...]:
    try:
        if not path.exists():
            return tuple()
        return tuple(path.read_text(encoding="utf-8").splitlines())
    except OSError:
        return tuple()


def _read_json_object(path: Path) -> dict[str, Any] | None:
    text = _read_text(path)
    if text is None:
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return dict(value) if isinstance(value, Mapping) else None


def _parse_jsonl(lines: Iterable[str]) -> tuple[dict[str, Any], ...]:
    out: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            out.append(value)
    return tuple(out)


def _derive_output_text(events: Iterable[Mapping[str, Any]]) -> str | None:
    parts: list[str] = []
    for event in events:
        if event.get("type") == "text":
            text = event.get("text")
            if isinstance(text, str):
                parts.append(text)
        for block in _content_blocks(event):
            if block.get("type") != "text":
                continue
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    text = "\n".join(part for part in parts if part)
    return text or None


@dataclass(slots=True)
class _ToolUse:
    key: str
    name: str
    arguments: str
    result: str = ""
    is_error: bool = False


def _extract_tool_calls(events: Iterable[Mapping[str, Any]]) -> tuple[ToolCall, ...]:
    uses: list[_ToolUse] = []
    by_key: dict[str, _ToolUse] = {}
    anonymous_index = 0

    for event in events:
        for block in _event_and_content_blocks(event):
            block_type = _string_field(block, "type")
            if block_type == "tool_use":
                anonymous_index += 1
                key = (
                    _string_field(block, "id")
                    or _string_field(block, "tool_use_id")
                    or _string_field(block, "call_id")
                    or f"anonymous-{anonymous_index}"
                )
                call = _ToolUse(
                    key=key,
                    name=_string_field(block, "name")
                    or _string_field(block, "tool_name")
                    or "unknown",
                    arguments=_truncate(_stringify(block.get("input", block.get("arguments")))),
                )
                uses.append(call)
                by_key[key] = call
            elif block_type == "tool_result":
                key = (
                    _string_field(block, "tool_use_id")
                    or _string_field(block, "id")
                    or _string_field(block, "call_id")
                )
                call = by_key.get(key or "")
                if call is None:
                    continue
                call.result = _truncate(_stringify(block.get("content", block.get("result"))))
                call.is_error = bool(block.get("is_error") or block.get("error"))

    return tuple(
        ToolCall(
            name=call.name,
            arguments=call.arguments,
            result=call.result,
            is_error=call.is_error,
        )
        for call in uses
    )


def _event_and_content_blocks(event: Mapping[str, Any]):
    yield event
    yield from _content_blocks(event)


def _content_blocks(event: Mapping[str, Any]):
    message = event.get("message")
    if isinstance(message, Mapping):
        content = message.get("content")
        yield from _iter_mapping_list(content)
    content = event.get("content")
    yield from _iter_mapping_list(content)


def _iter_mapping_list(value: Any):
    if isinstance(value, list):
        for item in value:
            if isinstance(item, Mapping):
                yield item


def _string_field(mapping: Mapping[str, Any], key: str) -> str | None:
    value = mapping.get(key)
    return value if isinstance(value, str) and value else None


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        text_parts = [
            item.get("text")
            for item in value
            if isinstance(item, Mapping) and isinstance(item.get("text"), str)
        ]
        if text_parts:
            return "\n".join(text_parts)
    try:
        return json.dumps(value, indent=2, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def _truncate(value: str) -> str:
    if len(value) <= _MAX_TOOL_FIELD_CHARS:
        return value
    return value[:_MAX_TOOL_FIELD_CHARS] + "…"


__all__ = [
    "ToolCall",
    "TurnArtifacts",
    "load_turn_artifacts",
]
