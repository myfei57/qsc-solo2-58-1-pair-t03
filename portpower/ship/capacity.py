"""Versioned declared capacity for one vessel.

Older records are never overwritten: every declaration that passed review
appends a new :class:`CapacityVersion`. Effective windows are half-open
``[effective_at, next_effective_at)`` so ``as_of`` reconstructs exactly which
capacity governed any past moment.

Lifecycle states:

``pending``
    Review passed and the version is on file, but ``effective_at`` has not
    arrived yet. Only one pending/active ("live") version exists per vessel.
``active``
    The capacity currently in force.
``superseded``
    A newer version took effect; the record is retained for traceability.
``revoked``
    Undone while still pending (via retrofit revocation). It never governed
    anything and is ignored by ``as_of``.
"""

from __future__ import annotations

from dataclasses import dataclass

STATUS_PENDING = "pending"
STATUS_ACTIVE = "active"
STATUS_SUPERSEDED = "superseded"
STATUS_REVOKED = "revoked"

#: States that occupy the "head of chain" slot.
LIVE_STATUSES = (STATUS_PENDING, STATUS_ACTIVE)


@dataclass(frozen=True)
class CapacityVersion:
    ship: str
    version: int
    capacity: float
    effective_at: str
    status: str = STATUS_ACTIVE
    #: When this version stopped governing (superseded or revoked).
    closed_at: str | None = None
    #: Retrofit request that produced this version; None for baselines.
    source_request: str | None = None
    reason: str = ""

    def in_force_at(self, moment: str) -> bool:
        return self.status == STATUS_ACTIVE and self.effective_at <= moment

    def is_live(self) -> bool:
        return self.status in LIVE_STATUSES

    def activated(self) -> "CapacityVersion":
        return self._with(status=STATUS_ACTIVE)

    def superseded(self, at: str) -> "CapacityVersion":
        return self._with(status=STATUS_SUPERSEDED, closed_at=at)

    def revoked(self, at: str) -> "CapacityVersion":
        return self._with(status=STATUS_REVOKED, closed_at=at)

    def _with(self, **changes: object) -> "CapacityVersion":
        return CapacityVersion(
            ship=self.ship,
            version=self.version,
            capacity=self.capacity,
            effective_at=self.effective_at,
            status=changes.get("status", self.status),  # type: ignore[arg-type]
            closed_at=changes.get("closed_at", self.closed_at),  # type: ignore[arg-type]
            source_request=self.source_request,
            reason=self.reason,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "ship": self.ship,
            "version": self.version,
            "capacity": self.capacity,
            "effective_at": self.effective_at,
            "status": self.status,
            "closed_at": self.closed_at,
            "source_request": self.source_request,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, payload: object) -> "CapacityVersion | None":
        if not isinstance(payload, dict):
            return None
        ship = payload.get("ship")
        version = payload.get("version")
        capacity = payload.get("capacity")
        effective_at = payload.get("effective_at")
        if not isinstance(ship, str) or not ship:
            return None
        if not isinstance(version, int) or isinstance(version, bool):
            return None
        if not isinstance(capacity, (int, float)) or isinstance(capacity, bool):
            return None
        if not isinstance(effective_at, str) or not effective_at:
            return None
        status = payload.get("status", STATUS_ACTIVE)
        if not isinstance(status, str):
            return None
        closed_at = payload.get("closed_at")
        if closed_at is not None and not isinstance(closed_at, str):
            return None
        source_request = payload.get("source_request")
        if source_request is not None and not isinstance(source_request, str):
            return None
        reason = payload.get("reason", "")
        if not isinstance(reason, str):
            return None
        return cls(
            ship=ship,
            version=version,
            capacity=float(capacity),
            effective_at=effective_at,
            status=status,
            closed_at=closed_at,
            source_request=source_request,
            reason=reason,
        )
