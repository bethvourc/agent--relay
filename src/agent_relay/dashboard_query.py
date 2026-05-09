"""Parse dashboard query strings into a :class:`MetricsFilter`.

Lives in its own module so the HTTP handler stays slim and the parsing
logic is testable without spinning up a server.
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import parse_qs

from agent_relay.metrics import MetricsFilter


def parse_filter_from_query(query_string: str) -> tuple[MetricsFilter, tuple[str, ...]]:
    """Parse a URL query string into a (filter, errors) pair.

    ``errors`` is a tuple of human-readable messages about ignored values.
    The handler surfaces them in a banner on the rendered page so the user
    can see what was dropped without us 500-ing.

    Recognized keys:
      - ``since`` / ``until`` — ISO date (YYYY-MM-DD) or full ISO timestamp.
      - ``agent`` — repeatable; restricts to the listed agents.
      - ``q`` — free-text substring (matches session id + objective).
    """
    if not query_string:
        return MetricsFilter(), ()

    params = parse_qs(query_string, keep_blank_values=False)
    errors: list[str] = []

    since = _parse_iso(params.get("since", [None])[0], "since", errors)
    until = _parse_iso(params.get("until", [None])[0], "until", errors)

    agents = tuple(a.strip() for a in params.get("agent", []) if a.strip())
    q_raw = params.get("q", [None])[0]
    q = q_raw.strip() if q_raw else None

    return (
        MetricsFilter(since=since, until=until, agents=agents, q=q or None),
        tuple(errors),
    )


def _parse_iso(raw: str | None, field: str, errors: list[str]) -> datetime | None:
    if raw is None or not raw.strip():
        return None
    text = raw.strip()
    # Accept date-only inputs (from <input type="date">) and full ISO.
    try:
        if "T" not in text and len(text) == 10:
            dt = datetime.fromisoformat(text + "T00:00:00+00:00")
        else:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        errors.append(f"{field}={text!r} is not a valid date (YYYY-MM-DD)")
        return None
    # Ensure tz-aware so comparisons in metrics extractor don't choke.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt
