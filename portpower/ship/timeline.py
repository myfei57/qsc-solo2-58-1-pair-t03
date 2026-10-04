"""Canonical UTC timestamps for the versioned profile archive."""

from __future__ import annotations

from datetime import datetime, timezone


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize(value: str) -> str:
    """Canonicalize an ISO-8601 timestamp so strings sort chronologically.

    Naive timestamps are read as UTC. Raises ``ValueError`` for input that
    is not a parseable ISO-8601 timestamp.
    """

    text = value.strip()
    if not text:
        raise ValueError("timestamp is required")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()
