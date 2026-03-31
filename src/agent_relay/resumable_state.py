from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any


RESUMABLE_STATE_SCHEMA_VERSION = 1
RESUMABLE_STATE_KIND = "relay_resumable_state"
_LIST_FIELDS = (
    "current_plan",
    "assumptions",
    "blockers",
    "intended_edits",
    "remaining_work",
    "verification",
)
_STRING_FIELDS = (
    "source",
    "objective",
    "summary",
    "status",
    "reason",
    "next_step",
    "agent_key",
    "agent_display_name",
    "captured_at",
)


def normalize_resumable_state(value: Any, *, source: str | None = None) -> dict[str, Any] | None:
    mapping: Mapping[str, Any]
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            loaded = json.loads(stripped)
        except json.JSONDecodeError:
            mapping = {"summary": stripped}
        else:
            if not isinstance(loaded, Mapping):
                return None
            mapping = loaded
    elif isinstance(value, Mapping):
        mapping = value
    else:
        return None

    normalized: dict[str, Any] = {
        "schema_version": RESUMABLE_STATE_SCHEMA_VERSION,
        "kind": RESUMABLE_STATE_KIND,
        "source": source or _optional_string(mapping.get("source")) or "unknown",
    }

    for field_name in _STRING_FIELDS[1:]:
        normalized_value = _optional_string(mapping.get(field_name))
        if normalized_value is not None:
            normalized[field_name] = normalized_value

    turn_number = mapping.get("turn_number")
    if isinstance(turn_number, int):
        normalized["turn_number"] = turn_number

    for field_name in _LIST_FIELDS:
        values = _string_list(mapping.get(field_name))
        if values:
            normalized[field_name] = values

    metadata = mapping.get("metadata")
    if isinstance(metadata, Mapping):
        normalized["metadata"] = json.loads(json.dumps(metadata, sort_keys=True))

    return normalized


def resumable_state_text(value: Mapping[str, Any]) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def load_resumable_state_text(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return normalize_resumable_state(data)


def _optional_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped if stripped else None


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    if not isinstance(value, Sequence):
        return []
    values: list[str] = []
    for item in value:
        if isinstance(item, str):
            stripped = item.strip()
            if stripped:
                values.append(stripped)
    return values
