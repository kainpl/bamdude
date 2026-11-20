"""Bounded account-content deletion. One guarded plan, one root commit, no cascade shortcut."""

import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import delete, func, literal, or_, select, union_all, update

from backend.app.core.auth_lifetime import guard_accounts, stage_end_sessions, validate_captured
from backend.app.core.database import Base
from backend.app.models.api_key import APIKey
from backend.app.models.archive import PrintArchive
from backend.app.models.auth_ephemeral import AuthEphemeralToken
from backend.app.models.auth_session import AuthSession
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.library import LibraryFile
from backend.app.models.long_lived_token import LongLivedToken
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.product import ProductPart, ProductPlate
from backend.app.models.project import Project
from backend.app.models.project_line import ProjectLine
from backend.app.models.settings import Settings
from backend.app.models.user import User
from backend.app.models.user_otp_code import UserOTPCode
from backend.app.services import finished_stock, order_fulfilment, order_journal, part_stock, swap_terminal
from backend.app.services.archive import ArchiveService
from backend.app.services.archive_write_scope import archive_file_reference_scope
from backend.app.services.library_delete_cleanup import finish
from backend.app.services.library_delete_scope import footprint, library_delete_scope, product_ids_for_files
from backend.app.services.library_identity import LibraryIdentity
from backend.app.services.library_trash import library_trash_service
from backend.app.services.product_gate import ProductBusy, lock_nowait
from backend.app.services.queue_counters import (
    companion_auto_ids,
    detach_auto_queue_refs,
    detach_print_queue_refs,
    lock_auto_rows,
    update_queue_counters,
)

# The complete closure exceeded one second at both 25 and 16 in the
# isolated two-CPU WAL witness. Keep the implementation below that ceiling.
MAX_PARENTS = 8
MAX_CHILDREN = 1000
USER_POLICY = json.loads(
    (Path(__file__).parents[1] / "data/content_delete_user_reference_policy.json").read_text(encoding="utf-8")
)
_PARENTS = (PrintArchive, LibraryFile, PrintQueueItem, AutoQueueItem)


USER_CONTENT_DELETE_MESSAGES = {
    "user_content_delete_too_large": "This account has too much content to delete at once. Remove content through its ordinary pages, then retry.",
    "user_content_delete_active": "This account still owns active or retained print work. Resolve it through the queue or recovery page, then retry.",
    "user_content_delete_changed": "Account content changed while deletion was waiting. Retry the operation.",
}


def refused(code, **details):
    return HTTPException(409, {"error": code, "message": USER_CONTENT_DELETE_MESSAGES[code], **details})


@dataclass(frozen=True)
class Plan:
    parents: tuple[tuple, ...]
    files: tuple[LibraryIdentity, ...]
    read_files: tuple[LibraryIdentity, ...]
    orders: tuple[int, ...]
    lines: tuple[int, ...]
    products: tuple[int, ...]
    child_counts: tuple[tuple[str, int], ...]
    independent_copies: tuple[tuple[int, str | None], ...]
    lanes: tuple[int, ...]

    def ids(self, table):
        return [row[1] for row in self.parents if row[0] == table]


async def _token_condition(db, capture):
    keys = await db.scalars(select(APIKey.connector_identity).where(APIKey.user_id == capture.user_id))
    tokens = await db.scalars(select(LongLivedToken.token_identity).where(LongLivedToken.user_id == capture.user_id))
    return or_(
        (AuthEphemeralToken.account_identity == capture.account_identity)
        & AuthEphemeralToken.key_identity.is_(None)
        & AuthEphemeralToken.token_identity.is_(None),
        AuthEphemeralToken.key_identity.in_(list(keys)),
        AuthEphemeralToken.token_identity.in_(list(tokens)),
    )


