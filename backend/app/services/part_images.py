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
import functools
import hashlib
import logging
import re
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel
from sqlalchemy import event, exists, select
from sqlalchemy.orm import Session, selectinload
from starlette.responses import Response

from backend.app.core.config import settings
from backend.app.models.library import LibraryFile
from backend.app.models.plate_render import PlateRender, PlateRenderObject
from backend.app.models.product import Product, ProductPart, ProductPlate
from backend.app.schemas.part_image import (
    ImageChoice,
    InstanceImageRef,
    InstanceKey,
    PartImageCandidateOut,
    PartImageRef,
    PhotoState,
    PinState,
)
from backend.app.services.part_names import tally_objects
from backend.app.services.part_render_protocol import RENDERED, RENDERER_VERSION
from backend.app.services.preview_artifacts import disk
from backend.app.services.product_composition import part_index, part_sources, plate_objects, recipes_for_products
from backend.app.services.product_facets import SQL_CHUNK, id_chunks
from backend.app.services.product_files import product_part_images_dir

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


PHOTO_NAME = re.compile(r"[0-9a-f]{32}\.(png|jpg|webp)\Z")
_PREFERRED = ("toolpath", "model")  # over top_mask, across every source (spec §10.2, step 3)
_SHA = re.compile(r"[0-9a-f]{64}\Z")


def small_photo_name(name: str) -> str:
    return name.split(".", 1)[0] + ".sm.png"


@dataclass(frozen=True)
class _Render:
    render_id: int
    file_sha256: str
    plate_index: int
    status: str
    result_dir: str | None
    objects: dict  # identify_id -> (method, reason)


@dataclass
class _Plate:
    file: LibraryFile
    plate_index: int
    names: dict[int, str]  # identify_id -> name_key, sibling-folded over the whole plate
    owners: dict[int, int]  # identify_id -> the part it resolves to


@dataclass
class _Snapshot:
    parts: dict[int, ProductPart]
    products: dict[int, Product]
    sources: dict[int, list[_Plate]]  # part id -> its plates, in part_sources' order (files outside the trash)
    plates: dict[tuple[int, int, int], _Plate]  # (product id, file id, plate index)
    files: dict[int, LibraryFile]  # every file named, a trashed pin's too
    renders: dict[tuple[str, int], _Render]


@dataclass(frozen=True)
class _Effective:
    ref: PartImageRef
    lg: Path | None = None
    sm: Path | None = None


def _chunks(values: Iterable) -> Iterable[list]:
    ordered = sorted(set(values))
    for start in range(0, len(ordered), SQL_CHUNK):
        yield ordered[start : start + SQL_CHUNK]


async def _renders(db, hashes: Iterable[str]) -> dict[tuple[str, int], _Render]:
    rows = []
    for chunk in _chunks(h for h in hashes if h and _SHA.fullmatch(h)):
        rows += (
            await db.execute(
                select(
                    PlateRender.id,
                    PlateRender.file_sha256,
                    PlateRender.plate_index,
                    PlateRender.status,
                    PlateRender.result_dir,
                ).where(PlateRender.file_sha256.in_(chunk), PlateRender.renderer_version == RENDERER_VERSION)
            )
        ).all()
    objects: dict[int, dict] = {}
    for chunk in id_chunks(r.id for r in rows if r.result_dir):
        for o in (
            await db.execute(
                select(
                    PlateRenderObject.render_id,
                    PlateRenderObject.identify_id,
                    PlateRenderObject.method,
                    PlateRenderObject.reason,
                ).where(PlateRenderObject.render_id.in_(chunk))
            )
        ).all():
            objects.setdefault(o.render_id, {})[o.identify_id] = (o.method, o.reason)
    return {
        (r.file_sha256, r.plate_index): _Render(
            r.id, r.file_sha256, r.plate_index, r.status, r.result_dir, objects.get(r.id, {})
        )
        for r in rows
    }


