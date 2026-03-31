from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_relay.agents import get_agent_adapter, get_agent_display_name
from agent_relay.resumable_state import normalize_resumable_state, resumable_state_text


@dataclass(frozen=True, slots=True)
class ProviderCaptureResult:
    source_agent: str
    hook_name: str | None = None
    resumable_state: str | None = None
    planning_snapshot: str | None = None
    proposed_edits: str | None = None
    transcript: str | None = None
    session_metadata: str | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)


def capture_provider_state(
    repo_root: Path,
    *,
    agent_key: str,
    session_id: str,
) -> ProviderCaptureResult:
    adapter = get_agent_adapter(agent_key)
    spec = adapter.render_capture_hook_spec(repo_root, session_id)
    if spec is None:
        return ProviderCaptureResult(source_agent=agent_key)

    try:
        completed = subprocess.run(
            spec.command,
            cwd=spec.cwd,
            shell=True,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        return ProviderCaptureResult(
            source_agent=agent_key,
            hook_name=spec.hook_name,
            warnings=(f"{get_agent_display_name(agent_key)} capture hook could not start: {exc}",),
        )

    if completed.returncode != 0:
        error = completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"
        return ProviderCaptureResult(
            source_agent=agent_key,
            hook_name=spec.hook_name,
            warnings=(f"{get_agent_display_name(agent_key)} capture hook failed: {error}",),
        )

    payload_text = completed.stdout.strip()
    if not payload_text:
        return ProviderCaptureResult(
            source_agent=agent_key,
            hook_name=spec.hook_name,
            warnings=(f"{get_agent_display_name(agent_key)} capture hook returned no data.",),
        )

    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        return ProviderCaptureResult(
            source_agent=agent_key,
            hook_name=spec.hook_name,
            warnings=(f"{get_agent_display_name(agent_key)} capture hook returned invalid JSON: {exc}",),
        )

    if not isinstance(payload, dict):
        return ProviderCaptureResult(
            source_agent=agent_key,
            hook_name=spec.hook_name,
            warnings=(f"{get_agent_display_name(agent_key)} capture hook returned an unexpected payload shape.",),
        )

    resumable_state = _normalize_resumable_state_text(payload.get("resumable_state"))
    planning_snapshot = _optional_text(payload.get("planning_snapshot"))
    proposed_edits = _optional_text(payload.get("proposed_edits"))
    transcript = _optional_text(payload.get("transcript"))
    session_metadata = _normalize_session_metadata(payload.get("session_metadata"))
    warnings = _normalize_warnings(payload.get("warnings"))

    return ProviderCaptureResult(
        source_agent=agent_key,
        hook_name=spec.hook_name,
        resumable_state=resumable_state,
        planning_snapshot=planning_snapshot,
        proposed_edits=proposed_edits,
        transcript=transcript,
        session_metadata=session_metadata,
        warnings=warnings,
    )


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else None
    return None


def _normalize_session_metadata(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else None
    if isinstance(value, (dict, list)):
        return json.dumps(value, indent=2, sort_keys=True)
    return str(value).strip() or None


def _normalize_warnings(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        stripped = value.strip()
        return (stripped,) if stripped else ()
    if isinstance(value, list):
        warnings: list[str] = []
        for item in value:
            if isinstance(item, str):
                stripped = item.strip()
                if stripped:
                    warnings.append(stripped)
        return tuple(warnings)
    return ()


def _normalize_resumable_state_text(value: Any) -> str | None:
    normalized = normalize_resumable_state(value, source="provider_export")
    if normalized is None:
        return None
    return resumable_state_text(normalized).rstrip()