async def child_counts(db, capture, parents, order_ids, line_ids, product_ids, lanes, read_files):
    """Conservative row bounds, including non-FK credential bindings. No mutation here."""
    conditions = defaultdict(list)
    for key in USER_POLICY:
        table_name, column = key.split(".")
        table = Base.metadata.tables[table_name]
        conditions[table_name].append(table.c[column] == capture.user_id)
    roots = {
        name: select(Base.metadata.tables[name].c.id).where(Base.metadata.tables[name].c.id.in_(ids))
        for name, ids in parents.items()
        if ids
    }
    # Every immediate domain child is touched, retained, detached or deleted by
    # its owner. Count conservatively even when two paths reach the same row.
    for table in Base.metadata.tables.values():
        for fk in table.foreign_keys:
            if fk.column.table.name in roots and fk.column.name == "id":
                conditions[table.name].append(fk.parent.in_(roots[fk.column.table.name]))
    conditions[AuthSession.__tablename__].append(AuthSession.account_identity == capture.account_identity)
    conditions[AuthEphemeralToken.__tablename__].append(await _token_condition(db, capture))
    conditions[UserOTPCode.__tablename__].append(UserOTPCode.account_identity == capture.account_identity)
    conditions[Settings.__tablename__].append(Settings.key == f"user_{capture.user_id}_email_2fa_enabled")
    from backend.app.models.finished_stock import StockItem
    from backend.app.models.product import ProductFacet, ProductPlate, product_files

    conditions[ProductFacet.__tablename__].append(ProductFacet.product_id.in_(product_ids))
    conditions[PrinterQueue.__tablename__].append(PrinterQueue.id.in_(lanes))
    conditions[StockItem.__tablename__].append(StockItem.product_id.in_(product_ids))
    conditions[ProductPlate.__tablename__].append(ProductPlate.product_id.in_(product_ids))
    conditions[product_files.name].append(product_files.c.product_id.in_(product_ids))
    # Preserve each domain's exact count without one round trip per table while
    # the writer is held. UNION ALL never merges equal counts across domains.
    counts = dict(
        (
            await db.execute(
                union_all(
                    *(
                        select(literal(name), func.count())
                        .select_from(Base.metadata.tables[name])
                        .where(or_(*predicates))
                        for name, predicates in conditions.items()
                    )
                )
            )
        ).all()
    )
    # C1/unfile can add one journal and one credit per printed product part;
    # these INSERTs must be bounded too, before taking the writer.
    part_count = int(
        await db.scalar(select(func.count()).select_from(ProductPart).where(ProductPart.product_id.in_(product_ids)))
        or 0
    )
    counts["new_unfile_rows_bound"] = len(order_ids) + 2 * part_count * len(parents.get("print_archives", ()))
    counts["guarded_domain_rows_bound"] = (
        len(order_ids) + len(line_ids) + len(product_ids) + part_count + len(read_files)
    )
    from backend.app.services.product_facets import facets_of

    metadata = dict(
        (
            await db.execute(
                select(LibraryFile.id, LibraryFile.file_metadata).where(
                    LibraryFile.id.in_([*parents.get("library_files", ()), *[proof.id for proof in read_files]])
                )
            )
        ).all()
    )
    plates = list(
        await db.execute(
            select(ProductPlate.product_id, ProductPlate.library_file_id, ProductPlate.plate_index)
            .where(ProductPlate.product_id.in_(product_ids))
            .limit(MAX_CHILDREN + 1)
        )
    )
    if len(plates) > MAX_CHILDREN:
        raise refused("user_content_delete_too_large", parent_limit=MAX_PARENTS, child_limit=MAX_CHILDREN)
    wanted = set()
    for product_id, file_id, plate_index in plates:
        if file_id not in parents.get("library_files", ()):
            wanted.update(
                (product_id, file_id, kind, value) for kind, value in facets_of(metadata.get(file_id), plate_index)
            )
        if len(wanted) > MAX_CHILDREN:
            raise refused("user_content_delete_too_large", parent_limit=MAX_PARENTS, child_limit=MAX_CHILDREN)
    counts["new_product_facets_bound"] = len(wanted)
    counts["account_update_and_delete"] = 2
    return tuple(sorted(counts.items()))


