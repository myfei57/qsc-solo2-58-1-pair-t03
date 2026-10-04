"""HTTP-level checks for the versioned capacity API."""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone

from portpower.console.runtime import build_runtime
from portpower.console.routes import build_router
from portpower.ns.harbor import Harbor
from portpower.store.kvstore import RecordStore


class MutableClock:
    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment


def make_router(clock: MutableClock):
    store = RecordStore(tempfile.mkdtemp())
    store.load()
    runtime = build_runtime(Harbor("pp", "port"), store, clock=clock)
    return build_router(runtime)


def call(router, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    from urllib.parse import parse_qs, urlsplit

    from portpower.console.errors import error_response
    from portpower.console.http import Request

    parsed = urlsplit(path)
    request = Request(
        method=method,
        path=parsed.path,
        query=parse_qs(parsed.query, keep_blank_values=True),
        headers={},
        body=json.dumps(body).encode() if body is not None else b"",
    )
    try:
        response = router.dispatch(request)
    except Exception as exc:
        response = error_response(exc)
    return response.status, json.loads(response.payload.decode())


def main() -> int:
    clock = MutableClock(datetime(2026, 10, 1, tzinfo=timezone.utc))
    router = make_router(clock)
    failures: list[str] = []

    def check(label: str, condition: bool, detail="") -> None:
        print(f"[{'PASS' if condition else 'FAIL'}] {label}{(' -- ' + detail) if detail and not condition else ''}")
        if not condition:
            failures.append(label)

    ship = "MV-HULL-7"

    status, body = call(router, "POST", "/capacity/declare",
                        {"ship": ship, "capacity": 1000.0})
    check("declare baseline 200", status == 200 and body["version"]["version"] == 1,
          str(body))

    status, body = call(router, "POST", "/capacity/declare",
                        {"ship": ship, "capacity": 1200.0})
    check("second baseline refused 400", status == 400, str((status, body)))

    # Freeze then submit -> 409.
    status, body = call(router, "POST", "/retrofit/freeze", {"ship": ship})
    check("freeze 200", status == 200 and body["frozen"] is True, str(body))

    status, body = call(router, "POST", "/retrofit/submit",
                        {"ship": ship, "added_load": 500.0})
    check("submit while frozen -> 409", status == 409 and "frozen" in body["error"],
          str((status, body)))

    status, _ = call(router, "POST", "/retrofit/unfreeze", {"ship": ship})
    check("unfreeze 200", status == 200)

    status, body = call(router, "POST", "/retrofit/submit",
                        {"ship": ship, "added_load": 500.0})
    check("submit after unfreeze 200", status == 200, str(body))
    request_id = body["request"]["request_id"]

    # Approval with future effective_at.
    status, body = call(router, "POST", "/retrofit/review", {
        "ship": ship, "request_id": request_id, "approve": True,
        "effective_at": "2026-12-25T00:00:00+00:00",
    })
    check("approve 200 pending", status == 200
          and body["version"]["status"] == "pending"
          and body["version"]["capacity"] == 1500.0, str(body))

    # Current stays 1000; as-of after effective is still 1000 until tick.
    status, body = call(router, "GET", f"/capacity/current?ship={ship}")
    check("current stays 1000 before effective",
          body["current"]["capacity"] == 1000.0, str(body))

    status, body = call(router, "GET",
                        f"/capacity/as-of?ship={ship}&at=2026-10-15T00:00:00%2B00:00")
    check("as-of October -> 1000 v1",
          body["found"] and body["version"]["version"] == 1, str(body))

    # Revoke before effective -> 200 and version revoked.
    status, body = call(router, "POST", "/retrofit/revoke", {
        "ship": ship, "request_id": request_id, "reason": "wrong figure",
    })
    check("revoke approved request 200",
          status == 200 and body["request"]["status"] == "revoked"
          and body["version"]["status"] == "revoked", str(body))

    # Revoke again -> 409.
    status, body = call(router, "POST", "/retrofit/revoke", {
        "ship": ship, "request_id": request_id,
    })
    check("second revoke -> 409", status == 409, str((status, body)))

    # Version chain shows 1 active + 2 revoked.
    status, body = call(router, "GET", f"/capacity/versions?ship={ship}")
    chain = [(v["version"], v["status"]) for v in body["versions"]]
    check("chain visible with revoked record", chain == [(1, "active"), (2, "revoked")],
          str(chain))

    # Settlement + immutability.
    status, s1 = call(router, "POST", "/settlement", {
        "ship": ship,
        "period_start": "2026-10-01T00:00:00+00:00",
        "period_end": "2026-10-31T00:00:00+00:00",
        "usage": 100.0,
    })
    check("settle pinned v1", status == 200
          and s1["settlement"]["capacity_version"] == 1
          and s1["settlement"]["charge"] == 95.0, str(s1))

    status, body = call(router, "POST", "/settlement", {
        "ship": ship,
        "period_start": "2026-10-01T00:00:00+00:00",
        "period_end": "2026-10-31T00:00:00+00:00",
        "usage": 999.0,
    })
    check("duplicate settlement -> 409", status == 409, str((status, body)))

    status, body = call(router, "GET", f"/settlements?ship={ship}")
    check("settlements list immutable",
          body["count"] == 1 and body["settlements"][0]["usage"] == 100.0
          and body["total_charge"] == 95.0, str(body))

    # Clock is already past the first (revoked) window; submit anew.
    status, body = call(router, "POST", "/retrofit/submit", {
        "ship": ship, "added_load": 250.0,
        "effective_at": "2026-11-10T00:00:00+00:00",
    })
    rid2 = body["request"]["request_id"]
    call(router, "POST", "/retrofit/review", {
        "ship": ship, "request_id": rid2, "approve": True,
        "effective_at": "2026-11-10T00:00:00+00:00",
    })
    clock.moment = datetime(2026, 11, 11, tzinfo=timezone.utc)
    status, body = call(router, "POST", "/retrofit/tick", {"ship": ship})
    check("tick activates due version 1250",
          status == 200 and body["activated"]
          and body["version"]["capacity"] == 1250.0, str(body))

    status, body = call(router, "GET",
                        f"/capacity/as-of?ship={ship}&at=2026-11-11T00:00:00%2B00:00")
    check("as-of after activation -> 1250",
          body["version"]["capacity"] == 1250.0, str(body))

    # Revoking an effective request -> 409.
    status, body = call(router, "POST", "/retrofit/revoke", {
        "ship": ship, "request_id": rid2,
    })
    check("revoke effective -> 409", status == 409, str((status, body)))

    # Audit trail.
    status, body = call(router, "GET", "/audit")
    events = {e["event"] for e in body["entries"]}
    check("audit covers lifecycle",
          {"capacity.declare", "retrofit.submit", "retrofit.approve",
           "retrofit.revoke", "retrofit.freeze", "retrofit.effective",
           "settlement.record"} <= events,
          str(sorted(events)))

    print()
    if failures:
        print(f"{len(failures)} FAILURES: {failures}")
        return 1
    print("ALL HTTP CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