async def _load(db, part_ids: Iterable[int]) -> _Snapshot:
    """Everything a set of parts' pictures stands on, in a fixed number of statements.

    ``populate_existing``: a route that just merged or deleted a part answers through here,
    and a collection loaded before its flush would still hold the gone row -- whose aliases
    would then claim the survivor's objects (plan E4, task 28).
    """
    parts: dict[int, ProductPart] = {}
    for chunk in id_chunks(part_ids):
        rows = await db.execute(
            select(ProductPart).where(ProductPart.id.in_(chunk)).execution_options(populate_existing=True)
        )
        parts.update((p.id, p) for p in rows.scalars())
    products: dict[int, Product] = {}
    for chunk in id_chunks(p.product_id for p in parts.values()):
        rows = await db.execute(
            select(Product)
            .options(selectinload(Product.plates), selectinload(Product.parts))
            .where(Product.id.in_(chunk))
            .execution_options(populate_existing=True)
        )
        products.update((p.id, p) for p in rows.scalars())
    recipes = await recipes_for_products(db, products.values())
    files: dict[int, LibraryFile] = {}
    sources: dict[int, list[_Plate]] = {}
    plates: dict[tuple[int, int, int], _Plate] = {}
    for product_id, rows in recipes.items():
        index = part_index(products[product_id].parts)
        by_plate_id: dict[int, _Plate] = {}
        for plate, file, _recipe in rows:
            files[file.id] = file
            names = {
                identify_id: tally.name_key
                for tally in tally_objects(plate_objects(file.file_metadata, plate.plate_index))
                for identify_id in tally.identify_ids
            }
            entry = _Plate(
                file=file,
                plate_index=plate.plate_index,
                names=names,
                owners={i: index[key].id for i, key in names.items() if key in index},
            )
            by_plate_id[plate.id] = entry
            plates[(product_id, file.id, plate.plate_index)] = entry
        for part_id, found in part_sources(rows, lambda _file: True).items():
            sources[part_id] = [by_plate_id[s.plate_id] for s in found]
    pinned = {p.image_file_id for p in parts.values() if p.image_source == "instance" and p.image_file_id is not None}
    for chunk in id_chunks(pinned - files.keys()):
        files.update(
            (f.id, f) for f in (await db.execute(select(LibraryFile).where(LibraryFile.id.in_(chunk)))).scalars()
        )
    renders = await _renders(db, (f.file_hash for f in files.values()))
    return _Snapshot(parts, products, sources, plates, files, renders)


def _render_v(render: _Render, identify_id: int) -> str:
    return hashlib.sha256(f"{render.render_id}:{render.result_dir}:{identify_id}".encode()).hexdigest()[:12]


def _instance(render: _Render | None, identify_id: int) -> tuple[str, str | None] | None:
    """``(status, v)`` of one object's picture -- ready / pending / missing / skipped -- or ``None``
    when none will come (no row, or a failed / unavailable plate without a result)."""
    if render is None:
        return None
    if render.result_dir is None:
        return ("pending", None) if render.status == "pending" else None
    method, _reason = render.objects.get(identify_id, ("missing", None))
    if method in RENDERED:
        return "ready", _render_v(render, identify_id)
    return method, None


def _render_paths(render: _Render, identify_id: int) -> tuple[Path, Path]:
    from backend.app.services.part_renders import result_path  # part_renders imports this module

    directory = result_path(
        Path(settings.part_renders_dir), render.file_sha256, RENDERER_VERSION, render.plate_index, render.result_dir
    )
    return directory / f"{identify_id}.lg.png", directory / f"{identify_id}.sm.png"


def _render_effective(render: _Render, identify_id: int) -> _Effective:
    lg, sm = _render_paths(render, identify_id)
    return _Effective(PartImageRef(kind="render", status="ready", v=_render_v(render, identify_id)), lg=lg, sm=sm)


