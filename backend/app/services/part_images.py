"""Which picture a product part shows (spec part-thumbnails §8.6, §10-§12.3; plan E4).

One module owns the question, whole:

* the only WRITER of a part's picture choice (``product_parts.image_*``) and of the photos under
  ``products/<id>/part-images/`` (task 29);
* the only READER of the effective picture (``resolve``), of the editor's state and of the
  candidates (task 28). The name -> part mapping is decided at READ time from the file metadata
  (spec §10.1): nothing here caches a resolution, so a merge, a split or a new alias changes the
  picture without a write;
* the change COLLECTOR (this part). ``mark_changed`` gathers product ids on the session and the
  WebSocket event ``part_images_changed {product_ids}`` goes after the ROOT commit, never after a
  rollback or a session closed without a commit. Photo files written for the transaction are
  removed if it does not land; files it replaced are removed only once it has. The render writer
  says what it changed through ``mark_renders_changed`` in its OWN transaction, before its commit
  -- never from a background session: in the tests every session shares one connection, and
  closing any of them rolls back everyone's work (plan E4, D5).

The event order SQLAlchemy 2.0.51 gives, measured (plan E4, D6): a savepoint release fires
``after_commit`` while ``in_nested_transaction()`` is still true; a savepoint rollback ends in
``after_soft_rollback(nested)``; a root rollback and a close without a commit both end the root
transaction without a root ``after_commit``. A savepoint ends through commit, rollback or its
context manager -- a direct synchronous ``close()`` of the nested transaction rolls its SQL back
without ``after_soft_rollback`` and would leave its marks behind; nothing calls that, and
``AsyncSessionTransaction`` offers no such method.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from backend.app.core.config import settings
from backend.app.models.library import LibraryFile
from backend.app.models.product import ProductPlate
from backend.app.services.preview_artifacts import disk
from backend.app.services.product_facets import SQL_CHUNK, id_chunks

logger = logging.getLogger(__name__)

EVENT = "part_images_changed"
_STATE = "part_images.pending"
_SHUTDOWN_GRACE_SECONDS = 5.0

_listeners: list[Callable[[set[tuple[str, int]]], None]] = []
_tasks: set[asyncio.Task] = set()


@dataclass
class _Pending:
    products: set[int] = field(default_factory=set)
    render_keys: set[tuple[str, int]] = field(default_factory=set)
    after_commit: list[Path] = field(default_factory=list)
    on_rollback: list[Path] = field(default_factory=list)
    marks: dict = field(default_factory=dict)  # savepoint -> the state when it opened


def _sync(db) -> Session:
    return getattr(db, "sync_session", db)


def _pending(db) -> _Pending:
    info = _sync(db).info
    state = info.get(_STATE)
    if state is None:
        state = info[_STATE] = _Pending()
    return state


def mark_changed(db, product_ids: Iterable[int | None]) -> None:
    """Spec §12.3: these products' pictures (or candidates) may change with this transaction."""
    _pending(db).products.update(int(i) for i in product_ids if i is not None)


def unlink_after_commit(db, path: Path) -> None:
    """Remove ``path`` (a photo, or a product's ``part-images`` directory) once the transaction lands."""
    _pending(db).after_commit.append(path)


def unlink_on_rollback(db, path: Path) -> None:
    """``path`` was written for this transaction: remove it if the transaction does not land."""
    _pending(db).on_rollback.append(path)


def subscribe(listener: Callable[[set[tuple[str, int]]], None]) -> Callable[[], None]:
    """Hear the ``(file_sha256, plate_index)`` keys of committed render transitions (tests)."""
    _listeners.append(listener)
    return lambda: _listeners.remove(listener)


async def products_of_plates(db, keys: Iterable[tuple[str, int]]) -> set[int]:
    """Every product that links one of these plates -- through a file in the trash too."""
    wanted = set(keys)
    found: set[int] = set()
    hashes = sorted({sha for sha, _plate in wanted})
    for start in range(0, len(hashes), SQL_CHUNK):
        rows = await db.execute(
            select(ProductPlate.product_id, LibraryFile.file_hash, ProductPlate.plate_index)
            .join(LibraryFile, LibraryFile.id == ProductPlate.library_file_id)
            .where(LibraryFile.file_hash.in_(hashes[start : start + SQL_CHUNK]))
        )
        found |= {product_id for product_id, sha, plate in rows.all() if (sha, plate) in wanted}
    return found


