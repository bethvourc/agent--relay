from __future__ import annotations

from pathlib import Path
from typing import Callable

from agent_relay.converse import ConverseResult, TurnResult, converse


def run_session(
    repo_root: Path,
    *,
    agent: str,
    task: str,
    max_turns: int = 10,
    continue_from_session_id: str | None = None,
    owner: str = "cli:run",
    on_turn_start: Callable[[str, int, int], None] | None = None,
    on_turn_complete: Callable[[TurnResult], None] | None = None,
) -> ConverseResult:
    """Run a single agent in a Relay-managed session."""
    return converse(
        repo_root,
        agents=(agent,),
        task=task,
        max_turns=max_turns,
        continue_from_session_id=continue_from_session_id,
        owner=owner,
        on_turn_start=on_turn_start,
        on_turn_complete=on_turn_complete,
    )
