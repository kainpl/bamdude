"""A whole library mutation plan: process locks, library, product, prints, jobs.

Call before any mutation. Recursive/bulk helpers reuse the root plan; they may
not discover a new member or enter a lower gate after an earlier iteration.
"""

from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import select, text, true

from backend.app.core.lock_ledger import LIBRARY_FILE, before_lock, ledger
from backend.app.models.archive import PrintArchive
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.library import LibraryFile, LibraryFolder
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.product import Product, ProductFacet, ProductPlate, product_files, product_folders
from backend.app.services import queue_sources
from backend.app.services.archive_write_scope import lock_prints
from backend.app.services.library_identity import FolderIdentity, LibraryIdentity
from backend.app.services.product_gate import PRODUCT_BUSY, ProductBusy, lock_nowait, lock_nowait_where, product_gate
from backend.app.services.queue_ops import claim_lanes

_OWNED = "library_delete_plan"


def changed():
    return HTTPException(409, {"error": "source_changed", "message": "Library content changed; retry the operation."})


@dataclass(frozen=True)
class Footprint:
    products: tuple[int, ...]
    archives: tuple[int, ...]
    jobs: tuple[tuple, ...]
    archive_proofs: tuple[tuple, ...] = ()

    @property
    def lanes(self):
        return sorted({row[3] for row in self.jobs if row[0] == "queue"})


async def product_ids_for_files(db, ids, *, folder_ids=()):
    products = set(await db.scalars(select(product_files.c.product_id).where(product_files.c.library_file_id.in_(ids))))
    products.update(await db.scalars(select(Product.id).where(Product.origin_file_id.in_(ids))))
    products.update(
        await db.scalars(
            select(product_folders.c.product_id).where(product_folders.c.library_folder_id.in_(folder_ids))
        )
    )
    for model in (ProductPlate, ProductFacet):
        products.update(await db.scalars(select(model.product_id).where(model.library_file_id.in_(ids))))
    return products


async def footprint(db, ids, *, archive_ids=(), queue_item_ids=(), auto_item_ids=(), folder_ids=()) -> Footprint:
    products = await product_ids_for_files(db, ids, folder_ids=folder_ids)
    archives = set(archive_ids)
    archives.update(await db.scalars(select(PrintArchive.id).where(PrintArchive.library_file_id.in_(ids))))
    jobs = []
    for kind, model in (("auto", AutoQueueItem), ("queue", PrintQueueItem)):
        from sqlalchemy import or_

        selected = queue_item_ids if kind == "queue" else auto_item_ids
        conditions = [model.library_file_id.in_(ids), model.archive_id.in_(archive_ids), model.id.in_(selected)]
        if kind == "queue":
            conditions.append(PrintQueueItem.source_auto_item_id.in_(auto_item_ids))
        else:
            conditions.append(AutoQueueItem.assigned_to_item_id.in_(queue_item_ids))
        rows = await db.scalars(
            select(model).where(or_(*conditions)).order_by(model.id).execution_options(populate_existing=True)
        )
        for row in rows:
            jobs.append(
                (
                    kind,
                    row.id,
                    row.recovery_identity,
                    row.queue_id if kind == "queue" else None,
                    row.library_file_id,
                    row.archive_id,
                    row.project_id,
                    row.project_line_id,
                    row.status,
                    row.queue_source_id,
                    getattr(row, "source_auto_item_id", None),
                    getattr(row, "source_auto_identity", None),
                    getattr(row, "assigned_to_item_id", None),
                )
            )
            if row.archive_id is not None:
                archives.add(row.archive_id)
    archive_proofs = []
    ordered_archives = sorted(archives)
    for start in range(0, len(ordered_archives), 500):
        archive_proofs.extend(
            tuple(row)
            for row in await db.execute(
                select(PrintArchive.id, PrintArchive.recovery_identity, PrintArchive.library_file_id)
                .where(PrintArchive.id.in_(ordered_archives[start : start + 500]))
                .order_by(PrintArchive.id)
            )
        )
    return Footprint(tuple(sorted(products)), tuple(ordered_archives), tuple(jobs), tuple(archive_proofs))


async def lock_library(db, ids):
    result = {}
    for file_id in sorted(set(ids)):
        before_lock(db, "library_files", file_id, LIBRARY_FILE)
        row = await db.scalar(
            select(LibraryFile)
            .where(LibraryFile.id == file_id)
            # NO KEY UPDATE also serializes edits/deletes, but does not block
            # an existing print writer's FK KEY SHARE while it finishes its
            # archive. Deletion upgrades only after the print guards.
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        )
        ledger(db).note("library_files", file_id, LIBRARY_FILE)
        if row is not None:
            result[file_id] = row
    return result


