"""Vessel retrofit submissions: versioned, withdrawable, freezable."""

from __future__ import annotations

from portpower.ship.freeze import RetrofitFrozen, retrofits_frozen
from portpower.ship.history import ProfileHistory, ProfileRecord
from portpower.ship.registry import ProfileRegistry
from portpower.store.kvstore import RecordStore

MAX_ADDED_LOAD = 5000.0


def submit_retrofit(
    store: RecordStore,
    registry: ProfileRegistry,
    history: ProfileHistory,
    ship: str,
    added_load: float,
    effective_at: str | None = None,
) -> ProfileRecord:
    """Append a retrofit version on top of the currently effective profile.

    The new version only steers judgments from ``effective_at`` (default:
    now) onwards; judgments made before that keep the capacity they were
    made with.
    """

    if retrofits_frozen(store, ship):
        raise RetrofitFrozen(f"retrofits for ship {ship} are frozen")
    if added_load <= 0:
        raise ValueError("added load must be positive")
    if added_load > MAX_ADDED_LOAD:
        raise ValueError("added load exceeds retrofit ceiling")
    current = history.effective_now(ship)
    if current is None:
        raise LookupError(f"profile not found for ship {ship}")
    record = history.append(current.profile.with_added_load(added_load), effective_at)
    refresh_projection(registry, history, ship)
    return record


def withdraw_retrofit(
    registry: ProfileRegistry,
    history: ProfileHistory,
    ship: str,
    version: int | None = None,
) -> ProfileRecord:
    """Withdraw a submitted version, keeping the withdrawal itself on record.

    Usage settled before the withdrawal is never recalculated: the version
    stays in the archive with a ``withdrawn_at`` stamp, so replays with an
    ``as_of`` earlier than the withdrawal still see it.
    """

    record = history.withdraw(ship, version)
    refresh_projection(registry, history, ship)
    return record


def refresh_projection(
    registry: ProfileRegistry,
    history: ProfileHistory,
    ship: str,
) -> None:
    """Re-align the current-profile snapshot with the archive's effective-now."""

    record = history.effective_now(ship)
    if record is None:
        registry.drop(ship)
    else:
        registry.save(record.profile)
