"""Compute-on-read metrics extractor for sessions.

Parses per-turn artifacts (turns/turn-NNN/output.jsonl + state.json) and
journal events to produce typed token / cost / latency rollups.

The extractor is deliberately tolerant: any missing field becomes None and
no shape mismatch ever raises. Phase 2 surfaces (CLI, exporters, alerts,
watch panel) are all thin renderers on top of these dataclasses.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from agent_relay.layout import (
    derived_view_path,
    sessions_root,
    turn_dir,
    turns_dir,
)
from agent_relay.storage import is_session

_FAILURE_STATUSES = frozenset({"error", "failed", "interrupted", "timeout"})
_KNOWN_AGENTS = ("claude", "codex", "gemini")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input: int | None = None
    output: int | None = None
    cache_read: int | None = None
    cache_creation: int | None = None

    @property
    def total(self) -> int | None:
        parts = [
            v
            for v in (self.input, self.output, self.cache_read, self.cache_creation)
            if v is not None
        ]
        return sum(parts) if parts else None

    def to_dict(self) -> dict[str, int | None]:
        return {
            "input": self.input,
            "output": self.output,
            "cache_read": self.cache_read,
            "cache_creation": self.cache_creation,
            "total": self.total,
        }

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input=_add_optional(self.input, other.input),
            output=_add_optional(self.output, other.output),
            cache_read=_add_optional(self.cache_read, other.cache_read),
            cache_creation=_add_optional(self.cache_creation, other.cache_creation),
        )


@dataclass(frozen=True, slots=True)
class TurnMetrics:
    session_id: str
    turn_number: int
    agent: str
    model: str | None
    started_at: str | None
    finished_at: str | None
    duration_ms: int | None
    api_duration_ms: int | None
    tokens: TokenUsage
    cost_usd: float | None
    tool_calls: int
    status: str | None
    succeeded: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "turn_number": self.turn_number,
            "agent": self.agent,
            "model": self.model,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "api_duration_ms": self.api_duration_ms,
            "tokens": self.tokens.to_dict(),
            "cost_usd": self.cost_usd,
            "tool_calls": self.tool_calls,
            "status": self.status,
            "succeeded": self.succeeded,
        }


@dataclass(frozen=True, slots=True)
class SessionMetrics:
    session_id: str
    current_agent: str
    current_status: str
    objective: str | None
    started_at: str | None
    updated_at: str | None
    turn_count: int
    successful_turns: int
    total_tokens: TokenUsage
    total_cost_usd: float | None
    total_duration_ms: int
    by_agent: dict[str, TokenUsage] = field(default_factory=dict)
    cost_by_agent: dict[str, float] = field(default_factory=dict)
    turns: tuple[TurnMetrics, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "current_agent": self.current_agent,
            "current_status": self.current_status,
            "objective": self.objective,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "turn_count": self.turn_count,
            "successful_turns": self.successful_turns,
            "total_tokens": self.total_tokens.to_dict(),
            "total_cost_usd": self.total_cost_usd,
            "total_duration_ms": self.total_duration_ms,
            "by_agent": {k: v.to_dict() for k, v in self.by_agent.items()},
            "cost_by_agent": dict(self.cost_by_agent),
            "turns": [t.to_dict() for t in self.turns],
        }


@dataclass(frozen=True, slots=True)
class MetricsFilter:
    """Single source of truth for metric scoping.

    Built from CLI flags (``cmd_metrics``) or query strings (dashboard
    handler) and consumed by :func:`extract_cross_session_metrics`. Empty
    fields mean "no constraint"; ``is_identity`` returns True when the
    filter is fully empty (the extractor can skip filtering work).

    Future fields (``until``, ``q``, ``session_ids``) will plug in here
    without touching every call site.
    """

    since: datetime | None = None
    until: datetime | None = None
    agents: tuple[str, ...] = ()
    session_ids: tuple[str, ...] = ()
    q: str | None = None  # free-text substring match (objective + session id)

    @property
    def is_identity(self) -> bool:
        return (
            self.since is None
            and self.until is None
            and not self.agents
            and not self.session_ids
            and not self.q
        )

    def matches_session(self, sm: SessionMetrics) -> bool:
        """True if this session passes the non-turn-level filters."""
        if self.session_ids and sm.session_id not in self.session_ids:
            return False
        if self.q:
            haystack = (sm.session_id + " " + (sm.objective or "")).lower()
            if self.q.lower() not in haystack:
                return False
        return True


@dataclass(frozen=True, slots=True)
class CrossSessionMetrics:
    sessions: tuple[SessionMetrics, ...]
    by_agent: dict[str, TokenUsage] = field(default_factory=dict)
    cost_by_agent: dict[str, float] = field(default_factory=dict)
    by_day: dict[str, TokenUsage] = field(default_factory=dict)
    total_tokens: TokenUsage = field(default_factory=TokenUsage)
    total_cost_usd: float | None = None
    total_duration_ms: int = 0
    session_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "sessions": [s.to_dict() for s in self.sessions],
            "by_agent": {k: v.to_dict() for k, v in self.by_agent.items()},
            "cost_by_agent": dict(self.cost_by_agent),
            "by_day": {k: v.to_dict() for k, v in self.by_day.items()},
            "total_tokens": self.total_tokens.to_dict(),
            "total_cost_usd": self.total_cost_usd,
            "total_duration_ms": self.total_duration_ms,
            "session_count": self.session_count,
        }


# ---------------------------------------------------------------------------
# Public extractors
# ---------------------------------------------------------------------------


def extract_turn_metrics(
    repo_root: Path,
    session_id: str,
    turn_number: int,
) -> TurnMetrics:
    """Extract metrics for one turn. Missing fields become None — never raises
    on shape mismatch."""
    tdir = turn_dir(repo_root, session_id, turn_number)
    state = _load_state_json(tdir / "state.json")
    events = _parse_output_jsonl(tdir / "output.jsonl")

    agent = _state_str(state, "agent_key") or _guess_agent_from_events(events)
    started_at = _state_meta(state, "started_at")
    finished_at = _state_meta(state, "finished_at")
    status = _state_str(state, "status")

    extracted = _extract_from_events(agent, events)

    duration_ms = extracted.duration_ms
    if duration_ms is None and started_at and finished_at:
        duration_ms = _derive_duration_ms(started_at, finished_at)

    return TurnMetrics(
        session_id=session_id,
        turn_number=turn_number,
        agent=agent or "unknown",
        model=extracted.model,
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=duration_ms,
        api_duration_ms=extracted.api_duration_ms,
        tokens=extracted.tokens,
        cost_usd=extracted.cost_usd,
        tool_calls=extracted.tool_calls,
        status=status,
        succeeded=status not in _FAILURE_STATUSES if status else True,
    )


def extract_session_metrics(repo_root: Path, session_id: str) -> SessionMetrics:
    """Aggregate all turns for a single session."""
    view = _load_derived_view(repo_root, session_id)
    turns: list[TurnMetrics] = []
    for turn_number in _list_turn_numbers(repo_root, session_id):
        turns.append(extract_turn_metrics(repo_root, session_id, turn_number))

    by_agent: dict[str, TokenUsage] = {}
    cost_by_agent: dict[str, float] = {}
    total_tokens = TokenUsage()
    total_cost: float | None = None
    total_duration_ms = 0
    successful = 0
    started_at = _view_str(view, "created_at")

    for t in turns:
        total_tokens = total_tokens + t.tokens
        if t.duration_ms is not None:
            total_duration_ms += t.duration_ms
        if t.cost_usd is not None:
            total_cost = (total_cost or 0.0) + t.cost_usd
            cost_by_agent[t.agent] = cost_by_agent.get(t.agent, 0.0) + t.cost_usd
        by_agent[t.agent] = by_agent.get(t.agent, TokenUsage()) + t.tokens
        if t.succeeded:
            successful += 1
        if started_at is None and t.started_at:
            started_at = t.started_at

    return SessionMetrics(
        session_id=session_id,
        current_agent=_view_str(view, "current_agent") or "unknown",
        current_status=_view_str(view, "current_status") or "unknown",
        objective=_view_str(view, "objective"),
        started_at=started_at,
        updated_at=_view_str(view, "updated_at"),
        turn_count=len(turns),
        successful_turns=successful,
        total_tokens=total_tokens,
        total_cost_usd=total_cost,
        total_duration_ms=total_duration_ms,
        by_agent=by_agent,
        cost_by_agent=cost_by_agent,
        turns=tuple(turns),
    )


def extract_cross_session_metrics(
    repo_root: Path,
    *,
    filter: MetricsFilter | None = None,
    since: datetime | None = None,
    agents: Iterable[str] | None = None,
) -> CrossSessionMetrics:
    """Aggregate metrics across all sessions in the repo.

    Pass a ``MetricsFilter`` to scope the result. The legacy ``since`` /
    ``agents`` kwargs still work and compose with the filter (filter wins
    on conflict).
    """
    if filter is None:
        filter = MetricsFilter(
            since=since,
            agents=tuple(agents) if agents else (),
        )
    elif since is not None or agents is not None:
        # Caller passed both — merge: filter takes precedence, kwargs fill gaps.
        filter = MetricsFilter(
            since=filter.since or since,
            until=filter.until,
            agents=filter.agents or (tuple(agents) if agents else ()),
            session_ids=filter.session_ids,
            q=filter.q,
        )

    agents_filter = set(filter.agents) if filter.agents else None
    by_agent: dict[str, TokenUsage] = {}
    cost_by_agent: dict[str, float] = {}
    by_day: dict[str, TokenUsage] = {}
    total_tokens = TokenUsage()
    total_cost: float | None = None
    total_duration_ms = 0
    sessions: list[SessionMetrics] = []

    root = sessions_root(repo_root)
    if not root.exists():
        return CrossSessionMetrics(sessions=())

    for session_dir in sorted(root.iterdir()):
        if not session_dir.is_dir():
            continue
        sid = session_dir.name
        if not is_session(repo_root, sid):
            continue
        try:
            sm = extract_session_metrics(repo_root, sid)
        except Exception:
            continue

        if filter.since is not None and not _session_after(sm, filter.since):
            continue
        if filter.until is not None and not _session_before(sm, filter.until):
            continue
        if not filter.matches_session(sm):
            continue

        keep_turns = sm.turns
        if agents_filter is not None:
            keep_turns = tuple(t for t in sm.turns if t.agent in agents_filter)
            if not keep_turns:
                continue
            sm = _rebuild_session_with_turns(sm, keep_turns)

        sessions.append(sm)

        for agent, usage in sm.by_agent.items():
            by_agent[agent] = by_agent.get(agent, TokenUsage()) + usage
        for agent, cost in sm.cost_by_agent.items():
            cost_by_agent[agent] = cost_by_agent.get(agent, 0.0) + cost

        total_tokens = total_tokens + sm.total_tokens
        if sm.total_cost_usd is not None:
            total_cost = (total_cost or 0.0) + sm.total_cost_usd
        total_duration_ms += sm.total_duration_ms

        for t in sm.turns:
            day = _iso_day(t.started_at) or _iso_day(sm.started_at)
            if day is None:
                continue
            by_day[day] = by_day.get(day, TokenUsage()) + t.tokens

    return CrossSessionMetrics(
        sessions=tuple(sessions),
        by_agent=by_agent,
        cost_by_agent=cost_by_agent,
        by_day=by_day,
        total_tokens=total_tokens,
        total_cost_usd=total_cost,
        total_duration_ms=total_duration_ms,
        session_count=len(sessions),
    )


# ---------------------------------------------------------------------------
# Per-agent extraction
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Extracted:
    tokens: TokenUsage
    cost_usd: float | None
    duration_ms: int | None
    api_duration_ms: int | None
    model: str | None
    tool_calls: int


def _extract_from_events(agent: str | None, events: list[dict[str, Any]]) -> _Extracted:
    if agent == "claude":
        return _extract_claude(events)
    if agent == "codex":
        return _extract_codex(events)
    if agent == "gemini":
        return _extract_gemini(events)
    # Unknown agent: best-effort generic.
    return _extract_generic(events)


def _extract_claude(events: list[dict[str, Any]]) -> _Extracted:
    result = _last_event_of_type(events, "result")
    tokens = TokenUsage()
    cost: float | None = None
    duration_ms: int | None = None
    api_duration_ms: int | None = None
    model: str | None = None

    if result:
        usage = result.get("usage") if isinstance(result.get("usage"), Mapping) else None
        tokens = _tokens_from_usage(usage) if usage else tokens
        cost = _coerce_float(result.get("total_cost_usd"))
        duration_ms = _coerce_int(result.get("duration_ms"))
        api_duration_ms = _coerce_int(result.get("duration_api_ms"))
        model_usage = result.get("modelUsage")
        if isinstance(model_usage, Mapping) and model_usage:
            model = next(iter(model_usage.keys()), None)

    if model is None:
        for event in events:
            msg = event.get("message") if isinstance(event, Mapping) else None
            if isinstance(msg, Mapping):
                m = msg.get("model")
                if isinstance(m, str) and m:
                    model = m
                    break

    tool_calls = _count_claude_tool_calls(events)
    return _Extracted(tokens, cost, duration_ms, api_duration_ms, model, tool_calls)


def _extract_codex(events: list[dict[str, Any]]) -> _Extracted:
    tokens = TokenUsage()
    cost: float | None = None
    duration_ms: int | None = None
    model: str | None = None
    tool_calls = 0

    for event in events:
        if not isinstance(event, Mapping):
            continue
        # Codex shapes vary; check several spots.
        usage = event.get("token_usage") or event.get("usage")
        if isinstance(usage, Mapping):
            t = _tokens_from_usage(usage)
            tokens = tokens + t
        if event.get("type") == "item.completed":
            item = event.get("item")
            if isinstance(item, Mapping):
                item_type = item.get("type", "")
                if isinstance(item_type, str) and (
                    "tool" in item_type or "function_call" in item_type
                ):
                    tool_calls += 1
                m = item.get("model")
                if isinstance(m, str) and m and model is None:
                    model = m
        if event.get("type") == "result":
            duration_ms = _coerce_int(event.get("duration_ms")) or duration_ms
            cost = _coerce_float(event.get("total_cost_usd")) or cost

    return _Extracted(tokens, cost, duration_ms, None, model, tool_calls)


def _extract_gemini(events: list[dict[str, Any]]) -> _Extracted:
    tokens = TokenUsage()
    cost: float | None = None
    duration_ms: int | None = None
    model: str | None = None
    tool_calls = 0

    for event in events:
        if not isinstance(event, Mapping):
            continue
        msg = event.get("message") if isinstance(event.get("message"), Mapping) else event
        if isinstance(msg, Mapping):
            usage = msg.get("usage") or msg.get("usageMetadata")
            if isinstance(usage, Mapping):
                tokens = tokens + _tokens_from_usage(usage)
            m = msg.get("model")
            if isinstance(m, str) and m and model is None:
                model = m
            for block in _iter_content_blocks(msg):
                btype = block.get("type")
                if btype in ("function_call", "tool_use", "toolCall"):
                    tool_calls += 1
        if event.get("type") == "result":
            duration_ms = _coerce_int(event.get("duration_ms")) or duration_ms
            cost = _coerce_float(event.get("total_cost_usd")) or cost

    return _Extracted(tokens, cost, duration_ms, None, model, tool_calls)


def _extract_generic(events: list[dict[str, Any]]) -> _Extracted:
    tokens = TokenUsage()
    for event in events:
        if not isinstance(event, Mapping):
            continue
        usage = event.get("usage") or event.get("token_usage")
        if isinstance(usage, Mapping):
            tokens = tokens + _tokens_from_usage(usage)
    return _Extracted(tokens, None, None, None, None, 0)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_output_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _load_state_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _state_str(state: dict[str, Any] | None, key: str) -> str | None:
    if not state:
        return None
    v = state.get(key)
    return v if isinstance(v, str) and v else None


def _state_meta(state: dict[str, Any] | None, key: str) -> str | None:
    if not state:
        return None
    meta = state.get("metadata")
    if not isinstance(meta, Mapping):
        return None
    v = meta.get(key)
    return v if isinstance(v, str) and v else None


def _load_derived_view(repo_root: Path, session_id: str) -> dict[str, Any] | None:
    return _load_state_json(derived_view_path(repo_root, session_id))


def _view_str(view: dict[str, Any] | None, key: str) -> str | None:
    if not view:
        return None
    v = view.get(key)
    return v if isinstance(v, str) and v else None


def _list_turn_numbers(repo_root: Path, session_id: str) -> list[int]:
    tdir = turns_dir(repo_root, session_id)
    if not tdir.exists():
        return []
    out: list[int] = []
    for child in tdir.iterdir():
        if not child.is_dir():
            continue
        name = child.name
        if not name.startswith("turn-"):
            continue
        try:
            out.append(int(name[5:]))
        except ValueError:
            continue
    return sorted(out)


def _tokens_from_usage(usage: Mapping[str, Any]) -> TokenUsage:
    return TokenUsage(
        input=_coerce_int(
            usage.get("input_tokens")
            or usage.get("inputTokens")
            or usage.get("prompt_tokens")
            or usage.get("promptTokenCount")
        ),
        output=_coerce_int(
            usage.get("output_tokens")
            or usage.get("outputTokens")
            or usage.get("completion_tokens")
            or usage.get("candidatesTokenCount")
        ),
        cache_read=_coerce_int(
            usage.get("cache_read_input_tokens") or usage.get("cacheReadInputTokens")
        ),
        cache_creation=_coerce_int(
            usage.get("cache_creation_input_tokens") or usage.get("cacheCreationInputTokens")
        ),
    )


def _count_claude_tool_calls(events: list[dict[str, Any]]) -> int:
    count = 0
    for event in events:
        if not isinstance(event, Mapping):
            continue
        msg = event.get("message")
        if not isinstance(msg, Mapping):
            continue
        if msg.get("role") != "assistant":
            continue
        for block in msg.get("content", []) or []:
            if isinstance(block, Mapping) and block.get("type") == "tool_use":
                count += 1
    return count


def _iter_content_blocks(msg: Mapping[str, Any]):
    content = msg.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, Mapping):
                yield block


def _last_event_of_type(events: list[dict[str, Any]], event_type: str) -> dict[str, Any] | None:
    for event in reversed(events):
        if isinstance(event, Mapping) and event.get("type") == event_type:
            return dict(event)
    return None


def _guess_agent_from_events(events: list[dict[str, Any]]) -> str | None:
    for event in events:
        if not isinstance(event, Mapping):
            continue
        if event.get("type") == "item.completed":
            return "codex"
        msg = event.get("message")
        if isinstance(msg, Mapping):
            role = msg.get("role")
            if role == "assistant":
                return "claude"
            if role == "model":
                return "gemini"
        # Claude result events carry distinctive modelUsage / total_cost_usd.
        if event.get("type") == "result" and ("modelUsage" in event or "total_cost_usd" in event):
            return "claude"
    return None


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _coerce_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _add_optional(a: int | None, b: int | None) -> int | None:
    if a is None and b is None:
        return None
    return (a or 0) + (b or 0)


def _derive_duration_ms(started_at: str, finished_at: str) -> int | None:
    try:
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    delta_ms = int((end - start).total_seconds() * 1000)
    return max(delta_ms, 0)


def _iso_day(timestamp: str | None) -> str | None:
    if not timestamp:
        return None
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.strftime("%Y-%m-%d")


def _session_after(session: SessionMetrics, since: datetime) -> bool:
    candidates = [session.started_at, session.updated_at]
    for ts in candidates:
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt >= since:
            return True
    # If no parseable timestamp, exclude when filter is active.
    return False


def _session_before(session: SessionMetrics, until: datetime) -> bool:
    """True if the session has at least one parseable timestamp <= ``until``."""
    candidates = [session.started_at, session.updated_at]
    for ts in candidates:
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt <= until:
            return True
    return False


def _rebuild_session_with_turns(
    base: SessionMetrics, turns: tuple[TurnMetrics, ...]
) -> SessionMetrics:
    by_agent: dict[str, TokenUsage] = {}
    cost_by_agent: dict[str, float] = {}
    total_tokens = TokenUsage()
    total_cost: float | None = None
    total_duration_ms = 0
    successful = 0
    for t in turns:
        total_tokens = total_tokens + t.tokens
        if t.duration_ms is not None:
            total_duration_ms += t.duration_ms
        if t.cost_usd is not None:
            total_cost = (total_cost or 0.0) + t.cost_usd
            cost_by_agent[t.agent] = cost_by_agent.get(t.agent, 0.0) + t.cost_usd
        by_agent[t.agent] = by_agent.get(t.agent, TokenUsage()) + t.tokens
        if t.succeeded:
            successful += 1
    return SessionMetrics(
        session_id=base.session_id,
        current_agent=base.current_agent,
        current_status=base.current_status,
        objective=base.objective,
        started_at=base.started_at,
        updated_at=base.updated_at,
        turn_count=len(turns),
        successful_turns=successful,
        total_tokens=total_tokens,
        total_cost_usd=total_cost,
        total_duration_ms=total_duration_ms,
        by_agent=by_agent,
        cost_by_agent=cost_by_agent,
        turns=turns,
    )


@dataclass(frozen=True, slots=True)
class DailyBucket:
    """One day's worth of aggregated metrics. Produced by :func:`bucketize`.

    ``label`` is an ISO date (``YYYY-MM-DD``); zero-fill days for missing
    data. ``cost`` is None when no turn in the bucket had a cost (preserves
    the "unknown vs $0" distinction).
    """

    label: str
    tokens: int
    cost: float | None
    turns: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "tokens": self.tokens,
            "cost": self.cost,
            "turns": self.turns,
        }


def bucketize(
    metrics: CrossSessionMetrics,
    *,
    by: str = "day",
    limit: int = 30,
) -> tuple[DailyBucket, ...]:
    """Aggregate per-turn data into time buckets for charting.

    Currently only ``by="day"`` is supported; the parameter exists so future
    "week" / "hour" variants can plug in without touching call sites.

    Returns an ordered tuple of :class:`DailyBucket`, oldest first, with
    ``limit`` controlling the trailing window in days. Gap days are
    zero-filled so charts produce a continuous timeline.
    """
    if by != "day":
        raise ValueError(f"unsupported bucket: {by!r}")

    tokens_by_day: dict[str, int] = {}
    cost_by_day: dict[str, float | None] = {}
    turns_by_day: dict[str, int] = {}

    for session in metrics.sessions:
        fallback_day = _iso_day(session.started_at) or _iso_day(session.updated_at)
        for turn in session.turns:
            day = _iso_day(turn.started_at) or _iso_day(turn.finished_at) or fallback_day
            if day is None:
                continue
            tokens_by_day[day] = tokens_by_day.get(day, 0) + (turn.tokens.total or 0)
            turns_by_day[day] = turns_by_day.get(day, 0) + 1
            if turn.cost_usd is not None:
                cost_by_day[day] = (cost_by_day.get(day) or 0.0) + turn.cost_usd

    if not tokens_by_day and not turns_by_day:
        return ()

    days_with_data = sorted(set(tokens_by_day) | set(turns_by_day) | set(cost_by_day))
    last_day = date.fromisoformat(days_with_data[-1])
    span = max(1, min(limit, (last_day - date.fromisoformat(days_with_data[0])).days + 1))
    first_day = last_day - timedelta(days=span - 1)

    buckets: list[DailyBucket] = []
    cursor = first_day
    while cursor <= last_day:
        key = cursor.isoformat()
        buckets.append(
            DailyBucket(
                label=key,
                tokens=tokens_by_day.get(key, 0),
                cost=cost_by_day.get(key),
                turns=turns_by_day.get(key, 0),
            )
        )
        cursor += timedelta(days=1)
    return tuple(buckets)


__all__ = [
    "TokenUsage",
    "TurnMetrics",
    "SessionMetrics",
    "CrossSessionMetrics",
    "DailyBucket",
    "MetricsFilter",
    "extract_turn_metrics",
    "extract_session_metrics",
    "extract_cross_session_metrics",
    "bucketize",
]
