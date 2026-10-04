"""Durable per-ship freeze that blocks new retrofit submissions."""

from __future__ import annotations

from portpower.store.kvstore import RecordStore

PREFIX = "ship.retrofit.freeze."


class RetrofitFrozen(RuntimeError):
    """Raised when a retrofit is submitted while the vessel is frozen."""


def freeze_key(ship: str) -> str:
    return PREFIX + ship


def freeze_retrofits(store: RecordStore, ship: str) -> None:
    store.put_json(freeze_key(ship), True)


def unfreeze_retrofits(store: RecordStore, ship: str) -> None:
    store.delete(freeze_key(ship))


def retrofits_frozen(store: RecordStore, ship: str) -> bool:
    return store.get_json(freeze_key(ship)) is True
