"""Append-only settlement ledger.

Once usage is settled the entry is immutable: it freezes the amount, the
window it covers and **the capacity version that governed it**. Revoking a
retrofit or approving a newer version never touches these records, so
already-settled usage is never recomputed. Re-settling the same
``(ship, period_start, period_end)`` is rejected rather than double-counted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from portpower.ship.clock import Clock, stamp
from portpower.store.kvstore import RecordStore

SETTLEMENT_PREFIX = "ship.settlement."


def settlement_key(ship: str) -> str:
    return SETTLEMENT_PREFIX + ship


class AlreadySettled(RuntimeError):
    """Raised when a window already has an immutable settlement entry."""


@dataclass(frozen=True)
class Settlement:
    ship: str
    period_start: str
    period_end: str
    usage: float
    charge: float
    #: Capacity version in force while the usage accrued.
    capacity_version: int
    #: Declared capacity snapshot at settlement time.
    capacity: float
    settled_at: str

    @property
    def window(self) -> tuple[str, str]:
        return self.period_start, self.period_end

    def as_dict(self) -> dict[str, object]:
        return {
            "ship": self.ship,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "usage": self.usage,
            "charge": self.charge,
            "capacity_version": self.capacity_version,
            "capacity": self.capacity,
            "settled_at": self.settled_at,
        }

    @classmethod
    def from_dict(cls, payload: object) -> "Settlement | None":
        if not isinstance(payload, dict):
            return None
        try:
            return cls(
                ship=str(payload["ship"]),
                period_start=str(payload["period_start"]),
                period_end=str(payload["period_end"]),
                usage=float(payload["usage"]),
                charge=float(payload["charge"]),
                capacity_version=int(payload["capacity_version"]),
                capacity=float(payload["capacity"]),
                settled_at=str(payload["settled_at"]),
            )
        except (KeyError, TypeError, ValueError):
            return None


class SettlementLedger:
    def __init__(self, store: RecordStore) -> None:
        self._store = store

    def settle(
        self,
        ship: str,
        period_start: str,
        period_end: str,
        usage: float,
        charge: float,
        capacity_version: int,
        capacity: float,
        clock: Clock | None = None,
    ) -> Settlement:
        if period_end < period_start:
            raise ValueError("period_end precedes period_start")
        if usage < 0:
            raise ValueError("settled usage must not be negative")

        def mutate(document: Any) -> tuple[Any, Settlement]:
            entries = _entries_from(document)
            if any(
                item.period_start == period_start and item.period_end == period_end
                for item in entries
            ):
                raise AlreadySettled(
                    f"ship {ship} window {period_start}..{period_end} already settled"
                )
            entry = Settlement(
                ship=ship,
                period_start=period_start,
                period_end=period_end,
                usage=float(usage),
                charge=float(charge),
                capacity_version=capacity_version,
                capacity=float(capacity),
                settled_at=stamp(clock),
            )
            entries.append(entry)
            entries.sort(key=lambda item: item.period_start)
            return {"settlements": [item.as_dict() for item in entries]}, entry

        return self._store.update_json(settlement_key(ship), mutate)

    def list_for(self, ship: str) -> list[Settlement]:
        return _entries_from(self._store.get_json(settlement_key(ship)))

    def for_window(self, ship: str, start: str, end: str) -> Settlement | None:
        return next(
            (
                item
                for item in self.list_for(ship)
                if item.period_start == start and item.period_end == end
            ),
            None,
        )

    def total_charge(self, ship: str) -> float:
        return sum(item.charge for item in self.list_for(ship))


def _entries_from(document: Any) -> list[Settlement]:
    if not isinstance(document, dict):
        return []
    raw = document.get("settlements")
    if not isinstance(raw, list):
        return []
    entries = [
        entry
        for item in raw
        if (entry := Settlement.from_dict(item)) is not None
    ]
    entries.sort(key=lambda item: item.period_start)
    return entries
