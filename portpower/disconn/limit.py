"""Load limit enforcement against the vessel's declared envelope."""

from __future__ import annotations

from portpower.ship.history import ProfileHistory


class LoadExceedsProfile(RuntimeError):
    """Raised when a requested load exceeds the vessel's declared envelope."""


class LimitEnforcer:
    """Judges requests against the profile version effective right now."""

    def __init__(self, history: ProfileHistory) -> None:
        self._history = history

    def allow(self, ship: str, load: float) -> bool:
        record = self._history.effective_now(ship)
        if record is None:
            return False
        return load <= record.profile.max_load