async def mark_renders_changed(db, keys: Iterable[tuple[str, int]]) -> None:
    """The render writer's door (plan E4, D5): these plates' pictures change with this transaction.

    Called in the writer's own session, before its commit. A failed lookup never raises: it runs
    in a savepoint and costs the event, never the writer's transaction. ``begin_nested()`` flushes
    before it opens the savepoint, so the caller arrives with a valid unit of work -- the render
    writer's callers write through Core statements and have nothing pending.
    """
    wanted = {(sha, int(plate)) for sha, plate in keys}
    if not wanted:
        return
    _pending(db).render_keys |= wanted
    try:
        async with db.begin_nested():
            product_ids = await products_of_plates(db, wanted)
    except Exception:
        logger.exception("Products of %d changed plate render(s) were not read; no event for them", len(wanted))
        return
    mark_changed(db, product_ids)


async def mark_files_changed(db, library_file_ids: Iterable[int]) -> None:
    """A library file went to the trash, came back or changed: every product that links it."""
    product_ids: set[int] = set()
    for chunk in id_chunks(library_file_ids):
        rows = await db.execute(select(ProductPlate.product_id).where(ProductPlate.library_file_id.in_(chunk)))
        product_ids |= set(rows.scalars())
    mark_changed(db, product_ids)


def _spawn(coro) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        logger.warning("Part image follow-up dropped: no running event loop")
        return
    task = loop.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def _remove_paths(paths: list[Path]) -> None:
    root = Path(settings.products_dir).resolve()
    for path in paths:
        try:
            if root not in path.resolve().parents:
                logger.warning("Part image cleanup refused a path outside the products root: %s", path.name)
                continue
            if path.is_dir():
                if path.name == "part-images":
                    shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Part image file %s was not removed: %s", path.name, type(exc).__name__)


def _remove_later(paths: list[Path]) -> None:
    if paths:  # owned (D21): a cancelled shutdown still waits for a removal already running
        _spawn(disk(_remove_paths, list(paths)))


async def _broadcast(product_ids: list[int]) -> None:
    from backend.app.core.websocket import ws_manager

    try:
        await ws_manager.broadcast({"type": EVENT, "data": {"product_ids": product_ids}})
    except Exception:
        logger.exception("Part image event for %d product(s) was not sent", len(product_ids))


@event.listens_for(Session, "after_transaction_create")
def _savepoint_opened(session, transaction) -> None:
    if transaction.nested:
        state = _pending(session)
        state.marks[transaction] = (
            frozenset(state.products),
            frozenset(state.render_keys),
            len(state.after_commit),
            len(state.on_rollback),
        )


@event.listens_for(Session, "after_soft_rollback")
def _savepoint_rolled_back(session, previous_transaction) -> None:
    if not previous_transaction.nested:
        return  # the root's end is handled in _root_ended, which runs first
    state = session.info.get(_STATE)
    mark = state.marks.pop(previous_transaction, None) if state is not None else None
    if mark is None:
        return
    products, render_keys, kept_after, kept_rollback = mark
    state.products = set(products)
    state.render_keys = set(render_keys)
    del state.after_commit[kept_after:]
    doomed, state.on_rollback = state.on_rollback[kept_rollback:], state.on_rollback[:kept_rollback]
    _remove_later(doomed)


@event.listens_for(Session, "after_commit")
def _committed(session) -> None:
    if session.in_nested_transaction():
        return  # a savepoint released: its marks stay with the transaction around it
    state = session.info.pop(_STATE, None)
    if state is None:
        return
    _remove_later(state.after_commit)
    if state.render_keys:
        for listener in list(_listeners):
            try:
                listener(set(state.render_keys))
            except Exception:
                logger.exception("Part render change listener failed")  # never undoes a committed write
    if state.products:
        _spawn(_broadcast(sorted(state.products)))


@event.listens_for(Session, "after_transaction_end")
def _root_ended(session, transaction) -> None:
    if transaction.parent is not None:
        return
    state = session.info.pop(_STATE, None)
    if state is not None:  # ended without a root commit -- a rollback or a close: nothing it wrote landed
        _remove_later(state.on_rollback)


async def drain() -> None:
    """Wait for every event and file removal already started (tests, shutdown)."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


async def shutdown() -> None:
    """Lifespan end: let what started finish briefly, then cancel the rest."""
    try:
        await asyncio.wait_for(drain(), _SHUTDOWN_GRACE_SECONDS)
    except TimeoutError:
        for task in list(_tasks):
            task.cancel()
        await asyncio.gather(*list(_tasks), return_exceptions=True)
