"""Clock used whenever a domain record needs to stamp the current time.

A single injectable seam keeps versioning, retrofit review and settlement
deterministic in tests while production callers just use ``utc_now``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Protocol


class Clock(Protocol):
    def __call__(self) -> datetime: ...


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def stamp(clock: Clock | None = None) -> str:
    """ISO-8601 UTC timestamp (second resolution), matching the audit ledger."""

    return (clock or utc_now)().isoformat()