def _pin(part: ProductPart, snap: _Snapshot) -> tuple[PinState, _Effective | None]:
    """Spec §10.2 / §10.4, in this order: unlinked, trashed, gone, someone else's, not rendered."""
    file = snap.files.get(part.image_file_id)
    product = snap.products.get(part.product_id)
    if file is None or product is None or all(pp.library_file_id != file.id for pp in product.plates):
        return PinState(valid=False, reason="file_unlinked"), None
    if file.deleted_at is not None:
        return PinState(valid=False, reason="file_trashed"), None
    plate = snap.plates.get((part.product_id, file.id, part.image_plate_index))
    if plate is None or part.image_identify_id not in plate.names:
        return PinState(valid=False, reason="object_gone"), None
    if plate.owners.get(part.image_identify_id) != part.id:
        return PinState(valid=False, reason="not_this_part"), None
    render = snap.renders.get((file.file_hash, part.image_plate_index))
    state = _instance(render, part.image_identify_id)
    if state is None or state[0] != "ready":
        return PinState(valid=False, reason="not_rendered"), None
    return PinState(valid=True), _render_effective(render, part.image_identify_id)


def _auto(part: ProductPart, snap: _Snapshot) -> _Effective | None:
    """Spec §10.2: sources in order, instances by id, toolpath / model before top_mask, else pending."""
    fallback: _Effective | None = None
    waiting = False
    for plate in snap.sources.get(part.id, []):
        render = snap.renders.get((plate.file.file_hash, plate.plate_index))
        if render is not None and render.result_dir is None:
            waiting = waiting or render.status == "pending"
            continue
        for identify_id in sorted(i for i, owner in plate.owners.items() if owner == part.id):
            state = _instance(render, identify_id)
            if state is None or state[0] != "ready":
                continue
            if render.objects[identify_id][0] in _PREFERRED:
                return _render_effective(render, identify_id)
            fallback = fallback or _render_effective(render, identify_id)
    if fallback is not None:
        return fallback
    if waiting:
        return _Effective(PartImageRef(kind="render", status="pending", v=None))
    return None


def _photo_path(part: ProductPart) -> Path | None:
    if part.image_source != "photo" or not part.image_photo or not PHOTO_NAME.fullmatch(part.image_photo):
        return None
    return product_part_images_dir(part.product_id) / part.image_photo


async def _photos_present(parts: Iterable[ProductPart]) -> dict[int, bool]:
    wanted = {p.id: path for p in parts if (path := _photo_path(p)) is not None}
    if not wanted:
        return {}
    return await disk(lambda: {pid: path.is_file() for pid, path in wanted.items()})  # one owned hop (D16, D21)


def _effective(part: ProductPart, snap: _Snapshot, present: dict[int, bool]) -> _Effective | None:
    photo = _photo_path(part)
    if photo is not None and present.get(part.id):
        return _Effective(
            PartImageRef(kind="photo", status="ready", v=part.image_photo),
            lg=photo,
            sm=photo.with_name(small_photo_name(photo.name)),
        )
    if part.image_source == "instance" and part.kind == "printed":
        _state, pinned = _pin(part, snap)
        if pinned is not None:
            return pinned
    return _auto(part, snap)


def _choice(part: ProductPart, snap: _Snapshot, present: dict[int, bool]) -> ImageChoice:
    if part.image_source == "instance" and part.image_file_id is not None:
        return ImageChoice(
            source="instance",
            instance=InstanceKey(
                library_file_id=part.image_file_id,
                plate_index=part.image_plate_index,
                identify_id=part.image_identify_id,
            ),
            pin=_pin(part, snap)[0],
        )
    if part.image_source == "photo":
        return ImageChoice(source="photo", photo=PhotoState(present=present.get(part.id, False)))
    return ImageChoice(source="auto")


async def _answers(db, part_ids: Iterable[int], *, editor: bool):
    snap = await _load(db, part_ids)
    present = await _photos_present(snap.parts.values())
    effective = {pid: _effective(part, snap, present) for pid, part in snap.parts.items()}
    choices = {pid: _choice(part, snap, present) for pid, part in snap.parts.items()} if editor else {}
    return effective, choices


async def resolve(db, part_ids: Iterable[int]) -> dict[int, PartImageRef | None]:
    """Spec §10.2: every part's effective picture, in a fixed number of statements."""
    effective, _choices = await _answers(db, part_ids, editor=False)
    return {pid: (e.ref if e is not None else None) for pid, e in effective.items()}


