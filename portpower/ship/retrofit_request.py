"""Retrofit requests with a review lifecycle.

A request moves through ``submitted -> approved -> effective``. It may be
revoked only before the new capacity has taken effect; revocation is itself a
durable, timestamped fact (the original request is never erased). Freezes
placed during an audit window block new submissions outright.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from portpower.ship.clock import Clock, stamp
from portpower.store.kvstore import RecordStore

REQUESTS_PREFIX = "ship.retrofit.requests."
FREEZE_PREFIX = "ship.retrofit.frozen."

#: Submitted, awaiting review; no capacity version exists yet.
STATUS_SUBMITTED = "submitted"
#: Review passed and a future-dated capacity version was appended.
STATUS_APPROVED = "approved"
#: The appended capacity version is now in force.
STATUS_EFFECTIVE = "effective"
#: Undone before taking effect; kept on file as a revocation record.
STATUS_REVOKED = "revoked"
#: Review refused; kept on file.
STATUS_REJECTED = "rejected"

OPEN_STATUSES = (STATUS_SUBMITTED, STATUS_APPROVED)


def requests_key(ship: str) -> str:
    return REQUESTS_PREFIX + ship


def freeze_key(ship: str) -> str:
    return FREEZE_PREFIX + ship


class RetrofitFrozen(RuntimeError):
    """Raised when a retrofit is submitted while the vessel is frozen."""


class RetrofitNotRevocable(RuntimeError):
    """Raised when a request can no longer be undone."""


class RetrofitStateError(RuntimeError):
    """Raised when a lifecycle transition is illegal in the current state."""


@dataclass(frozen=True)
class RetrofitRequest:
    request_id: str
    ship: str
    added_load: float
    status: str
    submitted_at: str
    effective_at: str | None = None
    decided_at: str | None = None
    revoked_at: str | None = None
    revoke_reason: str | None = None
    #: Capacity version produced on approval.
    version: int | None = None

    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    def as_dict(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "ship": self.ship,
            "added_load": self.added_load,
            "status": self.status,
            "submitted_at": self.submitted_at,
            "effective_at": self.effective_at,
            "decided_at": self.decided_at,
            "revoked_at": self.revoked_at,
            "revoke_reason": self.revoke_reason,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, payload: object) -> "RetrofitRequest | None":
        if not isinstance(payload, dict):
            return None
        try:
            request_id = str(payload["request_id"])
            ship = str(payload["ship"])
            added_load = float(payload["added_load"])
            status = str(payload["status"])
            submitted_at = str(payload["submitted_at"])
        except (KeyError, TypeError, ValueError):
            return None
        effective_at = _optional_str(payload.get("effective_at"))
        decided_at = _optional_str(payload.get("decided_at"))
        revoked_at = _optional_str(payload.get("revoked_at"))
        revoke_reason = _optional_str(payload.get("revoke_reason"))
        raw_version = payload.get("version")
        version: int | None
        if raw_version is None:
            version = None
        elif isinstance(raw_version, int) and not isinstance(raw_version, bool):
            version = raw_version
        else:
            return None
        return cls(
            request_id=request_id,
            ship=ship,
            added_load=added_load,
            status=status,
            submitted_at=submitted_at,
            effective_at=effective_at,
            decided_at=decided_at,
            revoked_at=revoked_at,
            revoke_reason=revoke_reason,
            version=version,
        )


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


class RetrofitRegistry:
    """Durable store of every retrofit request made for each vessel."""

    def __init__(self, store: RecordStore) -> None:
        self._store = store

    # -- freeze -----------------------------------------------------------

    def freeze(self, ship: str, at: str | None = None, clock: Clock | None = None) -> None:
        payload = {"ship": ship, "frozen": True, "at": at or stamp(clock)}
        self._store.put_json(freeze_key(ship), payload)

    def unfreeze(self, ship: str) -> None:
        self._store.delete(freeze_key(ship))

    def is_frozen(self, ship: str) -> bool:
        document = self._store.get_json(freeze_key(ship))
        return isinstance(document, dict) and document.get("frozen") is True

    def freeze_info(self, ship: str) -> dict[str, object] | None:
        document = self._store.get_json(freeze_key(ship))
        return document if isinstance(document, dict) else None

    # -- requests ---------------------------------------------------------

    def list_for(self, ship: str) -> list[RetrofitRequest]:
        document = self._store.get_json(requests_key(ship))
        if not isinstance(document, dict):
            return []
        raw = document.get("requests")
        if not isinstance(raw, list):
            return []
        requests = [
            request
            for item in raw
            if (request := RetrofitRequest.from_dict(item)) is not None
        ]
        requests.sort(key=lambda request: request.submitted_at)
        return requests

    def get(self, ship: str, request_id: str) -> RetrofitRequest | None:
        for request in self.list_for(ship):
            if request.request_id == request_id:
                return request
        return None

    def open_request(self, ship: str) -> RetrofitRequest | None:
        for request in reversed(self.list_for(ship)):
            if request.is_open():
                return request
        return None

    def submit(
        self,
        ship: str,
        added_load: float,
        requested_effective_at: str | None = None,
        clock: Clock | None = None,
    ) -> RetrofitRequest:
        if self.is_frozen(ship):
            raise RetrofitFrozen(
                f"ship {ship} is frozen for retrofit review; submission rejected"
            )

        def mutate(document: Any) -> tuple[Any, RetrofitRequest]:
            requests = _requests_from(document)
            submitted_at = stamp(clock)
            request_id = f"RR-{ship.upper()}-{len(requests) + 1:04d}"
            request = RetrofitRequest(
                request_id=request_id,
                ship=ship,
                added_load=added_load,
                status=STATUS_SUBMITTED,
                submitted_at=submitted_at,
                effective_at=requested_effective_at,
            )
            requests.append(request)
            return {"requests": [item.as_dict() for item in requests]}, request

        return self._store.update_json(requests_key(ship), mutate)

    def save(self, request: RetrofitRequest) -> None:
        def mutate(document: Any) -> tuple[Any, RetrofitRequest]:
            requests = _requests_from(document)
            updated = [
                request if item.request_id == request.request_id else item
                for item in requests
            ]
            if all(item.request_id != request.request_id for item in requests):
                updated.append(request)
            updated.sort(key=lambda item: item.submitted_at)
            return {"requests": [item.as_dict() for item in updated]}, request

        self._store.update_json(requests_key(request.ship), mutate)


def _requests_from(document: Any) -> list[RetrofitRequest]:
    if not isinstance(document, dict):
        return []
    raw = document.get("requests")
    if not isinstance(raw, list):
        return []
    requests = [
        request
        for item in raw
        if (request := RetrofitRequest.from_dict(item)) is not None
    ]
    requests.sort(key=lambda request: request.submitted_at)
    return requests
