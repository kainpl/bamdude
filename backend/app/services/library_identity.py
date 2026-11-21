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
    """The one proof that a library row is still the row a caller read (G03 B, A32).

    Captured when the row is read, asked under the lock where the caller decides. Every door that answers
    ``source_changed`` (or ``superseded``) for a library source stores this — never a tuple of its own.
    ``recovery_identity`` is the row's own identity (m253): id, path, hash and creation time alone can all
    be the next row's on SQLite. A capture made before m253 has none and is compared by the other fields.
    """

    id: int
    path: str
    sha256: str | None
    created: str | None
    recovery_identity: str | None = None

    @classmethod
    def capture(cls, row):
        return cls(row.id, row.file_path, row.file_hash, _time(row.created_at), getattr(row, "recovery_identity", None))

    def matches(self, row) -> bool:
        if row is None:
            return False
        current = type(self).capture(row)
        if self.recovery_identity is None:
            return (self.id, self.path, self.sha256, self.created) == (
                current.id,
                current.path,
                current.sha256,
                current.created,
            )
        return self == current


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
