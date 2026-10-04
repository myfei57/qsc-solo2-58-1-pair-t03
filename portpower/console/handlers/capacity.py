"""Versioned declared-capacity endpoints.

Covers baseline declarations, the retrofit request lifecycle
(submit / review / revoke / status), the audit-window freeze, as-of capacity
traceability and immutable settlements.
"""

from __future__ import annotations

from portpower.console.http import BadRequest, Request, Response
from portpower.console.validation import optional_text, require_number, require_text
from portpower.ship.capacity_ledger import (
    CapacityExists,
    CapacityNotRevocable,
    CapacityPending,
    CapacityStateError,
)
from portpower.ship.retrofit_request import (
    RetrofitFrozen,
    RetrofitNotRevocable,
)
from portpower.ship.settlement import AlreadySettled


def capacity_declare(runtime, request: Request) -> Response:
    payload = request.json_object()
    ship = require_text(payload, "ship")
    capacity = require_number(payload, "capacity")
    if capacity <= 0:
        raise BadRequest("capacity must be positive")
    effective_at = optional_text(payload, "effective_at") or None
    try:
        version = runtime.capacities.baseline(
            ship, capacity, effective_at=effective_at
        )
    except CapacityExists as exc:
        raise BadRequest(str(exc)) from exc
    runtime.ledger.append(
        "capacity.declare",
        f"ship={ship} version=1 capacity={capacity}",
    )
    return Response.json(200, {"declared": True, "version": version.as_dict()})


def capacity_versions(runtime, request: Request) -> Response:
    ship = request.query_one("ship", "") or ""
    versions = runtime.capacities.list_for(ship)
    return Response.json(
        200,
        {
            "ship": ship,
            "count": len(versions),
            "versions": [item.as_dict() for item in versions],
        },
    )


def capacity_current(runtime, request: Request) -> Response:
    ship = request.query_one("ship", "") or ""
    version = runtime.declarations.current_capacity(ship)
    if version is None:
        raise LookupError(f"no declared capacity on file for ship {ship}")
    return Response.json(200, {"ship": ship, "current": version.as_dict()})


def capacity_as_of(runtime, request: Request) -> Response:
    ship = request.query_one("ship", "") or ""
    moment = request.query_one("at")
    if not moment:
        raise BadRequest("at (ISO timestamp) is required")
    version = runtime.declarations.capacity_at(ship, moment)
    return Response.json(
        200,
        {
            "ship": ship,
            "at": moment,
            "found": version is not None,
            "version": version.as_dict() if version is not None else None,
        },
    )


def retrofit_submit(runtime, request: Request) -> Response:
    payload = request.json_object()
    ship = require_text(payload, "ship")
    added = require_number(payload, "added_load")
    effective_at = optional_text(payload, "effective_at") or None
    submitted = runtime.declarations.submit(ship, added, effective_at)
    return Response.json(
        200,
        {"submitted": True, "request": submitted.as_dict()},
    )


def retrofit_review(runtime, request: Request) -> Response:
    payload = request.json_object()
    ship = require_text(payload, "ship")
    request_id = require_text(payload, "request_id")
    approve = bool(payload.get("approve", True))
    effective_at = optional_text(payload, "effective_at") or None
    reason = optional_text(payload, "reason")
    decision = runtime.declarations.review(
        ship, request_id, approve=approve, effective_at=effective_at, reason=reason
    )
    if approve:
        return Response.json(
            200,
            {
                "approved": True,
                "request": decision.request.as_dict(),
                "version": decision.version.as_dict(),
            },
        )
    return Response.json(200, {"approved": False, "request": decision.as_dict()})


def retrofit_revoke(runtime, request: Request) -> Response:
    payload = request.json_object()
    ship = require_text(payload, "ship")
    request_id = require_text(payload, "request_id")
    reason = optional_text(payload, "reason")
    revoked, version = runtime.declarations.revoke(ship, request_id, reason)
    return Response.json(
        200,
        {
            "revoked": True,
            "request": revoked.as_dict(),
            "version": version.as_dict() if version is not None else None,
        },
    )


def retrofit_status(runtime, request: Request) -> Response:
    ship = request.query_one("ship", "") or ""
    requests = runtime.declarations.requests_for(ship)
    return Response.json(
        200,
        {
            "ship": ship,
            "frozen": runtime.declarations.is_frozen(ship),
            "count": len(requests),
            "requests": [item.as_dict() for item in requests],
        },
    )


def retrofit_freeze(runtime, request: Request) -> Response:
    payload = request.json_object()
    ship = require_text(payload, "ship")
    info = runtime.declarations.freeze(ship)
    return Response.json(200, {"frozen": True, **info})


def retrofit_unfreeze(runtime, request: Request) -> Response:
    payload = request.json_object()
    ship = require_text(payload, "ship")
    runtime.declarations.unfreeze(ship)
    return Response.json(200, {"frozen": False, "ship": ship})


def retrofit_tick(runtime, request: Request) -> Response:
    """Advance due future-dated versions into effect (idempotent)."""

    payload = request.json_object()
    ship = require_text(payload, "ship")
    version = runtime.declarations.activate_due(ship)
    return Response.json(
        200,
        {
            "ship": ship,
            "activated": version is not None,
            "version": version.as_dict() if version is not None else None,
        },
    )


def settle(runtime, request: Request) -> Response:
    payload = request.json_object()
    ship = require_text(payload, "ship")
    start = require_text(payload, "period_start")
    end = require_text(payload, "period_end")
    usage = require_number(payload, "usage")
    rate = payload.get("rate", runtime.tariff.rate)
    if isinstance(rate, bool) or not isinstance(rate, (int, float)):
        raise BadRequest("rate must be a number")
    entry = runtime.declarations.settle_usage(
        ship, start, end, usage, float(rate)
    )
    return Response.json(200, {"settled": True, "settlement": entry.as_dict()})


def settlements(runtime, request: Request) -> Response:
    ship = request.query_one("ship", "") or ""
    entries = runtime.declarations.settlements_for(ship)
    return Response.json(
        200,
        {
            "ship": ship,
            "count": len(entries),
            "total_charge": sum(item.charge for item in entries),
            "settlements": [item.as_dict() for item in entries],
        },
    )


#: Domain errors that map to 409 Conflict rather than 400/500.
CONFLICT_ERRORS = (
    RetrofitFrozen,
    RetrofitNotRevocable,
    CapacityNotRevocable,
    CapacityPending,
    CapacityStateError,
    AlreadySettled,
)
