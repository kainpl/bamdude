"""The first-layer-complete notification is announced once per RUN -- the claim that survives a restart.

``main._first_layer_notified`` is process memory, and a process-fresh MQTT client delivers layer edges again:
after a restart on layers 2-10 the print's first layer was announced a second time. The fact therefore lives on
the run's execution archive (``extra_data[MARKER]``), addressed by the archive's immutable ``recovery_identity``
-- never by a printer id, a file name or a row id alone (SQLite reuses both ids).

The marker is written BEFORE the notification is sent, as a compare-and-set under the print's own write scope:
of any number of claimants one wins. A crash between the write and the send loses that one notification and can
never produce a second. ``main`` keeps its dict as a cache, seeded from the marker where a run is bound.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from backend.app.models.archive import PrintArchive
from backend.app.services.archive_write_scope import archive_write_scope, locked_print

MARKER = "first_layer_announced"


def announced(archive) -> bool:
    """Whether this archive's run has already claimed its announcement."""
    extra = getattr(archive, "extra_data", None)
    return isinstance(extra, dict) and bool(extra.get(MARKER))


async def claim(session_factory, archive_id: int, archive_identity: str) -> bool:
    """Claim the run's one announcement; ``False`` when it was claimed before -- by any process.

    One short transaction with no I/O. The row is read again under the archive's write scope (the in-process
    lock, then SQLite's writer or PostgreSQL's guard and row lock) and must still be the run's own incarnation.
    A row that is gone or belongs to another run takes no marker and answers ``True``: nothing durable can speak
    for that run, so the caller's process memory decides, as it did before the marker existed.
    """
    async with session_factory() as db, archive_write_scope(db, archive_id):
        archive = await locked_print(
            db,
            archive_id,
            select(PrintArchive)
            .where(PrintArchive.id == archive_id)
            .with_for_update()
            .execution_options(populate_existing=True),
        )
        if archive is None or archive.recovery_identity != archive_identity:
            return True
        if announced(archive):
            return False
        extra = archive.extra_data if isinstance(archive.extra_data, dict) else {}
        archive.extra_data = {**extra, MARKER: datetime.now(timezone.utc).isoformat()}
        await db.commit()
    return True