async def plan(db, capture, *, inspect_files=True):
    from backend.app.services.queue_source_release import independent_archive_bytes

    parents, files, order_ids, line_ids = [], [], set(), set()
    ids_by_table = {}
    selected_queue = []
    for model in _PARENTS:
        rows = list(
            await db.scalars(
                select(model)
                .where(model.created_by_id == capture.user_id)
                .order_by(model.id)
                .limit(MAX_PARENTS + 1)
                .execution_options(populate_existing=True)
            )
        )
        if len(parents) + len(rows) > MAX_PARENTS:
            raise refused("user_content_delete_too_large", parent_limit=MAX_PARENTS, child_limit=MAX_CHILDREN)
        ids_by_table[model.__tablename__] = [row.id for row in rows]
        for row in rows:
            identity = getattr(row, "recovery_identity", None)
            parents.append(
                (
                    model.__tablename__,
                    row.id,
                    identity,
                    getattr(row, "project_id", None),
                    getattr(row, "project_line_id", None),
                )
            )
            if isinstance(row, LibraryFile):
                files.append(LibraryIdentity.capture(row))
            if isinstance(row, PrintQueueItem):
                selected_queue.append(row)
            if getattr(row, "project_id", None) is not None:
                order_ids.add(row.project_id)
            if getattr(row, "project_line_id", None) is not None:
                line_ids.add(row.project_line_id)
    # Companion router rows are also removed by detach_print_queue_refs.
    companion_ids = set(await companion_auto_ids(db, [row.id for row in selected_queue]))
    if companion_ids:
        already = set(ids_by_table.get("auto_queue_items", ()))
        for row in await db.scalars(select(AutoQueueItem).where(AutoQueueItem.id.in_(companion_ids - already))):
            parents.append(
                (AutoQueueItem.__tablename__, row.id, row.recovery_identity, row.project_id, row.project_line_id)
            )
            ids_by_table.setdefault("auto_queue_items", []).append(row.id)
            if row.project_id is not None:
                order_ids.add(row.project_id)
        if len(parents) > MAX_PARENTS:
            raise refused("user_content_delete_too_large", parent_limit=MAX_PARENTS, child_limit=MAX_CHILDREN)
    lines = list(
        await db.scalars(select(ProjectLine).where(ProjectLine.project_id.in_(order_ids)).order_by(ProjectLine.id))
    )
    line_ids.update(row.id for row in lines)
    scope = await footprint(
        db,
        [row.id for row in files],
        archive_ids=ids_by_table.get("print_archives", ()),
        queue_item_ids=ids_by_table.get("print_queue", ()),
        auto_item_ids=ids_by_table.get("auto_queue_items", ()),
    )
    products = tuple(sorted({row.product_id for row in lines if row.product_id is not None} | set(scope.products)))
    # Unfiling can credit products of a FOREIGN library source too. Reserve its
    # proof/gates/parts now, not after the order and print locks. Keeper files
    # also supply the facet refresh performed by the product owner.
    provenance = set(
        await db.scalars(
            select(PrintArchive.library_file_id).where(PrintArchive.id.in_(ids_by_table.get("print_archives", ())))
        )
    ) - {None}
    products = tuple(sorted(set(products) | await product_ids_for_files(db, provenance)))
    if len(products) + len(line_ids) > MAX_CHILDREN:
        raise refused("user_content_delete_too_large", parent_limit=MAX_PARENTS, child_limit=MAX_CHILDREN)
    keepers = set(
        await db.scalars(
            select(ProductPlate.library_file_id)
            .where(ProductPlate.product_id.in_(products))
            .distinct()
            .limit(MAX_PARENTS + 1)
        )
    )
    read_ids = (provenance | keepers) - set(ids_by_table.get("library_files", ()))
    if len(parents) + len(read_ids) > MAX_PARENTS:
        raise refused("user_content_delete_too_large", parent_limit=MAX_PARENTS, child_limit=MAX_CHILDREN)
    read_files = tuple(
        LibraryIdentity.capture(row)
        for row in await db.scalars(
            select(LibraryFile)
            .where(LibraryFile.id.in_(read_ids))
            .order_by(LibraryFile.id)
            .execution_options(populate_existing=True)
        )
    )
    lanes = tuple(scope.lanes)
    counts = await child_counts(db, capture, ids_by_table, order_ids, line_ids, products, lanes, read_files)
    if sum(value for _, value in counts) > MAX_CHILDREN:
        raise refused("user_content_delete_too_large", parent_limit=MAX_PARENTS, child_limit=MAX_CHILDREN)
    copies = []
    if inspect_files:
        for row in selected_queue:
            if (
                row.queue_source_id is not None
                and row.status != "pending"
                and row.archive_id is not None
                and row.archive_id not in ids_by_table.get("print_archives", ())
            ):
                copies.append((row.id, await independent_archive_bytes(db, row.archive_id)))
    return Plan(
        tuple(parents),
        tuple(files),
        read_files,
        tuple(sorted(order_ids)),
        tuple(sorted(line_ids)),
        products,
        counts,
        tuple(copies),
        lanes,
    )