@asynccontextmanager
async def library_delete_scope(
    db,
    identities,
    *,
    first=None,
    archive_ids=(),
    queue_item_ids=(),
    auto_item_ids=(),
    extra_products=(),
    early=None,
    folder_proofs=(),
    read_identities=(),
):
    proofs = {item.id: item for item in identities}
    library_proofs = {item.id: item for item in read_identities}
    library_proofs.update(proofs)
    owned = db.info.get(_OWNED)
    if owned is not None:
        if any(owned.get(key) != value for key, value in library_proofs.items()):
            raise changed()
        yield
        return
    if db.new or db.dirty or db.deleted:
        raise RuntimeError("library deletion requires a clean preflight session")
    folder_ids = [proof.id for proof in folder_proofs]
    extra = {
        "archive_ids": archive_ids,
        "queue_item_ids": queue_item_ids,
        "auto_item_ids": auto_item_ids,
        "folder_ids": folder_ids,
    }
    plan = await footprint(db, proofs, **extra)
    await db.commit()  # close SQLite's old reader BEFORE the new plan takes its writer

    async def before(session):
        if first is not None:
            await first(session)
        if session.get_bind().dialect.name == "sqlite" and not session.in_transaction():
            await session.execute(text("BEGIN IMMEDIATE"))
        rows = await lock_library(session, library_proofs)
        if set(rows) != set(library_proofs) or any(
            not proof.matches(rows[key]) for key, proof in library_proofs.items()
        ):
            raise changed()
        try:
            await product_gate(session, [*plan.products, *extra_products])
            await lock_nowait_where(session, ProductPlate.__table__, ProductPlate.library_file_id.in_(proofs))
            await lock_nowait_where(session, ProductFacet.__table__, ProductFacet.product_id.in_(plan.products))
            await lock_nowait_where(session, product_files, product_files.c.library_file_id.in_(proofs))
            # A folder DELETE may cascade on PostgreSQL. NOWAIT after the early
            # product gates both prevents a new child after the final reread
            # and refuses a busy existing folder owner instead of a new wait cycle.
            await lock_nowait(session, LibraryFolder.__table__, folder_ids)
            current_folders = tuple(
                FolderIdentity.capture(row)
                for row in await session.scalars(
                    select(LibraryFolder)
                    .where(LibraryFolder.id.in_(folder_ids))
                    .order_by(LibraryFolder.id)
                    .execution_options(populate_existing=True)
                )
            )
            if current_folders != tuple(folder_proofs):
                raise changed()
            if folder_ids:
                from backend.app.models.library_scan import LibraryScanJob

                active_scan = await session.scalar(
                    select(LibraryScanJob.id)
                    .where(LibraryScanJob.folder_id.in_(folder_ids), LibraryScanJob.status.in_(("queued", "running")))
                    .limit(1)
                )
                if active_scan is not None:
                    raise HTTPException(
                        409, "A library scan is still using this folder. Wait for it to finish, then retry."
                    )
            if early is not None:
                await early(session)
        except ProductBusy as error:
            raise HTTPException(409, {"error": "product_busy", "message": PRODUCT_BUSY}) from error

    async with (
        queue_sources.storage_mutation(),
        claim_lanes(db, plan.lanes, archive_ids=plan.archives, before_db=before),
    ):
        for start in range(0, len(plan.archives), 500):
            await lock_prints(db, plan.archives[start : start + 500])
        for kind, row_id, _identity, lane, *_details in plan.jobs:
            model = AutoQueueItem if kind == "auto" else PrintQueueItem
            # A job only in the lane the plan read it in, which is held (L1); one moved since is the plan's refusal
            # when its footprint is read again.
            await db.execute(
                select(model.id)
                .where(model.id == row_id, true() if kind == "auto" else PrintQueueItem.queue_id == lane)
                .with_for_update(key_share=True)
            )
        try:
            # The early NO KEY UPDATE let existing print writers finish their
            # FK backfill without a print -> library wait cycle. Upgrade only
            # our already-held rows, without waiting: no new FK child can
            # appear after the final footprint read and escape this plan.
            await lock_nowait(db, LibraryFile.__table__, library_proofs)
            await lock_nowait(db, Product.__table__, [*plan.products, *extra_products])
        except ProductBusy as error:
            raise changed() from error
        if await footprint(db, proofs, **extra) != plan:
            raise changed()
        db.info[_OWNED] = proofs
        try:
            yield
        finally:
            db.info.pop(_OWNED, None)


def require_library_plan(db, file):
    proof = (db.info.get(_OWNED) or {}).get(file.id)
    if proof is None or not proof.matches(file):
        raise RuntimeError("library mutation must use its complete owning plan")


async def selected_files(db, file_ids=(), folder_ids=()):
    folders = set(folder_ids)
    frontier = set(folder_ids)
    while frontier:
        children = (
            set(await db.scalars(select(LibraryFolder.id).where(LibraryFolder.parent_id.in_(frontier)))) - folders
        )
        folders.update(children)
        frontier = children
    ids = set(file_ids)
    ids.update(await db.scalars(select(LibraryFile.id).where(LibraryFile.folder_id.in_(folders))))
    rows = await db.scalars(select(LibraryFile).where(LibraryFile.id.in_(ids)).order_by(LibraryFile.id))
    folder_rows = await db.scalars(
        select(LibraryFolder)
        .where(LibraryFolder.id.in_(folders))
        .order_by(LibraryFolder.id)
        .execution_options(populate_existing=True)
    )
    return tuple(LibraryIdentity.capture(row) for row in rows), tuple(
        FolderIdentity.capture(row) for row in folder_rows
    )


async def guard_delete_actor(db, auth_result):
    user, all_files = auth_result
    if user is None:
        return
    from backend.app.core.auth_lifetime import guard_accounts, validate_captured

    capture = getattr(user, "_auth_capture", None)
    if capture is None:
        raise HTTPException(401, "Authentication required")
    proof = await guard_accounts(db, [capture])
    await validate_captured(
        db, capture, required_permissions=("library:delete_all" if all_files else "library:delete_own",), proof=proof
    )
