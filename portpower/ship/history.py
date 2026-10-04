"""Versioned, bitemporal archive of every profile a vessel has announced.

Each submission appends a new version stamped with the moment it takes
effect (``effective_at``) and the moment the archive learned about it
(``recorded_at``). Withdrawals only stamp ``withdrawn_at`` on the version;
nothing is ever deleted, so a period that was already settled can be
replayed exactly as it was judged back then.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from portpower.ship.profile import Profile
from portpower.ship.timeline import normalize, now
from portpower.store.kvstore import RecordStore

PREFIX = "ship.history."


def history_key(ship: str) -> str:
    return PREFIX + ship


class VersionNotFound(LookupError):
    """Raised when no matching profile version exists for a vessel."""


class VersionAlreadyWithdrawn(RuntimeError):
    """Raised when withdrawing a version that is already withdrawn."""


@dataclass(frozen=True)
class ProfileRecord:
    version: int
    profile: Profile
    effective_at: str | None = None
    recorded_at: str | None = None
    withdrawn_at: str | None = None

    @property
    def withdrawn(self) -> bool:
        return self.withdrawn_at is not None

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "version": self.version,
            "effective_at": self.effective_at,
            "recorded_at": self.recorded_at,
            "withdrawn_at": self.withdrawn_at,
        }
        payload.update(self.profile.as_dict())
        return payload

    @classmethod
    def from_dict(cls, payload: object) -> "ProfileRecord | None":
        if not isinstance(payload, dict):
            return None
        profile = Profile.from_dict(payload)
        version = payload.get("version")
        if profile is None or not isinstance(version, int):
            return None
        return cls(
            version=version,
            profile=profile,
            effective_at=_optional_timestamp(payload.get("effective_at")),
            recorded_at=_optional_timestamp(payload.get("recorded_at")),
            withdrawn_at=_optional_timestamp(payload.get("withdrawn_at")),
        )


class ProfileHistory:
    def __init__(self, store: RecordStore) -> None:
        self._store = store

    def append(self, profile: Profile, effective_at: str | None = None) -> ProfileRecord:
        moment = now()
        effective = normalize(effective_at) if effective_at is not None else moment

        def mutate(document: object) -> tuple[object, ProfileRecord]:
            records = _parse(document)
            version = records[-1].version + 1 if records else 1
            record = ProfileRecord(
                version=version,
                profile=profile,
                effective_at=effective,
                recorded_at=moment,
            )
            records.append(record)
            return [item.as_dict() for item in records], record

        return self._store.update_json(history_key(profile.ship), mutate)

    def withdraw(
        self,
        ship: str,
        version: int | None = None,
        at: str | None = None,
    ) -> ProfileRecord:
        """Stamp ``withdrawn_at`` on a version, keeping the version itself.

        ``version`` defaults to the latest version that is still active.
        """

        moment = normalize(at) if at is not None else now()

        def mutate(document: object) -> tuple[object, ProfileRecord]:
            records = _parse(document)
            target = _withdraw_target(ship, records, version)
            updated = replace(target, withdrawn_at=moment)
            rewritten = [
                updated if item.version == target.version else item for item in records
            ]
            return [item.as_dict() for item in rewritten], updated

        return self._store.update_json(history_key(ship), mutate)

    def list_for(self, ship: str) -> list[ProfileRecord]:
        return _parse(self._store.get_json(history_key(ship)))

    def latest(self, ship: str) -> ProfileRecord | None:
        records = self.list_for(ship)
        return records[-1] if records else None

    def count(self, ship: str) -> int:
        return len(self.list_for(ship))

    def effective_now(self, ship: str) -> ProfileRecord | None:
        moment = now()
        return self.effective_at(ship, moment, as_of=moment)

    def effective_at(
        self,
        ship: str,
        at: str,
        as_of: str | None = None,
    ) -> ProfileRecord | None:
        """The version in force at ``at`` as the archive knew it at ``as_of``.

        ``as_of`` defaults to now. Pass a settlement moment to replay what
        was judged then: versions submitted or withdrawn after ``as_of``
        stay invisible, so settled usage is never recalculated.
        """

        moment = normalize(at)
        known = normalize(as_of) if as_of is not None else now()
        best: ProfileRecord | None = None
        for record in self.list_for(ship):
            if not _visible(record, known):
                continue
            if record.effective_at is not None and record.effective_at > moment:
                continue
            if best is None or _rank(record) > _rank(best):
                best = record
        return best


def _rank(record: ProfileRecord) -> tuple[str, int]:
    return (record.effective_at or "", record.version)


def _visible(record: ProfileRecord, as_of: str) -> bool:
    if record.recorded_at is not None and record.recorded_at > as_of:
        return False
    return record.withdrawn_at is None or record.withdrawn_at > as_of


def _parse(document: object) -> list[ProfileRecord]:
    if not isinstance(document, list):
        return []
    records: list[ProfileRecord] = []
    for item in document:
        record = ProfileRecord.from_dict(item)
        if record is not None:
            records.append(record)
    records.sort(key=lambda item: item.version)
    return records


def _withdraw_target(
    ship: str,
    records: list[ProfileRecord],
    version: int | None,
) -> ProfileRecord:
    if version is not None:
        for record in records:
            if record.version == version:
                if record.withdrawn:
                    raise VersionAlreadyWithdrawn(
                        f"ship {ship} version {version} already withdrawn"
                    )
                return record
        raise VersionNotFound(f"no version {version} for ship {ship}")
    for record in reversed(records):
        if not record.withdrawn:
            return record
    raise VersionNotFound(f"no active version for ship {ship}")


def _optional_timestamp(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None