async def _keep_checks(db, planned):
    reasons = []
    archives = list(await db.scalars(select(PrintArchive).where(PrintArchive.id.in_(planned.ids("print_archives")))))
    from backend.app.services.content_delete_keeps import archive_keep_reasons

    for archive in archives:
        reasons.extend(await archive_keep_reasons(db, archive))
    for model in (PrintQueueItem, AutoQueueItem):
        selected = planned.ids(model.__tablename__)
        rows = await db.scalars(select(model).where(model.id.in_(selected)))
        for row in rows:
            companion = isinstance(row, AutoQueueItem) and row.assigned_to_item_id in planned.ids("print_queue")
            if (
                row.status not in {"pending", "completed", "failed", "cancelled"}
                and not (companion and row.status == "assigned")
            ) or (row.status == "pending" and getattr(row, "started_at", None) is not None):
                reasons.append("active_job")
            if isinstance(row, PrintQueueItem):
                queue = await db.get(PrinterQueue, row.queue_id)
                if queue is not None and queue.current_item_id == row.id:
                    reasons.append("current_item")
                if row.status != "pending" and row.archive_id is not None:
                    archive = await db.get(PrintArchive, row.archive_id)
                    printer = await db.get(Printer, queue.printer_id) if queue is not None else None
                    if archive is not None:
                        if archive.status not in {"completed", "failed", "cancelled", "archived"}:
                            reasons.append("archive_open")
                        if printer is not None and printer.awaiting_plate_clear_archive_id == archive.id:
                            reasons.append("plate_gate")
                        if swap_terminal.claim_stage(
                            archive.extra_data
                        ) == swap_terminal.CLAIM_OWED or swap_terminal.sending(archive.id):
                            reasons.append("swap_owed")
            elif row.assigned_to_item_id is not None:
                companion = await db.get(PrintQueueItem, row.assigned_to_item_id)
                if companion is not None and companion.id not in planned.ids("print_queue"):
                    reasons.append("assigned_job")
            if isinstance(row, AutoQueueItem) and row.recovery_identity:
                if (
                    await db.scalar(
                        select(PrintQueueItem.id)
                        .where(
                            PrintQueueItem.source_auto_identity == row.recovery_identity,
                            PrintQueueItem.status.in_(("printing", "paused", "dispatching")),
                        )
                        .limit(1)
                    )
                    is not None
                ):
                    reasons.append("active_execution")
                executions = await db.scalars(
                    select(PrintArchive).where(PrintArchive.source_auto_identity == row.recovery_identity)
                )
                for execution in executions:
                    if execution.status in {"printing", "paused", "dispatching"}:
                        reasons.append("active_execution")
                    if (
                        await db.scalar(
                            select(Printer.id).where(Printer.awaiting_plate_clear_archive_id == execution.id).limit(1)
                        )
                        is not None
                    ):
                        reasons.append("plate_gate")
                    if swap_terminal.claim_stage(
                        execution.extra_data
                    ) == swap_terminal.CLAIM_OWED or swap_terminal.sending(execution.id):
                        reasons.append("swap_owed")
    from backend.app.models.queue_source import QueueSource

    for model in (PrintQueueItem, AutoQueueItem):
        foreign = await db.execute(
            select(model, QueueSource.state)
            .outerjoin(QueueSource, QueueSource.id == model.queue_source_id)
            .where(
                or_(
                    model.library_file_id.in_(planned.ids("library_files")),
                    model.archive_id.in_(planned.ids("print_archives")),
                ),
                model.id.not_in(planned.ids(model.__tablename__)),
                model.status.in_(("printing", "assigned", "dispatching", "paused")),
            )
        )
        if any(state != "ready" for _row, state in foreign):
            reasons.append("foreign_live_source")
    reasons.extend("archive_copy_missing" for _, reason in planned.independent_copies if reason is not None)
    if reasons:
        raise refused(
            "user_content_delete_active", reasons=sorted(set(reasons)), counts={"parents": len(planned.parents)}
        )


