"""A captured library incarnation, never authority obtained from a late numeric lookup."""

from dataclasses import dataclass
from datetime import datetime, timezone


def _time(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.isoformat()


@dataclass(frozen=True)
class LibraryIdentity:
    id: int
    path: str
    sha256: str | None
    created: str | None

    @classmethod
    def capture(cls, row):
        return cls(row.id, row.file_path, row.file_hash, _time(row.created_at))

    def matches(self, row) -> bool:
        return row is not None and self == type(self).capture(row)

    @classmethod
    def from_proof(cls, value):
        file_id, path, sha256, created = value
        return cls(file_id, path, sha256, _time(created))


@dataclass(frozen=True)
class FolderIdentity:
    id: int
    name: str
    parent_id: int | None
    external_path: str | None
    created: str | None

    @classmethod
    def capture(cls, row):
        return cls(row.id, row.name, row.parent_id, row.external_path, _time(row.created_at))
