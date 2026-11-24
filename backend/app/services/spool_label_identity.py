"""Versioned identity proofs for labels printed from the local inventory."""

from hashlib import sha256
from hmac import compare_digest

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.database import take_write_lock
from backend.app.models.spool import Spool

LABEL_KEY_PATTERN = r"^[a-f0-9]{32}$"


class SpoolLabelChangedError(ValueError):
    pass


def label_key(spool_id: int, identity: str) -> str:
    # 128 bits keeps the proof compact on small labels. The stored UUID stays
    # fixed for the life of the row, including edits to lot and other fields.
    return sha256(f"spool-label-v2:local:{spool_id}:{identity}".encode("ascii")).hexdigest()[:32]


def matches_label_key(spool_id: int, identity: str | None, key: str) -> bool:
    return bool(identity and key.isascii() and compare_digest(label_key(spool_id, identity), key))


def local_spool_label_link(base: str, spool_id: int, identity: str | None) -> str:
    link = f"{base}/inventory?spool={spool_id}"
    if identity:
        return f"{link}&src=local&v=2&key={label_key(spool_id, identity)}"
    return link


async def lock_spool_for_label(db: AsyncSession, spool_id: int, key: str) -> None:
    """Bind a scanned-label write to the verified spool row until commit."""
    await take_write_lock(db, Spool.__table__, spool_id)
    identity = await db.scalar(
        select(Spool.label_identity).where(Spool.id == spool_id, Spool.archived_at.is_(None)).with_for_update()
    )
    if not matches_label_key(spool_id, identity, key):
        raise SpoolLabelChangedError