async def detach_account_rows(db, capture):
    """The reviewed 35 user edges. No SQLite FK inference or anonymous preference."""
    from backend.app.services import library_file_photos, stock_issues

    owners = {
        "library_file_photos.uploaded_by_id": library_file_photos.detach_user,
        "project_events.user_id": order_journal.detach_user,
        "product_part_stock_movements.created_by": part_stock.detach_user,
        "stock_item_movements.created_by": finished_stock.detach_user,
        "stock_issues.created_by": stock_issues.detach_user,
    }

    await db.execute(delete(AuthEphemeralToken).where(await _token_condition(db, capture)))
    await db.execute(delete(UserOTPCode).where(UserOTPCode.account_identity == capture.account_identity))
    await db.execute(delete(AuthSession).where(AuthSession.account_identity == capture.account_identity))
    for key, rule in USER_POLICY.items():
        name, column_name = key.split(".")
        if key in owners:
            await owners[key](db, capture.user_id)
            continue
        table = Base.metadata.tables[name]
        column = table.c[column_name]
        if rule["ondelete"] == "CASCADE":
            await db.execute(delete(table).where(column == capture.user_id))
        else:
            values = {column_name: None}
            values.update({col.name: col for col in table.c if col.onupdate is not None})
            await db.execute(update(table).where(column == capture.user_id).values(values))
    await db.execute(delete(Settings).where(Settings.key == f"user_{capture.user_id}_email_2fa_enabled"))