async def editor_states(db, part_ids: Iterable[int]) -> dict[int, ImageChoice]:
    """Spec §10.4: the stored choice and whether its pin / photo still holds."""
    _effective_, choices = await _answers(db, part_ids, editor=True)
    return choices


async def instance_refs(
    db, keys: Iterable[tuple[int, int, int]]
) -> dict[tuple[int, int, int], InstanceImageRef | None]:
    """``(library_file_id, plate_index, identify_id) -> its picture`` -- files outside the trash only."""
    wanted = set(keys)
    files: dict[int, LibraryFile] = {}
    for chunk in id_chunks(k[0] for k in wanted):
        files.update(
            (f.id, f)
            for f in (
                await db.execute(select(LibraryFile).where(LibraryFile.id.in_(chunk), LibraryFile.deleted_at.is_(None)))
            ).scalars()
        )
    renders = await _renders(db, (f.file_hash for f in files.values()))
    out: dict[tuple[int, int, int], InstanceImageRef | None] = {}
    for key in wanted:
        file_id, plate_index, identify_id = key
        file = files.get(file_id)
        state = _instance(renders.get((file.file_hash, plate_index)), identify_id) if file is not None else None
        out[key] = (
            InstanceImageRef(
                library_file_id=file_id, plate_index=plate_index, identify_id=identify_id, status=state[0], v=state[1]
            )
            if state is not None
            else None
        )
    return out


async def candidates(db, part_id: int, visible: Callable[[LibraryFile], bool]) -> list[PartImageCandidateOut] | None:
    """Spec §11.2: the part's instances on every linked plate outside the trash (plan E4, D18)."""
    snap = await _load(db, [part_id])
    part = snap.parts.get(part_id)
    if part is None:
        return None
    pinned = (
        (part.image_file_id, part.image_plate_index, part.image_identify_id)
        if part.image_source == "instance"
        else None
    )
    out: list[PartImageCandidateOut] = []
    for plate in snap.sources.get(part_id, []):
        render = snap.renders.get((plate.file.file_hash, plate.plate_index))
        shown = visible(plate.file)
        for identify_id in sorted(i for i, owner in plate.owners.items() if owner == part_id):
            method, reason = (
                render.objects.get(identify_id, ("missing", None))
                if render is not None and render.result_dir
                else (None, None)
            )
            state = _instance(render, identify_id)
            out.append(
                PartImageCandidateOut(
                    library_file_id=plate.file.id,
                    filename=plate.file.filename if shown else None,
                    hidden=not shown,
                    plate_index=plate.plate_index,
                    identify_id=identify_id,
                    method=method,
                    reason=reason,
                    plate_status=render.status if render is not None else "none",
                    v=state[1] if state is not None and state[0] == "ready" else None,
                    pinned=(plate.file.id, plate.plate_index, identify_id) == pinned,
                )
            )
    return out


def _existing(*paths: Path | None) -> Path | None:
    for path in paths:
        if path is not None and path.is_file():
            return path
    return None


async def image_file(db, part_id: int, size: str) -> tuple[Path, str] | None:
    """The file behind ``GET /product-parts/{id}/image``: ``(path, v)``; ``sm`` falls back to ``lg``."""
    effective = (await _answers(db, [part_id], editor=False))[0].get(part_id)
    if effective is None or effective.ref.status != "ready":
        return None
    path = await disk(_existing, effective.sm if size == "sm" else effective.lg, effective.lg)
    if path is None:
        logger.warning("Part %s picture %s is not on disk", part_id, effective.ref.kind)
        return None
    return path, effective.ref.v


async def instance_file(
    db, product_id: int, library_file_id: int, plate_index: int, identify_id: int, size: str
) -> tuple[Path, str] | None:
    """One object's picture of a plate the product links (spec §11.1); anything else is ``None`` (one 404)."""
    linked = await db.scalar(
        select(
            exists().where(
                ProductPlate.product_id == product_id,
                ProductPlate.library_file_id == library_file_id,
                ProductPlate.plate_index == plate_index,
            )
        )
    )
    file = await db.get(LibraryFile, library_file_id) if linked else None
    if file is None or file.deleted_at is not None:
        return None
    render = (await _renders(db, [file.file_hash])).get((file.file_hash, plate_index))
    state = _instance(render, identify_id)
    if state is None or state[0] != "ready":
        return None
    lg, sm = _render_paths(render, identify_id)
    path = await disk(_existing, sm if size == "sm" else lg, lg)
    return (path, state[1]) if path is not None else None


