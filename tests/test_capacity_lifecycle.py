"""End-to-end checks for versioned declared capacity (manual test harness).

Run with: python3 -m tests.test_capacity_lifecycle
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone

from portpower.audit.ledger import Ledger
from portpower.console.runtime import build_runtime
from portpower.ns.harbor import Harbor
from portpower.ship.capacity_ledger import CapacityLedger
from portpower.ship.declaration_service import DeclarationService
from portpower.ship.retrofit_request import (
    RetrofitFrozen,
    RetrofitNotRevocable,
    RetrofitRegistry,
)
from portpower.ship.settlement import AlreadySettled, SettlementLedger
from portpower.store.kvstore import RecordStore

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}" + (f" -- {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


def iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


def build_service(clock: FakeClock):
    store_dir = tempfile.mkdtemp()
    store = RecordStore(store_dir)
    store.load()
    capacities = CapacityLedger(store)
    retrofits = RetrofitRegistry(store)
    settlements = SettlementLedger(store)
    ledger = Ledger(store)
    service = DeclarationService(
        capacities=capacities,
        retrofits=retrofits,
        settlements=settlements,
        ledger=ledger,
        clock=clock,
    )
    return service, capacities, retrofits, settlements, ledger, store


def main() -> int:
    clock = FakeClock(datetime(2026, 10, 1, tzinfo=timezone.utc))
    service, capacities, retrofits, settlements, ledger, store = build_service(clock)
    ship = "MV-TEST"

    # 1. Baseline declaration at 1000 kW.
    v1 = capacities.baseline(ship, 1000.0, effective_at=iso(clock.now), clock=clock)
    check("baseline is version 1", v1.version == 1 and v1.capacity == 1000.0)

    # 2. New declaration governs only after its effective moment; as-of works.
    clock.advance(days=10)
    t_before = iso(clock.now)
    req = service.submit(ship, 500.0)
    check("request submitted", req.status == "submitted" and req.request_id)

    clock.advance(hours=2)
    review_at = clock.now
    decision = service.review(
        ship, req.request_id, approve=True,
        effective_at=iso(review_at + timedelta(days=7)),
    )
    check("approval produced pending v2", decision.version.version == 2
          and decision.version.status == "pending")

    # Before effective date, as-of still returns v1.
    clock.advance(days=1)
    mid_review = iso(clock.now)
    got = service.capacity_at(ship, mid_review)
    check("as-of before effective date still shows v1=1000",
          got is not None and got.version == 1 and got.capacity == 1000.0)

    # Effective read still v1 while v2 pending; current head shows pending.
    eff = service.current_capacity(ship)
    check("effective capacity stays 1000 while v2 pending",
          eff is not None and eff.version == 1 and eff.capacity == 1000.0)

    # 3. Settlement before the change settles against v1.
    s1 = service.settle_usage(ship, "2026-10-01T00:00:00+00:00",
                              "2026-10-10T00:00:00+00:00", 120.0, 0.95)
    check("settlement pinned to v1", s1.capacity_version == 1
          and s1.capacity == 1000.0 and s1.charge == 114.0)

    # 4. Revocation while pending: v2 marked revoked, request revoked,
    #    already-settled entry untouched.
    revoked, revoked_version = service.revoke(ship, req.request_id, "typo in form")
    check("request marked revoked", revoked.status == "revoked"
          and revoked.revoked_at is not None and revoked.revoke_reason == "typo in form")
    check("pending v2 marked revoked",
          revoked_version is not None and revoked_version.status == "revoked")

    check("settled v1 entry unchanged after revoke",
          settlements.for_window(ship, s1.period_start, s1.period_end) == s1)

    # As-of queries still return v1; revoked v2 never governs.
    clock.advance(days=30)
    got = service.capacity_at(ship, iso(clock.now))
    check("as-of long after revoked v2 still returns v1",
          got is not None and got.version == 1)

    # Revoking an already-revoked request fails.
    try:
        service.revoke(ship, req.request_id)
        check("double revoke rejected", False)
    except RetrofitNotRevocable:
        check("double revoke rejected", True)

    # 5. Corrected resubmission creates a fresh request and v2 later on.
    clock.advance(days=1)
    t2_submit = iso(clock.now)
    req2 = service.submit(ship, 300.0,
                          effective_at=iso(clock.now + timedelta(days=7)))
    clock.advance(hours=1)
    d2 = service.review(ship, req2.request_id, approve=True,
                        effective_at=iso(clock.now + timedelta(days=7)))
    check("corrected retrofit approved as new pending version v3",
          d2.version.version == 3 and d2.version.capacity == 1300.0)

    clock.advance(days=8)
    activated = service.activate_due(ship)
    check("due version activates to v3=1300",
          activated is not None and activated.version == 3
          and activated.capacity == 1300.0)

    chain = capacities.list_for(ship)
    statuses = [(v.version, v.status, v.capacity) for v in chain]
    check("chain retains revoked and superseded records",
          statuses == [
              (1, "superseded", 1000.0),
              (2, "revoked", 1500.0),
              (3, "active", 1300.0),
          ], str(statuses))

    # Historical reconstruction at three moments.
    check("as-of t1 -> 1000 v1",
          (lambda x: x is not None and x.version == 1 and x.capacity == 1000.0)(
              service.capacity_at(ship, "2026-10-05T00:00:00+00:00")))
    check("as-of during pending/revoked window -> 1000 v1",
          (lambda x: x is not None and x.version == 1)(
              service.capacity_at(ship, "2026-10-20T00:00:00+00:00")))
    check("as-of after v3 effective -> 1300",
          (lambda x: x is not None and x.capacity == 1300.0)(
              service.capacity_at(ship, iso(clock.now))))

    # 6. Effective version cannot be revoked -> settled usage never reopened.
    try:
        service.revoke(ship, req2.request_id)
        check("revoke after effective date rejected", False)
    except RetrofitNotRevocable:
        check("revoke after effective date rejected", True)

    # Settlement after change is pinned to v2; the v1 settlement is immutable.
    s2 = service.settle_usage(ship, "2026-11-19T00:00:00+00:00",
                              "2026-11-26T00:00:00+00:00", 200.0, 0.95)
    check("second settlement pinned to v3=1300",
          s2.capacity_version == 3 and s2.capacity == 1300.0 and s2.charge == 190.0)
    check("first settlement still 114.0 on v1", s1.charge == 114.0
          and s1.capacity_version == 1)

    try:
        service.settle_usage(ship, s1.period_start, s1.period_end, 999.0, 0.95)
        check("duplicate settlement window rejected", False)
    except AlreadySettled:
        check("duplicate settlement window rejected", True)

    # 7. Freeze during audit: submissions explicitly refused.
    service.freeze(ship)
    check("freeze flag visible", service.is_frozen(ship))
    try:
        service.submit(ship, 100.0)
        check("submission while frozen rejected", False)
    except RetrofitFrozen as exc:
        check("submission while frozen rejected", "frozen" in str(exc))

    service.unfreeze(ship)
    req3 = service.submit(ship, 100.0)
    check("submission accepted after unfreeze",
          req3.status == "submitted")

    # 8. Audit trail carries submit/approve/revoke/freeze/settle events.
    events = {e.event for e in ledger.entries()}
    for expected in ("retrofit.submit", "retrofit.approve", "retrofit.revoke",
                     "retrofit.freeze", "retrofit.unfreeze",
                     "retrofit.effective", "settlement.record"):
        check(f"audit event {expected}", expected in events, str(sorted(events)))

    # 9. Persistence: reopen the store and reconstruct.
    reopened_capacities = CapacityLedger(store)
    reopened_settlements = SettlementLedger(store)
    reopened_retrofits = RetrofitRegistry(store)
    got = reopened_capacities.as_of(ship, iso(clock.now))
    check("after reopen, as-of gives 1300 v3",
          got is not None and got.capacity == 1300.0 and got.version == 3)
    check("after reopen, two settlements retained",
          len(reopened_settlements.list_for(ship)) == 2)
    check("after reopen, request history retained",
          len(reopened_retrofits.list_for(ship)) == 3)

    # 10. Full HTTP runtime still wires.
    rt = build_runtime(Harbor("pp", "port"), RecordStore(tempfile.mkdtemp()))
    check("http runtime has declaration service",
          hasattr(rt, "declarations") and hasattr(rt, "capacities"))

    print()
    if failures:
        print(f"{len(failures)} FAILURES: {failures}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