async def delete_content(db, target, admin, current_user, creds, check_target):
    from contextlib import AsyncExitStack

    from backend.app.api.routes._workshop_rights import ensure_may_file

    planned = await plan(db, target)
    principal = None

    async def first(session):
        nonlocal principal
        guard = await guard_accounts(session, [admin._auth_capture, target], require_active=False)
        principal = await validate_captured(
            session, admin._auth_capture, required_permissions=("users:delete",), proof=guard
        )
        if not principal.user.is_admin:
            raise HTTPException(403, "Administrator required")
        user = await session.get(User, target.user_id, populate_existing=True)
        await check_target(session, user, current_user)

    async def early(session):
        await order_fulfilment.lock_orders(session, planned.orders)
        await finished_stock.lock_positions_for_lines(session, planned.lines)
        for line_id in planned.lines:
            line = await session.get(ProjectLine, line_id, populate_existing=True)
            if line is not None:
                await finished_stock.lock_line(session, line)

    async with library_delete_scope(
        db,
        planned.files,
        read_identities=planned.read_files,
        first=first,
        early=early,
        archive_ids=planned.ids("print_archives"),
        queue_item_ids=planned.ids("print_queue"),
        auto_item_ids=planned.ids("auto_queue_items"),
        extra_products=planned.products,
    ):
        fresh = await plan(db, target, inspect_files=False)
        if (
            fresh.parents != planned.parents
            or fresh.files != planned.files
            or fresh.read_files != planned.read_files
            or fresh.orders != planned.orders
            or fresh.lines != planned.lines
            or fresh.products != planned.products
            or fresh.child_counts != planned.child_counts
            or fresh.lanes != planned.lanes
        ):
            raise refused("user_content_delete_changed")
        await _keep_checks(db, planned)
        archives = list(
            await db.scalars(
                select(PrintArchive).where(PrintArchive.id.in_(planned.ids("print_archives"))).order_by(PrintArchive.id)
            )
        )
        by_order = defaultdict(list)
        for archive in archives:
            if archive.project_id is not None and archive.deleted_at is None:
                by_order[archive.project_id].append(archive)
        if by_order:
            await ensure_may_file(creds, [row for rows in by_order.values() for row in rows])
        if planned.orders:
            from backend.app.api.routes._workshop_rights import ensure_consequence
            from backend.app.core.permissions import Permission

            await ensure_consequence(creds, Permission.ORDERS_READ)
        for order_id, rows in by_order.items():
            try:
                await order_fulfilment.ensure_prints_can_leave(db, order_id, [row.id for row in rows])
            except order_fulfilment.FulfilmentError as error:
                raise HTTPException(error.status, str(error)) from error
        parts = list(
            await db.scalars(
                select(ProductPart).where(ProductPart.product_id.in_(planned.products)).order_by(ProductPart.id)
            )
        )
        await part_stock.lock_parts(db, parts)
        affected = []
        for order_id in planned.orders:
            order = await db.get(Project, order_id)
            affected.append(
                {
                    "id": order_id,
                    "name": order.name if order else "",
                    "prints": len(by_order.get(order_id, ())),
                    "jobs": sum(
                        1
                        for row in planned.parents
                        if row[0] in {"print_queue", "auto_queue_items"} and row[3] == order_id
                    ),
                }
            )
        removals = []
        service = ArchiveService(db)
        async with AsyncExitStack() as paths:
            for stored in sorted({row.file_path for row in archives if row.file_path}):
                await paths.enter_async_context(archive_file_reference_scope(db, stored))
            for order_id, rows in by_order.items():
                payload = {
                    "count": len(rows),
                    "archive_ids": [row.id for row in rows],
                    "prints": [{"archive_id": row.id, "name": row.print_name or row.filename} for row in rows],
                }
                for row in rows:
                    row.project_id = row.project_line_id = None
                    await part_stock.credit_unfiled_print(
                        db, row, created_by=principal.user.id, note=part_stock.NOTE_UNFILED_FROM_ORDER
                    )
                await order_journal.record(db, order_id, "prints_unfiled", payload, actor=principal.user)
            for row in archives:
                removals.append(await service.delete_archive_rows(row))
            for proof in planned.files:
                row = await db.get(LibraryFile, proof.id, populate_existing=True)
                await library_trash_service.purge_rows(db, row)
            queue_ids = planned.ids("print_queue")
            # Every router row this deletion writes -- its planned rows, the companions of the printer rows it
            # deletes, the back-references its detach nulls -- locked ascending as one set (G03 B, A20).
            backrefs = await db.scalars(
                select(AutoQueueItem.id).where(AutoQueueItem.assigned_to_item_id.in_(queue_ids))
            )
            await lock_auto_rows(
                db, [*planned.ids("auto_queue_items"), *await companion_auto_ids(db, queue_ids), *backrefs]
            )
            await detach_print_queue_refs(db, queue_ids)
            await db.execute(delete(PrintQueueItem).where(PrintQueueItem.id.in_(queue_ids)))
            await detach_auto_queue_refs(db, planned.ids("auto_queue_items"))
            await db.execute(delete(AutoQueueItem).where(AutoQueueItem.id.in_(planned.ids("auto_queue_items"))))
            for lane in planned.lanes:
                await update_queue_counters(db, lane)
            await stage_end_sessions(db, target, "account_deleted")
            await detach_account_rows(db, target)
            # The account guard no longer blocks a row that merely names the account (A30), so the deletion keeps
            # its target from gaining referencing rows at its end: the row upgraded FOR UPDATE NOWAIT -- a granted
            # key-share check of an in-flight insert is the deletion's refusal, nothing written; once upgraded, a
            # later insert naming the account waits and then fails its own check.
            try:
                await lock_nowait(db, User.__table__, [target.user_id])
            except ProductBusy as exc:
                raise refused("user_content_delete_changed") from exc
            await db.execute(delete(User).where(User.account_identity == target.account_identity))
            await db.commit()
        # File-path process scopes above have ended; all planned archive
        # process guards remain owned until these fresh postcommit checks finish.
        for removal in removals:
            await service.cleanup_archive_removal(removal)
    await finish(db)
    return {
        "deleted": True,
        "counts": {
            "archives": len(planned.ids("print_archives")),
            "library_files": len(planned.files),
            "queue_items": len(planned.ids("print_queue")),
            "auto_queue_items": len(planned.ids("auto_queue_items")),
        },
        "affected_orders": affected,
    }
