"""Application service for declared capacity changes.

Orchestrates the retrofit lifecycle on top of the append-only
:class:`CapacityLedger` and :class:`RetrofitRegistry`:

* ``submit`` is refused while the vessel is frozen for review.
* ``review`` appends a future-dated capacity version; the new declaration
  governs nothing before its ``effective_at``.
* ``revoke`` is allowed only while the change is still pending. Revocation is
  recorded on both the request and the capacity version; neither prior
  versions nor settled usage are rewritten.
"""

from __future__ import annotations

from dataclasses import dataclass

from portpower.audit.ledger import Ledger
from portpower.ship.capacity import CapacityVersion
from portpower.ship.capacity_ledger import CapacityLedger
from portpower.ship.clock import Clock, stamp
from portpower.ship.retrofit_request import (
    STATUS_APPROVED,
    STATUS_EFFECTIVE,
    STATUS_REJECTED,
    STATUS_REVOKED,
    RetrofitNotRevocable,
    RetrofitRegistry,
    RetrofitRequest,
)
from portpower.ship.settlement import Settlement, SettlementLedger

MAX_ADDED_LOAD = 5000.0


@dataclass(frozen=True)
class ReviewDecision:
    request: RetrofitRequest
    version: CapacityVersion


class DeclarationService:
    def __init__(
        self,
        capacities: CapacityLedger,
        retrofits: RetrofitRegistry,
        settlements: SettlementLedger | None = None,
        ledger: Ledger | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._capacities = capacities
        self._retrofits = retrofits
        self._settlements = settlements
        self._ledger = ledger
        self._clock = clock

    # -- freeze -----------------------------------------------------------

    def freeze(self, ship: str) -> dict[str, object]:
        at = stamp(self._clock)
        self._retrofits.freeze(ship, at=at)
        self._audit("retrofit.freeze", f"ship={ship} frozen for retrofit review at {at}")
        info = self._retrofits.freeze_info(ship)
        return info or {"ship": ship, "frozen": True, "at": at}

    def unfreeze(self, ship: str) -> None:
        self._retrofits.unfreeze(ship)
        self._audit("retrofit.unfreeze", f"ship={ship} retrofit freeze lifted")

    def is_frozen(self, ship: str) -> bool:
        return self._retrofits.is_frozen(ship)

    # -- lifecycle --------------------------------------------------------

    def submit(
        self,
        ship: str,
        added_load: float,
        effective_at: str | None = None,
    ) -> RetrofitRequest:
        if added_load <= 0:
            raise ValueError("added load must be positive")
        if added_load > MAX_ADDED_LOAD:
            raise ValueError("added load exceeds retrofit ceiling")
        if self._capacities.effective(ship, clock=self._clock) is None:
            raise LookupError(f"no declared capacity on file for ship {ship}")
        if effective_at is not None and effective_at <= stamp(self._clock):
            raise ValueError("effective_at must be in the future")
        request = self._retrofits.submit(
            ship, added_load, requested_effective_at=effective_at, clock=self._clock
        )
        self._audit(
            "retrofit.submit",
            f"ship={ship} request={request.request_id} added={added_load}"
            + (f" effective_at={effective_at}" if effective_at else ""),
        )
        return request

    def review(
        self,
        ship: str,
        request_id: str,
        approve: bool,
        effective_at: str | None = None,
        reason: str = "",
    ) -> ReviewDecision | RetrofitRequest:
        request = self._require_open(ship, request_id)
        decided_at = stamp(self._clock)
        if not approve:
            rejected = RetrofitRequest(
                request_id=request.request_id,
                ship=request.ship,
                added_load=request.added_load,
                status=STATUS_REJECTED,
                submitted_at=request.submitted_at,
                effective_at=request.effective_at,
                decided_at=decided_at,
            )
            self._retrofits.save(rejected)
            self._audit(
                "retrofit.reject", f"ship={ship} request={request_id} rejected"
            )
            return rejected

        starts_at = effective_at or request.effective_at
        if starts_at is None or starts_at <= decided_at:
            raise ValueError("an approved retrofit needs a future effective_at")
        current = self._capacities.effective(ship, clock=self._clock)
        if current is None:
            raise LookupError(f"no declared capacity on file for ship {ship}")
        version = self._capacities.append_pending(
            ship,
            capacity=current.capacity + request.added_load,
            effective_at=starts_at,
            source_request=request.request_id,
            reason=reason,
        )
        approved = RetrofitRequest(
            request_id=request.request_id,
            ship=request.ship,
            added_load=request.added_load,
            status=STATUS_APPROVED,
            submitted_at=request.submitted_at,
            effective_at=starts_at,
            decided_at=decided_at,
            version=version.version,
        )
        self._retrofits.save(approved)
        self._audit(
            "retrofit.approve",
            f"ship={ship} request={request_id} version={version.version}"
            f" capacity={version.capacity} effective_at={starts_at}",
        )
        return ReviewDecision(request=approved, version=version)

    def activate_due(self, ship: str, at: str | None = None) -> CapacityVersion | None:
        """Flip an approved future version to active once its time has come."""

        moment = at or stamp(self._clock)
        pending = self._capacities.pending(ship)
        if pending is None or pending.effective_at > moment:
            return None
        version = self._capacities.activate(ship, pending.version, at=moment)
        if pending.source_request is not None:
            request = self._retrofits.get(ship, pending.source_request)
            if request is not None and request.status == STATUS_APPROVED:
                self._retrofits.save(
                    RetrofitRequest(
                        request_id=request.request_id,
                        ship=request.ship,
                        added_load=request.added_load,
                        status=STATUS_EFFECTIVE,
                        submitted_at=request.submitted_at,
                        effective_at=request.effective_at,
                        decided_at=request.decided_at,
                        version=request.version,
                    )
                )
        self._audit(
            "retrofit.effective",
            f"ship={ship} version={version.version} capacity={version.capacity}",
        )
        return version

    def revoke(
        self, ship: str, request_id: str, reason: str = ""
    ) -> tuple[RetrofitRequest, CapacityVersion | None]:
        """Undo a mistaken retrofit before its capacity took effect.

        Only ``submitted``/``approved`` requests can be revoked. An approved
        request has a pending capacity version which is marked revoked too;
        already-effective requests are refused so settled usage is never
        reopened. A request revoked while still merely ``submitted`` has no
        capacity version (``None`` is returned).
        """

        request = self._require_open(ship, request_id)
        at = stamp(self._clock)
        version: CapacityVersion | None = None
        if request.status == STATUS_APPROVED and request.version is not None:
            version = self._capacities.revoke_pending(ship, request.version, at=at)
        revoked = RetrofitRequest(
            request_id=request.request_id,
            ship=request.ship,
            added_load=request.added_load,
            status=STATUS_REVOKED,
            submitted_at=request.submitted_at,
            effective_at=request.effective_at,
            decided_at=request.decided_at,
            revoked_at=at,
            revoke_reason=reason or None,
            version=request.version,
        )
        self._retrofits.save(revoked)
        self._audit(
            "retrofit.revoke",
            f"ship={ship} request={request_id}"
            + (f" version={request.version}" if request.version else "")
            + (f" reason={reason}" if reason else ""),
        )
        return revoked, version

    # -- reads ------------------------------------------------------------

    def settle_usage(
        self,
        ship: str,
        period_start: str,
        period_end: str,
        usage: float,
        rate: float,
    ) -> Settlement:
        """Freeze a usage window against the capacity that governed it.

        The capacity version is resolved ``as_of`` the period end and copied
        onto the settlement. Nothing here is ever updated afterwards, so a
        later revocation or a newer declaration cannot reopen the amount.
        """

        if self._settlements is None:
            raise RuntimeError("settlement ledger is not configured")
        governed = self._capacities.as_of(ship, period_end)
        if governed is None:
            raise LookupError(
                f"no capacity version was in force for {ship} at {period_end}"
            )
        entry = self._settlements.settle(
            ship=ship,
            period_start=period_start,
            period_end=period_end,
            usage=usage,
            charge=round(usage * rate, 2),
            capacity_version=governed.version,
            capacity=governed.capacity,
            clock=self._clock,
        )
        self._audit(
            "settlement.record",
            f"ship={ship} window={period_start}..{period_end} usage={usage}"
            f" version={governed.version} charge={entry.charge}",
        )
        return entry

    def settlements_for(self, ship: str) -> list[Settlement]:
        if self._settlements is None:
            return []
        return self._settlements.list_for(ship)

    def capacity_at(self, ship: str, moment: str) -> CapacityVersion | None:
        return self._capacities.as_of(ship, moment)

    def current_capacity(self, ship: str) -> CapacityVersion | None:
        activated = self.activate_due(ship)
        if activated is not None:
            return activated
        return self._capacities.effective(ship, clock=self._clock)

    def requests_for(self, ship: str) -> list[RetrofitRequest]:
        return self._retrofits.list_for(ship)

    # -- helpers ----------------------------------------------------------

    def _require_open(self, ship: str, request_id: str) -> RetrofitRequest:
        request = self._retrofits.get(ship, request_id)
        if request is None:
            raise LookupError(f"retrofit request {request_id} not found for {ship}")
        if not request.is_open():
            raise RetrofitNotRevocable(
                f"request {request_id} is {request.status} and can no longer change"
            )
        return request

    def _audit(self, event: str, detail: str) -> None:
        if self._ledger is not None:
            self._ledger.append(event, detail)
