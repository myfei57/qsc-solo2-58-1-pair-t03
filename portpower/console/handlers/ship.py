"""Vessel listing, retrofit and profile history endpoints."""

from __future__ import annotations

from portpower.console.http import BadRequest, Request, Response
from portpower.console.validation import optional_text, require_number, require_text
from portpower.ship.demand import demand_of, headroom_of
from portpower.ship.freeze import freeze_retrofits, unfreeze_retrofits
from portpower.ship.retrofit import submit_retrofit, withdraw_retrofit
from portpower.ship.timeline import normalize, now


def ships(runtime, request: Request) -> Response:
    identifiers = runtime.fleet.identifiers()
    profiles: dict[str, object] = {}
    demand: dict[str, float] = {}
    headroom: dict[str, float] = {}
    for identifier in identifiers:
        profile = runtime.profiles.load(identifier)
        if profile is not None:
            profiles[identifier] = profile.as_dict()
        demand[identifier] = demand_of(runtime.profiles, identifier)
        headroom[identifier] = headroom_of(runtime.profiles, identifier)
    return Response.json(
        200,
        {
            "count": runtime.fleet.count(),
            "ship_ids": identifiers,
            "moored": runtime.fleet.moored(),
            "known_profiles": runtime.profiles.known_ships(),
            "profiles": profiles,
            "demand": demand,
            "headroom": headroom,
        },
    )


def ship_detail(runtime, request: Request) -> Response:
    identifier = request.query_one("ship", "") or ""
    ship = runtime.fleet.get(identifier)
    if ship is None:
        raise LookupError(f"unknown ship {identifier!r}")
    return Response.json(200, ship.describe())


def retrofit(runtime, request: Request) -> Response:
    payload = request.json_object()
    ship = require_text(payload, "ship")
    added = require_number(payload, "added_load")
    effective_at = optional_text(payload, "effective_at") or None
    record = submit_retrofit(
        runtime.store, runtime.profiles, runtime.history, ship, added, effective_at
    )
    runtime.ledger.append(
        "retrofit",
        f"{ship} v{record.version} "
        f"max_load={record.profile.max_load} effective_at={record.effective_at}",
    )
    return Response.json(
        200,
        {
            "retrofitted": True,
            "ship": ship,
            "version": record.version,
            "max_load": record.profile.max_load,
            "effective_at": record.effective_at,
            "recorded_at": record.recorded_at,
            "versions": runtime.history.count(ship),
        },
    )


def retrofit_withdraw(runtime, request: Request) -> Response:
    payload = request.json_object()
    ship = require_text(payload, "ship")
    version = _optional_version(payload)
    record = withdraw_retrofit(runtime.profiles, runtime.history, ship, version)
    runtime.ledger.append(
        "retrofit_withdraw",
        f"{ship} v{record.version} withdrawn_at={record.withdrawn_at}",
    )
    current = runtime.history.effective_now(ship)
    return Response.json(
        200,
        {
            "withdrawn": True,
            "ship": ship,
            "version": record.version,
            "withdrawn_at": record.withdrawn_at,
            "effective_now": current.profile.max_load if current is not None else None,
        },
    )


def retrofit_freeze(runtime, request: Request) -> Response:
    payload = request.json_object()
    ship = require_text(payload, "ship")
    freeze_retrofits(runtime.store, ship)
    runtime.ledger.append("retrofit_freeze", ship)
    return Response.json(200, {"ship": ship, "frozen": True})


def retrofit_unfreeze(runtime, request: Request) -> Response:
    payload = request.json_object()
    ship = require_text(payload, "ship")
    unfreeze_retrofits(runtime.store, ship)
    runtime.ledger.append("retrofit_unfreeze", ship)
    return Response.json(200, {"ship": ship, "frozen": False})


def capacity(runtime, request: Request) -> Response:
    ship = request.query_one("ship", "") or ""
    if not ship.strip():
        raise BadRequest("ship is required")
    at = request.query_one("at", None)
    as_of = request.query_one("as_of", None)
    moment = normalize(at) if at else now()
    known = normalize(as_of) if as_of else now()
    record = runtime.history.effective_at(ship, moment, known)
    return Response.json(
        200,
        {
            "ship": ship,
            "at": moment,
            "as_of": known,
            "capacity": record.profile.max_load if record is not None else None,
            "record": record.as_dict() if record is not None else None,
        },
    )


def history(runtime, request: Request) -> Response:
    ship = request.query_one("ship", "") or ""
    records = runtime.history.list_for(ship)
    latest = runtime.history.latest(ship)
    current = runtime.history.effective_now(ship)
    return Response.json(
        200,
        {
            "ship": ship,
            "count": len(records),
            "latest": latest.as_dict() if latest is not None else None,
            "effective_now": current.as_dict() if current is not None else None,
            "records": [item.as_dict() for item in records],
        },
    )


def _optional_version(payload: dict[str, object]) -> int | None:
    version = payload.get("version")
    if version is None:
        return None
    if isinstance(version, bool) or not isinstance(version, int):
        raise BadRequest("version must be an integer")
    return version
