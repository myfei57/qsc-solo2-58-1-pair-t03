"""Append-only capacity ledger keyed by vessel.

One document per ship holds an ordered chain of :class:`CapacityVersion`
records. Versions are never removed. Approving a retrofit appends a ``pending``
version; when its effective moment arrives it is ``activated`` and the version
it replaces is marked ``superseded``. Revoking a still-pending version marks it
``revoked`` without touching any later state.
"""

from __future__ import annotations

from typing import Any

from portpower.ship.capacity import (
    STATUS_ACTIVE,
    STATUS_PENDING,
    STATUS_REVOKED,
    STATUS_SUPERSEDED,
    CapacityVersion,
)
from portpower.ship.clock import Clock, stamp
from portpower.store.kvstore import RecordStore

PREFIX = "ship.capacity."


def capacity_key(ship: str) -> str:
    return PREFIX + ship


class CapacityLedger:
    def __init__(self, store: RecordStore) -> None:
        self._store = store

    def list_for(self, ship: str) -> list[CapacityVersion]:
        return _versions_from(self._store.get_json(capacity_key(ship)))

    def current(self, ship: str, at: str | None = None, clock: Clock | None = None) -> CapacityVersion | None:
        """The live head: pending if the future version is already on file."""

        versions = self.list_for(ship)
        live = [item for item in versions if item.is_live()]
        if not live:
            return None
        live.sort(key=lambda item: item.version)
        head = live[-1]
        if head.status == STATUS_PENDING:
            moment = at or stamp(clock)
            if moment >= head.effective_at:
                activated = self.activate(ship, head.version, moment)
                return activated
        return head

    def effective(self, ship: str, at: str | None = None, clock: Clock | None = None) -> CapacityVersion | None:
        """Capacity in force right now, activating any due pending version."""

        current = self.current(ship, at, clock)
        if current is None:
            return None
        if current.status == STATUS_PENDING:
            for version in reversed(self.list_for(ship)):
                if version.status == STATUS_ACTIVE:
                    return version
            return None
        return current

    def as_of(self, ship: str, moment: str) -> CapacityVersion | None:
        """Capacity that governed the vessel at ``moment``.

        Pending versions are excluded (they did not exist as decisions yet),
        and revoked versions never governed anything.
        """

        candidate: CapacityVersion | None = None
        for version in self.list_for(ship):
            if version.status in (STATUS_PENDING, STATUS_REVOKED):
                continue
            if version.effective_at <= moment and version.status in (
                STATUS_ACTIVE,
                STATUS_SUPERSEDED,
            ):
                if candidate is None or version.effective_at >= candidate.effective_at:
                    candidate = version
        return candidate

    def pending(self, ship: str) -> CapacityVersion | None:
        for version in reversed(self.list_for(ship)):
            if version.status == STATUS_PENDING:
                return version
        return None

    def baseline(
        self,
        ship: str,
        capacity: float,
        effective_at: str | None = None,
        clock: Clock | None = None,
        reason: str = "initial declaration",
    ) -> CapacityVersion:
        """Seed the first declaration; safe to call once per vessel."""

        def mutate(document: Any) -> tuple[Any, CapacityVersion]:
            versions = _versions_from(document)
            if versions:
                raise CapacityExists(f"capacity ledger already seeded for {ship}")
            version = CapacityVersion(
                ship=ship,
                version=1,
                capacity=capacity,
                effective_at=effective_at or stamp(clock),
                status=STATUS_ACTIVE,
                reason=reason,
            )
            versions.append(version)
            return _document(versions), version

        return self._store.update_json(capacity_key(ship), mutate)

    def append_pending(
        self,
        ship: str,
        capacity: float,
        effective_at: str,
        source_request: str,
        reason: str = "",
    ) -> CapacityVersion:
        """Record a reviewed declaration that starts governing in the future."""

        def mutate(document: Any) -> tuple[Any, CapacityVersion]:
            versions = _versions_from(document)
            if not versions:
                raise LookupError(f"no baseline capacity for ship {ship}")
            if any(item.status == STATUS_PENDING for item in versions):
                raise CapacityPending(f"ship {ship} already has a pending version")
            previous = max(
                (item for item in versions if item.status == STATUS_ACTIVE),
                key=lambda item: item.version,
                default=None,
            )
            if previous is None:
                raise LookupError(f"no active capacity version for ship {ship}")
            if effective_at <= previous.effective_at:
                raise ValueError(
                    "effective_at must be later than the current version"
                )
            version = CapacityVersion(
                ship=ship,
                version=max(item.version for item in versions) + 1,
                capacity=capacity,
                effective_at=effective_at,
                status=STATUS_PENDING,
                source_request=source_request,
                reason=reason,
            )
            versions.append(version)
            return _document(versions), version

        return self._store.update_json(capacity_key(ship), mutate)

    def activate(
        self,
        ship: str,
        version_number: int,
        at: str | None = None,
        clock: Clock | None = None,
    ) -> CapacityVersion:
        def mutate(document: Any) -> tuple[Any, CapacityVersion]:
            versions = _versions_from(document)
            moment = at or stamp(clock)
            pending = next(
                (
                    item
                    for item in versions
                    if item.version == version_number and item.status == STATUS_PENDING
                ),
                None,
            )
            if pending is None:
                raise CapacityStateError(
                    f"version {version_number} for {ship} is not pending"
                )
            if moment < pending.effective_at:
                raise CapacityStateError(
                    f"version {version_number} for {ship} is not effective yet"
                )
            updated: list[CapacityVersion] = []
            for item in versions:
                if item.version == version_number:
                    updated.append(item.activated())
                elif item.status == STATUS_ACTIVE:
                    updated.append(item.superseded(moment))
                else:
                    updated.append(item)
            target = next(item for item in updated if item.version == version_number)
            return _document(updated), target

        return self._store.update_json(capacity_key(ship), mutate)

    def revoke_pending(
        self, ship: str, version_number: int, at: str | None = None,
        clock: Clock | None = None,
    ) -> CapacityVersion:
        def mutate(document: Any) -> tuple[Any, CapacityVersion]:
            versions = _versions_from(document)
            moment = at or stamp(clock)
            updated: list[CapacityVersion] = []
            target: CapacityVersion | None = None
            for item in versions:
                if item.version == version_number:
                    if item.status != STATUS_PENDING:
                        raise CapacityNotRevocable(
                            f"version {version_number} for {ship} is {item.status}"
                        )
                    target = item.revoked(moment)
                    updated.append(target)
                else:
                    updated.append(item)
            if target is None:
                raise LookupError(
                    f"version {version_number} not found for ship {ship}"
                )
            return _document(updated), target

        return self._store.update_json(capacity_key(ship), mutate)

    def version_count(self, ship: str) -> int:
        return len(self.list_for(ship))


def _versions_from(document: Any) -> list[CapacityVersion]:
    if not isinstance(document, dict):
        return []
    raw = document.get("versions")
    if not isinstance(raw, list):
        return []
    versions = [
        version
        for item in raw
        if (version := CapacityVersion.from_dict(item)) is not None
    ]
    versions.sort(key=lambda item: (item.effective_at, item.version))
    return versions


def _document(versions: list[CapacityVersion]) -> dict[str, object]:
    ordered = sorted(versions, key=lambda item: (item.effective_at, item.version))
    return {"versions": [item.as_dict() for item in ordered]}


class CapacityExists(RuntimeError):
    """Raised when a baseline is seeded twice for one vessel."""


class CapacityPending(RuntimeError):
    """Raised when a second future-dated version is queued behind one pending."""


class CapacityStateError(RuntimeError):
    """Raised for illegal capacity version transitions."""


class CapacityNotRevocable(RuntimeError):
    """Raised when a version cannot be revoked in its current state."""