@functools.cache
def part_image_shapes() -> dict[type[BaseModel], str]:
    """Spec §11.3: every wire shape that carries a part's picture, and where it names the part.

    ``id`` -- the part model itself; ``part_id`` -- a row naming a part; ``instance`` -- a plate
    object (its file and plate come from the enclosing ``plate``, which carries no picture of its
    own). Looked up through the MRO, so a subclass (``StockMovementRowOut``) is covered. Built
    lazily: schemas/stock.py imports a service, and services must not import schemas at load time
    in an order that can close a cycle. The structural guard (tests/unit/test_part_image_shapes.py)
    fails on a new shape that names a part and is neither here nor explained in its allowlist.
    """
    from backend.app.schemas import finished_stock, listing, product, project

    return {
        product.ProductPartResponse: "id",
        product.PlateYieldEntry: "part_id",
        product.StockBalanceOut: "part_id",
        product.StockMovementOut: "part_id",
        project.PartFiguresOut: "part_id",
        project.LinePurchasedPartOut: "part_id",
        project.DroppedPartOut: "part_id",
        project.PlanPartCount: "part_id",
        project.PartStateOut: "part_id",
        finished_stock.StockItemPartOut: "part_id",
        finished_stock.StockJournalRow: "part_id",
        listing.ProductPartRow: "part_id",
        product.PlateUnassignedEntry: "instance",
        product.PlateRecipeResponse: "plate",
    }


def _kind(value: BaseModel) -> str | None:
    shapes = part_image_shapes()
    for cls in type(value).__mro__:
        if cls in shapes:
            return shapes[cls]
    return None


def _collect(value, plate, parts: list, instances: list) -> None:
    if isinstance(value, BaseModel):
        kind = _kind(value)
        if kind == "plate":
            plate = (value.library_file_id, value.plate_index)
        elif kind == "id":
            parts.append((value, value.id))
        elif kind == "part_id" and value.part_id is not None:
            parts.append((value, value.part_id))
        elif kind == "instance" and plate is not None and value.identify_id is not None:
            instances.append((value, (*plate, value.identify_id)))
        for name in type(value).model_fields:
            _collect(getattr(value, name), plate, parts, instances)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect(item, plate, parts, instances)
    elif isinstance(value, dict):
        for item in value.values():
            _collect(item, plate, parts, instances)


async def fill(db, payload, *, editor: bool = False) -> None:
    """Spec §11.3: give every part and plate object in ``payload`` its picture, in ONE read."""
    parts: list = []
    instances: list = []
    _collect(payload, None, parts, instances)
    if parts:
        effective, choices = await _answers(db, {pid for _obj, pid in parts}, editor=editor)
        for obj, pid in parts:
            found = effective.get(pid)
            obj.image = found.ref if found is not None else None
            if editor and _kind(obj) == "id":
                obj.image_choice = choices.get(pid)
    if instances:
        found = await instance_refs(db, {key for _obj, key in instances})
        for obj, key in instances:
            obj.image = found.get(key)


def attach(fn=None, *, editor: bool = False):
    """Mark a route whose answer shows parts: after the handler, ``fill`` its answer (plan E4, D4).

    ``editor`` also fills ``image_choice`` (spec §10.4) -- the product page and the part's doors.
    A ``Response`` passes through untouched. The route-coverage guard reads ``__part_images__``.
    """

    def wrap(endpoint):
        @functools.wraps(endpoint)
        async def run(*args, **kwargs):
            result = await endpoint(*args, **kwargs)
            if not isinstance(result, Response):
                await fill(kwargs["db"], result, editor=editor)
            return result

        run.__part_images__ = "editor" if editor else "image"
        return run

    return wrap(fn) if fn is not None else wrap
