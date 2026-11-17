"""Best-effort display counters, in a fresh transaction after terminal commit."""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import Session

from backend.app.core.database import take_write_lock
from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile
from backend.app.services import queue_sources
from backend.app.services.library_identity import LibraryIdentity

logger = logging.getLogger(__name__)
_PENDING = "library_usage_pending"
_COMMITTED = "library_usage_committed"
_MARKS = "library_usage_savepoints"


@dataclass(frozen=True)
class Counter:
    archive_id: int
    archive_identity: str
    source: LibraryIdentity
    epoch: int


async def stage(db, archive, library_file_id=None):
    if archive is None:
        return
    file_id = archive.library_file_id if library_file_id is None else library_file_id
    row = await db.get(LibraryFile, file_id) if file_id is not None else None
    if row is not None:
        proof = Counter(
            archive.id, archive.recovery_identity, LibraryIdentity.capture(row), queue_sources.current_epoch()
        )
        db.info.setdefault(_PENDING, set()).add(proof)


@event.listens_for(Session, "after_transaction_create")
def _savepoint(session, transaction):
    if transaction.nested:
        session.info.setdefault(_MARKS, {})[transaction] = set(session.info.get(_PENDING, ()))


@event.listens_for(Session, "after_soft_rollback")
def _rollback(session, previous):
    if previous.nested:
        session.info[_PENDING] = session.info.get(_MARKS, {}).pop(previous, set())


@event.listens_for(Session, "after_commit")
def _committed(session):
    if not session.in_nested_transaction():
        session.info.setdefault(_COMMITTED, set()).update(session.info.pop(_PENDING, set()))


@event.listens_for(Session, "after_transaction_end")
def _ended(session, transaction):
    if transaction.parent is None:
        session.info.pop(_PENDING, None)
        session.info.pop(_MARKS, None)


async def increment(factory, proof: Counter):
    if queue_sources.current_epoch() != proof.epoch:
        return
    async with queue_sources.storage_mutation():
        if queue_sources.current_epoch() == proof.epoch:
            await _increment_locked(factory, proof)


async def _increment_locked(factory, proof: Counter):
    async with factory() as db:
        await take_write_lock(db, LibraryFile.__table__, proof.source.id)
        row = await db.scalar(select(LibraryFile).where(LibraryFile.id == proof.source.id).with_for_update())
        if not proof.source.matches(row):
            return
        # A separate statement AFTER the wait: READ COMMITTED's UPDATE snapshot
        # would otherwise see a link detached while the library lock was held.
        linked = await db.scalar(
            select(PrintArchive.id).where(
                PrintArchive.id == proof.archive_id,
                PrintArchive.recovery_identity == proof.archive_identity,
                PrintArchive.library_file_id == proof.source.id,
            )
        )
        if linked is None:
            return
        await db.execute(
            update(LibraryFile)
            .where(LibraryFile.id == proof.source.id)
            .values(
                print_count=LibraryFile.print_count + 1, last_printed_at=datetime.now(timezone.utc).replace(tzinfo=None)
            )
        )
        await db.commit()


async def finish(db):
    proofs = db.info.pop(_COMMITTED, set())
    factory = async_sessionmaker(db.bind, expire_on_commit=False)
    for proof in proofs:
        try:
            await increment(factory, proof)
        except Exception as error:
            logger.warning("Library display counter skipped: error=%s", type(error).__name__)
