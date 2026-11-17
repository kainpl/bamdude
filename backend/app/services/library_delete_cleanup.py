"""Exact library paths follow the root commit, never a SAVEPOINT or rollback."""

import logging
from pathlib import Path

from sqlalchemy import event, or_, select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import Session

from backend.app.core.config import settings
from backend.app.models.library import LibraryFile
from backend.app.services import queue_sources
from backend.app.services.preview_artifacts import disk

logger = logging.getLogger(__name__)
_KEY = "library_delete_cleanup"
_READY = "library_delete_cleanup_committed"


def stage(db, removal):
    state = db.info.setdefault(_KEY, {"items": [], "marks": {}})
    state["items"].append(removal)


@event.listens_for(Session, "after_transaction_create")
def _savepoint(session, transaction):
    state = session.info.get(_KEY)
    if transaction.nested and state is not None:
        state["marks"][transaction] = len(state["items"])


@event.listens_for(Session, "after_soft_rollback")
def _rollback(session, previous):
    state = session.info.get(_KEY)
    if previous.nested and state is not None:
        del state["items"][state["marks"].pop(previous, 0) :]


@event.listens_for(Session, "after_commit")
def _commit(session):
    if not session.in_nested_transaction():
        state = session.info.pop(_KEY, None)
        if state is not None:
            session.info.setdefault(_READY, []).extend(state["items"])
            from backend.app.services.content_delete_rows import FolderRemoval
            from backend.app.services.virtual_printer import virtual_printer_manager

            # The pre-F target has no pending badge; the accepted F composition
            # does. Mark synchronously at commit before the first await in sync.
            mark = getattr(virtual_printer_manager, "mark_config_pending", None)
            if mark is not None:
                for item in state["items"]:
                    if isinstance(item, FolderRemoval):
                        for vp_id in item.vp_ids:
                            mark(vp_id)


@event.listens_for(Session, "after_transaction_end")
def _ended(session, transaction):
    if transaction.parent is None:
        session.info.pop(_KEY, None)


def _owned_path(stored):
    if not stored:
        return None
    root = Path(settings.base_dir).resolve()
    path = Path(stored)
    # Reject an outside absolute path BEFORE resolving it (it may be a NAS).
    if path.is_absolute():
        try:
            path.relative_to(root)
        except ValueError:
            return None
    else:
        path = root / path
    try:
        resolved = path.resolve()
    except (ValueError, OSError):
        return None
    owned = (Path(settings.library_dir), Path(settings.archive_dir) / "library", root / "library-files")
    if not any(resolved.is_relative_to(folder.resolve()) for folder in owned):
        return None
    return path


async def finish(db):
    """Call outside the root mutation scope after commit. No new task/outbox is created."""
    removals = db.info.pop(_READY, [])
    if not removals:
        return
    factory = async_sessionmaker(db.bind, expire_on_commit=False)
    for removal in removals:
        from backend.app.services.content_delete_rows import FolderRemoval

        if isinstance(removal, FolderRemoval):
            from backend.app.services.virtual_printer import virtual_printer_manager

            try:
                await virtual_printer_manager.sync_from_db()
            except Exception as error:
                logger.warning("Virtual folder configuration sync failed: error=%s", type(error).__name__)
            continue
        candidates = (
            *((removal.thumbnail_path,) if removal.external else (removal.file_path, removal.thumbnail_path)),
            *removal.covers,
        )
        try:
            async with queue_sources.storage_mutation(), factory() as check:
                # The id may already belong to a new file. Never clear its covers
                # or a shared path on the strength of the deleted row's id.
                if await check.get(LibraryFile, removal.id) is not None:
                    continue
                paths = []
                for stored in candidates:
                    path = _owned_path(stored)
                    if path is None:
                        continue
                    names = {stored, str(path), path.relative_to(Path(settings.base_dir).resolve()).as_posix()}
                    shared = await check.scalar(
                        select(LibraryFile.id)
                        .where(or_(LibraryFile.file_path.in_(names), LibraryFile.thumbnail_path.in_(names)))
                        .limit(1)
                    )
                    if shared is None:
                        from backend.app.models.library_file_makerworld_meta import LibraryFileMakerworldMeta

                        shared_cover = await check.scalar(
                            select(LibraryFileMakerworldMeta.id)
                            .where(
                                or_(
                                    LibraryFileMakerworldMeta.cover_path.in_(names),
                                    LibraryFileMakerworldMeta.variant_cover_path.in_(names),
                                )
                            )
                            .limit(1)
                        )
                        if shared_cover is None:
                            paths.append(path)
                await check.rollback()
                for path in paths:
                    await disk(path.unlink, missing_ok=True)
        except Exception as error:
            logger.warning("Library cleanup after commit failed: file=%s error=%s", removal.id, type(error).__name__)
