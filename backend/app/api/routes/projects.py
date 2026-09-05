"""Orders (projects): lines of products for a customer, figures from the archive.

Spec: docs/superpowers/specs/2026-09-02-projects-redesign-design.md.
Route handlers never commit — the get_db dependency does.
"""

import asyncio
import logging
import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.core.auth import RequireCameraStreamToken, RequirePermission
from backend.app.core.config import settings
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.archive import PrintArchive
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.customer import Customer
from backend.app.models.part_stock import ProductPartStockMovement
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.product import Product, ProductPart, ProductPlate
from backend.app.models.project import Project
from backend.app.models.project_line import ProjectLine, ProjectProcurement
from backend.app.models.user import User
from backend.app.schemas.auto_queue import AutoQueueItemCreate
from backend.app.schemas.project import (
    PROJECT_PRIORITIES,
    PROJECT_STATUSES,
    BankSurplusResponse,
    BatchAddArchives,
    BatchAddQueueItems,
    LinePlanOut,
    LineProductOut,
    OrderPlanResponse,
    PartFiguresOut,
    PlanAlternativeOut,
    PlanEnqueueCreated,
    PlanEnqueueRequest,
    PlanEnqueueResponse,
    PlanPartCount,
    PlanRowOut,
    PlanTotalsOut,
    ProcurementOut,
    ProcurementUpdate,
    ProjectCreate,
    ProjectDuplicate,
    ProjectFiguresOut,
    ProjectLineCreate,
    ProjectLineResponse,
    ProjectLineUpdate,
    ProjectListResponse,
    ProjectResponse,
    ProjectUpdate,
    StockMovedOut,
    TimelineEvent,
)
from backend.app.services import part_stock
from backend.app.services.auto_queue_add import add_items_to_auto_queue
from backend.app.services.order_metrics import (
    attribute,
    grouped_figures,
    load_order_context,
    procurement_figures,
    project_figures,
)
from backend.app.services.plan_engine import OrderPlan, plan_for_order
from backend.app.services.print_option_defaults import preference_options
from backend.app.services.product_composition import PlateRecipe, recipes_for_products
from backend.app.services.product_files import (
    ALLOWED_ATTACHMENT_EXTENSIONS,
    COVER_EXTENSIONS,
    IMAGE_CONTENT_TYPES,
    effective_cover,
)
from backend.app.services.queue_batch import enqueue_batch_copies

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects", tags=["projects"])


# ---------- response building ----------


async def _get_project(db: AsyncSession, project_id: int) -> Project:
    project = (
        await db.execute(
            select(Project)
            .options(selectinload(Project.lines), selectinload(Project.customer))
            .where(Project.id == project_id)
        )
    ).scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


async def _response(db: AsyncSession, project_id: int) -> ProjectResponse:
    """The one response builder — every mutating handler returns through it.

    ⚠️ Lines are added and removed through ``Project.lines``, never with a bare
    ``db.add(ProjectLine(project_id=...))``. An eager loader does not overwrite
    a collection it finds already loaded, so a line filed straight into the
    table would be missing from the very answer that reports it.
    """
    await db.flush()
    ctx = await load_order_context(db, project_id)
    if ctx is None:
        raise HTTPException(status_code=404, detail="Project not found")
    figs, other = attribute(ctx)
    project = ctx.project
    customer = await db.get(Customer, project.customer_id) if project.customer_id else None
    lines = [
        ProjectLineResponse(
            id=line.id,
            product_id=line.product_id,
            product_name=ctx.products_by_id[line.product_id].name if line.product_id in ctx.products_by_id else "?",
            quantity=line.quantity,
            material=line.material,
            color=line.color,
            note=line.note,
            sort_order=line.sort_order,
            units_printed=figs[line.id].units_printed,
            from_stock_units=figs[line.id].from_stock_units,
            progress=figs[line.id].progress,
            parts=[
                PartFiguresOut(
                    part_id=p.part_id,
                    name=p.name,
                    qty_per_unit=p.qty_per_unit,
                    need=p.need,
                    usable=p.usable,
                    in_progress=p.in_progress,
                    remaining=p.remaining,
                    surplus=p.surplus,
                )
                for p in figs[line.id].parts
            ],
            archive_ids=list(figs[line.id].archive_ids),
        )
        for line in ctx.lines
    ]
    pf = project_figures(ctx, figs, other)
    return ProjectResponse(
        id=project.id,
        name=project.name,
        customer_id=project.customer_id,
        customer_name=customer.name if customer else None,
        description=project.description,
        color=project.color,
        status=project.status,
        notes=project.notes,
        attachments=project.attachments,
        tags=project.tags,
        due_date=project.due_date,
        priority=project.priority,
        price=project.price,
        url=project.url,
        cover_image_filename=project.cover_image_filename,
        created_at=project.created_at,
        updated_at=project.updated_at,
        lines=lines,
        procurement=[
            ProcurementOut(part_id=p.part_id, name=p.name, need=p.need, acquired=p.acquired, remaining=p.remaining)
            for p in procurement_figures(ctx)
        ],
        figures=ProjectFiguresOut(**pf.__dict__),
        other_archive_ids=[a.id for a in other],
    )


# ---------- CRUD ----------


@router.get("", response_model=list[ProjectListResponse])
@router.get("/", response_model=list[ProjectListResponse])
async def list_projects(
    status: str | None = None,
    customer_id: int | None = None,
    product_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_READ),
):
    query = (
        select(Project)
        .options(selectinload(Project.lines), selectinload(Project.customer))
        .order_by(Project.updated_at.desc())
    )
    if status:
        # Same answer ``update_project`` gives an unknown status: an empty list
        # reads as "no orders like that" and hides the typo — most cruelly for
        # ``archived``, which m158 retired and which a stale bookmark still asks for.
        if status not in PROJECT_STATUSES:
            raise HTTPException(status_code=400, detail="Invalid status")
        query = query.where(Project.status == status)
    if customer_id is not None:
        query = query.where(Project.customer_id == customer_id)
    if product_id is not None:
        # "Where is this product ordered?" — a subquery over the lines rather
        # than a join, so an order carrying two lines of the same product is
        # still one row. Composes with the filters above.
        query = query.where(Project.id.in_(select(ProjectLine.project_id).where(ProjectLine.product_id == product_id)))
    projects = (await db.execute(query)).scalars().all()
    product_ids = {line.product_id for p in projects for line in p.lines}
    # The order card draws a cover strip per line, and the EFFECTIVE cover may be
    # the first picture attachment rather than the column — hence a flag per
    # line, not a filename. One grouped lookup rather than a query per row.
    covered = (
        {
            product.id
            for product in (await db.execute(select(Product).where(Product.id.in_(product_ids)))).scalars()
            if effective_cover(product) is not None
        }
        if product_ids
        else set()
    )
    # One batched load for the whole page instead of an order context per row.
    # The figures are the same ones the detail endpoint answers — same loader,
    # same arithmetic, asked once (``services/order_metrics.grouped_figures``).
    figures = {
        order.project_id: order for order in await grouped_figures(db, project_ids=[project.id for project in projects])
    }
    out: list[ProjectListResponse] = []
    for project in projects:
        pf = figures.get(project.id)
        if pf is None:  # deleted between the two statements; nothing to report
            continue
        out.append(
            ProjectListResponse(
                id=project.id,
                name=project.name,
                customer_id=project.customer_id,
                customer_name=project.customer.name if project.customer else None,
                color=project.color,
                status=project.status,
                due_date=project.due_date,
                priority=project.priority,
                price=project.price,
                tags=project.tags,
                cover_image_filename=project.cover_image_filename,
                created_at=project.created_at,
                lines_count=len(project.lines),
                ordered=pf.ordered,
                printed=pf.printed,
                progress=pf.progress,
                # ``(sort_order, id)`` — the order every figure path puts the
                # lines in. The relationship's own order is the database's, so
                # the cover strip on the card would otherwise be free to differ
                # from the line list on the order page it opens.
                line_products=[
                    LineProductOut(product_id=line.product_id, has_cover=line.product_id in covered)
                    for line in sorted(project.lines, key=lambda line: (line.sort_order, line.id))
                ],
            )
        )
    return out


async def _check_customer(db: AsyncSession, customer_id: int | None) -> None:
    if customer_id is not None and await db.get(Customer, customer_id) is None:
        raise HTTPException(status_code=404, detail="Customer not found")


async def _check_product(db: AsyncSession, product_id: int) -> Product:
    product = await db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    return product


async def _reserve(db: AsyncSession, line: ProjectLine, units: int, user: User | None) -> None:
    """Rewrite a line's free-stock reservation (pass 8, Decision 4).

    ⚠️ Never commits — the reservation and the line edit that asked for it are
    ONE transaction, closed by ``get_db`` after the response is built. That is
    also why ``_response`` reads the reservation back out of the ledger rather
    than being handed the return value: the number on the wire is then the same
    number every other reader will see.

    A refusal is a 409 like the rest of the stock surface. In practice a
    reservation cannot be refused — ``move`` clamps it to what is on the shelf
    instead (Ruling 1) — but the writer decides that, not this route.
    """
    try:
        await part_stock.reserve_for_line(db, line, units, created_by=user.id if user else None)
    except part_stock.PartStockError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e


async def _release(db: AsyncSession, line: ProjectLine, note: str) -> None:
    """Put a line's reservation back (line deleted, order cancelled)."""
    try:
        await part_stock.release_for_line(db, line, note=note)
    except part_stock.PartStockError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e


@router.post("", response_model=ProjectResponse)
@router.post("/", response_model=ProjectResponse)
async def create_project(
    data: ProjectCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.PROJECTS_CREATE),
):
    await _check_customer(db, data.customer_id)
    for line in data.lines:
        await _check_product(db, line.product_id)
    project = Project(**data.model_dump(exclude={"lines"}))
    # Appended BEFORE the flush: on a pending row the collection is created
    # empty without a query, and the cascade fills in ``project_id``. Touching
    # it after the flush would be a lazy load, which async SQLAlchemy refuses.
    # ``from_stock_units`` is dropped from the dump because it is NOT a column
    # (pass 8, Decision 4): it becomes ledger movements below, once the rows
    # have ids to name.
    wanted: list[tuple[ProjectLine, int]] = []
    for i, line in enumerate(data.lines):
        row = ProjectLine(sort_order=i, **line.model_dump(exclude={"from_stock_units"}))
        project.lines.append(row)
        wanted.append((row, line.from_stock_units))
    db.add(project)
    await db.flush()
    # An order created WITH its lines reserves exactly as a line added later
    # does — otherwise the same dialog would silently mean nothing on the one
    # path that creates most lines.
    for row, units in wanted:
        if units:
            await _reserve(db, row, units, current_user)
    return await _response(db, project.id)


@router.get("/{project_id}", response_model=ProjectResponse)
async def get_project(
    project_id: int, db: AsyncSession = Depends(get_db), _: User | None = RequirePermission(Permission.PROJECTS_READ)
):
    return await _response(db, project_id)


@router.patch("/{project_id}", response_model=ProjectResponse)
async def update_project(
    project_id: int,
    data: ProjectUpdate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    project = await _get_project(db, project_id)
    lines = list(project.lines)
    if data.status is not None and data.status not in PROJECT_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid status")
    if data.priority is not None and data.priority not in PROJECT_PRIORITIES:
        raise HTTPException(status_code=400, detail="Invalid priority")
    if "customer_id" in data.model_fields_set:
        await _check_customer(db, data.customer_id)
    # Every field keys off model_fields_set: an explicit null CLEARS, an absent
    # field leaves the column alone (the tags/due_date/#2536 lesson, applied to all).
    for field_name in data.model_fields_set:
        setattr(project, field_name, getattr(data, field_name))
    if data.status == "cancelled":
        # Cancelling gives the shelf its kits back (pass 8, Decision 4) — the
        # order will never consume them. COMPLETING deliberately does not: the
        # stock was consumed, and the movements stand as the record of it.
        # Cancelling an already-cancelled order releases nothing a second time,
        # because the release reads what the ledger still holds and finds zero.
        #
        # ⚠️ Ruling 18: REACTIVATING a cancelled order does NOT restore its
        # reservations, and deliberately so. The kits went back on the shelf
        # and another order may have taken them since; silently taking them
        # again would be this route deciding, minutes or months later, that
        # this order still outranks whoever is holding them now. The operator
        # re-enters the number in the line dialog, which asks the shelf afresh.
        for line in lines:
            await _release(db, line, part_stock.NOTE_ORDER_CANCELLED)
    return await _response(db, project.id)


@router.delete("/{project_id}")
async def delete_project(
    project_id: int, db: AsyncSession = Depends(get_db), _: User | None = RequirePermission(Permission.PROJECTS_DELETE)
):
    """Archives and queue rows survive, unlinked (SET NULL done explicitly — SQLite enforces nothing)."""
    project = await _get_project(db, project_id)
    # Its lines go with it, so they go through the same two steps a single
    # deleted line does (Ruling 10): the kits come back to the shelf, and the
    # history that named the line stops naming an id that will be reused.
    for line in list(project.lines):
        await _release(db, line, part_stock.NOTE_PROJECT_DELETED)
        await part_stock.detach_line(db, line.id)
    for model in (PrintArchive, PrintQueueItem, AutoQueueItem):
        await db.execute(
            update(model).where(model.project_id == project_id).values(project_id=None, project_line_id=None)
        )
    await db.delete(project)
    return {"message": "Project deleted"}


# ---------- lines ----------


@router.post("/{project_id}/lines", response_model=ProjectResponse)
async def add_line(
    project_id: int,
    data: ProjectLineCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    project = await _get_project(db, project_id)
    await _check_product(db, data.product_id)
    line = ProjectLine(
        sort_order=max((ln.sort_order for ln in project.lines), default=-1) + 1,
        **data.model_dump(exclude={"from_stock_units"}),
    )
    project.lines.append(line)
    if data.from_stock_units:
        # Flushed first so the movements have a line id to name.
        await db.flush()
        await _reserve(db, line, data.from_stock_units, current_user)
    return await _response(db, project.id)


async def _get_line(db: AsyncSession, project_id: int, line_id: int) -> ProjectLine:
    line = await db.get(ProjectLine, line_id)
    if line is None or line.project_id != project_id:
        raise HTTPException(status_code=404, detail="Order line not found")
    return line


@router.patch("/{project_id}/lines/{line_id}", response_model=ProjectResponse)
async def update_line(
    project_id: int,
    line_id: int,
    data: ProjectLineUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    line = await _get_line(db, project_id, line_id)
    for field_name in data.model_fields_set - {"from_stock_units"}:
        setattr(line, field_name, getattr(data, field_name))
    if data.from_stock_units is not None:
        # Rewritten, never adjusted by a difference: ``reserve_for_line``
        # releases what this line holds and takes the new number off the shelf
        # again, in this same transaction. Editing 3 → 3 must therefore still
        # end at 3, which is why the release comes first — the product's
        # balance already has this line's own kits subtracted from it.
        await _reserve(db, line, data.from_stock_units, current_user)
    elif "quantity" in data.model_fields_set and await part_stock.reserved_units_for_line(db, line) > line.quantity:
        # Ruling 16: the quantity came down past what the line is holding, and
        # the dialog said nothing about the stock. Re-reserving AT the new
        # quantity releases exactly the difference — kits a line cannot use are
        # withheld from every other order for nothing. Only ever downwards: a
        # quantity going UP does not help itself to more of the shelf, because
        # nobody asked it to.
        await _reserve(db, line, line.quantity, current_user)
    return await _response(db, project_id)


@router.delete("/{project_id}/lines/{line_id}", response_model=ProjectResponse)
async def delete_line(
    project_id: int,
    line_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    line = await _get_line(db, project_id, line_id)
    # Release BEFORE detaching, or the reservation becomes invisible to the
    # query that hands it back and the kits stay off the shelf for good.
    await _release(db, line, part_stock.NOTE_LINE_DELETED)
    # The prints stay, and stay in the order: only the line they were filed
    # under goes. Done explicitly because SQLite enforces nothing — this
    # codebase never sets ``PRAGMA foreign_keys = ON``, so the ON DELETE SET
    # NULL these three FKs declare is honoured by PostgreSQL alone. The stock
    # ledger is the fourth table with that FK, and its history survives the
    # line: the parts are on the shelf whatever happened to the paperwork.
    for model in (PrintArchive, PrintQueueItem, AutoQueueItem):
        await db.execute(update(model).where(model.project_line_id == line_id).values(project_line_id=None))
    await part_stock.detach_line(db, line_id)
    project = await _get_project(db, project_id)
    project.lines.remove(line)  # delete-orphan turns this into the DELETE
    return await _response(db, project_id)


# ---------- free stock ----------


@router.post("/{project_id}/bank-surplus", response_model=BankSurplusResponse)
async def bank_surplus(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    """Move this order's overprint onto the product's shelf (pass 8, Decision 2).

    Never automatic, and that is the whole point of the button: a surplus is
    sometimes shipped with the order and sometimes scrapped, and only the
    operator knows which. Pressing it a second time moves only what has
    appeared since — ``surplus`` as ``order_metrics`` computes it (``usable −
    need`` per counted part, defective already excluded by ``row_quantity``)
    minus what this line has already banked. So the ledger holds the line's
    surplus once however many times the button is pressed, and a later print
    that grows the surplus is still bankable.

    A CANCELLED order banks too: the parts came off a bed regardless of what
    happened to the order afterwards, and they are exactly the ones most worth
    keeping.
    """
    ctx = await load_order_context(db, project_id)
    if ctx is None:
        raise HTTPException(status_code=404, detail="Project not found")
    figures, _other = attribute(ctx)

    banked = {
        (line_id, part_id): int(total or 0)
        for line_id, part_id, total in (
            await db.execute(
                select(
                    ProductPartStockMovement.project_line_id,
                    ProductPartStockMovement.product_part_id,
                    func.coalesce(func.sum(ProductPartStockMovement.delta), 0),
                )
                .where(
                    ProductPartStockMovement.project_line_id.in_([line.id for line in ctx.lines]),
                    ProductPartStockMovement.reason == "surplus_banked",
                )
                .group_by(ProductPartStockMovement.project_line_id, ProductPartStockMovement.product_part_id)
            )
        ).all()
    }

    # Per part, not per line: two lines of the same product feed one shelf, and
    # the toast says what landed on it.
    moved: dict[int, StockMovedOut] = {}
    for line in ctx.lines:
        for pf in figures[line.id].parts:
            delta = pf.surplus - banked.get((line.id, pf.part_id), 0)
            if delta <= 0:
                continue
            try:
                movement = await part_stock.move(
                    db,
                    part_id=pf.part_id,
                    delta=delta,
                    reason="surplus_banked",
                    project_line_id=line.id,
                    created_by=current_user.id if current_user else None,
                )
            except part_stock.PartStockError as e:
                raise HTTPException(status_code=409, detail=str(e)) from e
            if movement is None:
                continue
            # ``movement.delta``, not the ``delta`` asked for: the operator is
            # told what the LEDGER wrote. The two agree for a surplus today,
            # and the day they stop agreeing (a clamp, a rule added to ``move``)
            # the toast must follow the shelf, not the request.
            entry = moved.get(pf.part_id)
            if entry is None:
                moved[pf.part_id] = StockMovedOut(part_id=pf.part_id, name=pf.name, delta=movement.delta)
            else:
                entry.delta += movement.delta
    await db.commit()
    return BankSurplusResponse(moved=list(moved.values()), nothing_to_bank=not moved)


# ---------- procurement ----------


@router.patch("/{project_id}/procurement/{part_id}", response_model=ProjectResponse)
async def update_procurement(
    project_id: int,
    part_id: int,
    data: ProcurementUpdate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    project = await _get_project(db, project_id)
    part = await db.get(ProductPart, part_id)
    if part is None or part.kind != "purchased" or part.product_id not in {ln.product_id for ln in project.lines}:
        raise HTTPException(status_code=404, detail="Purchased part not found in this order")
    row = await db.get(ProjectProcurement, {"project_id": project_id, "product_part_id": part_id})
    if row is None:
        db.add(
            ProjectProcurement(project_id=project_id, product_part_id=part_id, quantity_acquired=data.quantity_acquired)
        )
    else:
        row.quantity_acquired = data.quantity_acquired
    return await _response(db, project_id)


# ---------- archives & queue ----------


@router.get("/{project_id}/archives")
async def list_project_archives(
    project_id: int,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_READ),
):
    """List archives in a project.

    ``limit`` is bounded at 500 — what the order page walks in — because an
    unbounded one is a whole farm's print history in a single response for the
    price of a query param.
    """
    # Verify project exists
    result = await db.execute(select(Project).where(Project.id == project_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Project not found")

    # Eager-load the relationships that archive_to_response touches —
    # created_by.username otherwise triggers a lazy load against an
    # already-returned async session → MissingGreenlet crash.
    query = (
        select(PrintArchive)
        .options(selectinload(PrintArchive.project), selectinload(PrintArchive.created_by))
        # ⚠️ ``deleted_at`` is the same filter every other order-archive reader
        # applies (``order_metrics``' two loaders, the figures behind them).
        # Without it a trashed print came back into the order page's walk, was
        # matched against no line — nothing attributes a deleted archive — and
        # so surfaced under "Unlisted": a print the operator had thrown away,
        # listed as work of theirs nobody had filed.
        .where(PrintArchive.project_id == project_id, PrintArchive.deleted_at.is_(None))
        # ⚠️ ``id`` is the TIEBREAKER, not decoration: ``created_at`` has second
        # resolution on SQLite, so two prints of the same second are a tie the
        # database may break differently for each LIMIT/OFFSET page — which
        # drops one of them from the order page's walk and repeats another.
        .order_by(PrintArchive.created_at.desc(), PrintArchive.id.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await db.execute(query)
    archives = result.scalars().all()

    # Import the response converter from archives module
    from backend.app.api.routes.archives import archive_to_response

    return [archive_to_response(a) for a in archives]


@router.post("/{project_id}/add-archives")
async def add_archives_to_project(
    project_id: int,
    data: BatchAddArchives,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    """File existing prints under this order, optionally under one of its lines."""
    await _get_project(db, project_id)  # 404s an order that is not there
    if data.project_line_id is not None:
        await _get_line(db, project_id, data.project_line_id)
    updated = 0
    for archive_id in data.archive_ids:
        archive = await db.get(PrintArchive, archive_id)
        if archive:
            # Same rule as the archive editor's project change (pass 8,
            # Decision 3): a print that was free stock stops being free stock
            # the moment an order counts it. Read before the assignment, which
            # is where ``project_id`` stops being what it was.
            was_unfiled = archive.project_id is None
            archive.project_id = project_id
            archive.project_line_id = data.project_line_id
            if was_unfiled:
                try:
                    await part_stock.reverse_unfiled_print(db, archive, note=part_stock.NOTE_FILED_UNDER_ORDER)
                except part_stock.PartStockError as e:
                    # The stock has already gone out to someone. Filing the
                    # print is still right — the ledger keeps the truth and the
                    # operator corrects it by hand from the product page.
                    logger.warning(
                        "Archive %s filed under order %s but its free-stock credit could not be reversed: %s",
                        archive.id,
                        project_id,
                        e,
                    )
            updated += 1
    return {"message": f"Added {updated} archives to project"}


@router.post("/{project_id}/remove-archives")
async def remove_archives_from_project(
    project_id: int,
    data: BatchAddArchives,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    """Unfile prints from this order — the line goes with the order, never alone."""
    updated = 0
    for archive_id in data.archive_ids:
        archive = (
            await db.execute(
                select(PrintArchive).where(PrintArchive.id == archive_id, PrintArchive.project_id == project_id)
            )
        ).scalar_one_or_none()
        if archive:
            archive.project_id = None
            archive.project_line_id = None
            # Out of the order is back onto the shelf (pass 8, Ruling 11): once
            # the order stops counting these parts, nothing does. Safe
            # unconditionally — ``credit_unfiled_print`` checks the status, the
            # (now NULL) project and the archive's own net, so a print that was
            # never free stock or is still holding some writes nothing.
            await part_stock.credit_unfiled_print(
                db,
                archive,
                created_by=current_user.id if current_user else None,
                note=part_stock.NOTE_UNFILED_FROM_ORDER,
            )
            updated += 1
    return {"message": f"Removed {updated} archives from project"}


@router.post("/{project_id}/add-queue")
async def add_queue_items_to_project(
    project_id: int,
    data: BatchAddQueueItems,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    """Batch add queue items to a project.

    A line only ever travels with the order it belongs to — the same rule
    ``add-archives``, ``remove-archives`` and ``archives.update_archive``
    follow. Re-filing an item under another order therefore drops the line it
    carries: keeping it would credit this order's work to a line of the old one.
    """
    # Verify project exists
    result = await db.execute(select(Project).where(Project.id == project_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Project not found")

    # Update queue items
    updated = 0
    for item_id in data.queue_item_ids:
        result = await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))
        item = result.scalar_one_or_none()
        if item:
            if item.project_line_id is not None:
                stale = await db.get(ProjectLine, item.project_line_id)
                if stale is None or stale.project_id != project_id:
                    item.project_line_id = None
            item.project_id = project_id
            updated += 1

    return {"message": f"Added {updated} queue items to project"}


# ---------- attachments ----------


def get_project_attachments_dir(project_id: int) -> Path:
    """Get the attachments directory for a project."""
    base_dir = Path(settings.archive_dir)
    return base_dir / "projects" / str(project_id) / "attachments"


@router.post("/{project_id}/attachments")
async def upload_attachment(
    project_id: int,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    """Upload an attachment to a project."""
    logger.info("=== UPLOAD START: %s for project %s ===", file.filename, project_id)

    # Verify project exists
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Validate file extension
    original_name = file.filename or "unknown"
    ext = os.path.splitext(original_name)[1].lower()
    if ext not in ALLOWED_ATTACHMENT_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"File type '{ext}' not supported. Allowed: images, PDFs, documents, STL, 3MF, archives.",
        )

    # Create attachments directory
    attachments_dir = get_project_attachments_dir(project_id)
    attachments_dir.mkdir(parents=True, exist_ok=True)

    # Generate unique filename
    unique_filename = f"{uuid.uuid4().hex}{ext}"
    file_path = (
        attachments_dir / unique_filename
    )  # SEC-PATH-OK: unique_filename = uuid4().hex + an extension validated against the attachment allowlist just above

    # Save file
    try:
        with open(file_path, "wb") as f:
            content = await file.read()
            f.write(content)
        logger.info("=== FILE SAVED: %s, size: %s ===", file_path, len(content))
    except Exception as e:
        logger.error("Failed to save attachment: %s", e)
        raise HTTPException(status_code=500, detail="Failed to save attachment")

    # Update project attachments JSON
    attachments = list(project.attachments or [])
    new_attachment = {
        "filename": unique_filename,
        "original_name": original_name,
        "size": len(content),
        "uploaded_at": datetime.now().isoformat(),
    }
    attachments.append(new_attachment)

    # Simple ORM update
    project.attachments = attachments
    db.add(project)  # Explicitly add to session

    logger.info("=== BEFORE COMMIT: %s attachments ===", len(attachments))

    await db.flush()
    await db.commit()

    logger.info("=== AFTER COMMIT ===")

    # Verify by re-querying
    result = await db.execute(select(Project).where(Project.id == project_id))
    fresh_project = result.scalar_one()

    logger.info("=== VERIFIED: %s attachments ===", len(fresh_project.attachments or []))

    return {
        "status": "success",
        "filename": unique_filename,
        "original_name": original_name,
        "attachments": fresh_project.attachments,
    }


@router.get("/{project_id}/attachments/{filename}")
async def download_attachment(
    project_id: int,
    filename: str,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_READ),
):
    """Download an attachment from a project."""
    # Validate filename to prevent path traversal
    if "/" in filename or "\\" in filename or ".." in filename or not filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    # Verify project exists
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Verify attachment exists in project
    attachments = project.attachments or []
    attachment = next((a for a in attachments if a.get("filename") == filename), None)
    if not attachment:
        raise HTTPException(status_code=404, detail="Attachment not found")

    # Check file exists
    file_path = (
        get_project_attachments_dir(project_id) / filename
    )  # SEC-PATH-OK: filename is rejected for / \ .. and empty just above the join
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Attachment file not found")

    return FileResponse(
        file_path,
        filename=attachment.get("original_name", filename),
        media_type="application/octet-stream",
    )


@router.delete("/{project_id}/attachments/{filename}")
async def delete_attachment(
    project_id: int,
    filename: str,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    """Delete an attachment from a project."""
    # Validate filename to prevent path traversal
    if "/" in filename or "\\" in filename or ".." in filename or not filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    # Verify project exists
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Find and remove attachment from list
    attachments = project.attachments or []
    attachment = next((a for a in attachments if a.get("filename") == filename), None)
    if not attachment:
        raise HTTPException(status_code=404, detail="Attachment not found")

    # Remove from list
    attachments = [a for a in attachments if a.get("filename") != filename]
    project.attachments = attachments if attachments else None

    # Delete file
    file_path = (
        get_project_attachments_dir(project_id) / filename
    )  # SEC-PATH-OK: filename is rejected for / \ .. and empty just above the join
    if file_path.exists():
        try:
            os.remove(file_path)
        except Exception as e:
            logger.warning("Failed to delete attachment file: %s", e)

    await db.flush()
    await db.refresh(project)

    return {
        "status": "success",
        "message": "Attachment deleted",
        "attachments": project.attachments,
    }


# ============ B.2 (#1155) — Project cover image ============


@router.post("/{project_id}/cover-image")
async def upload_project_cover_image(
    project_id: int,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    """Upload (or replace) the project's cover image (#1155).

    Stored alongside other attachments but tracked via
    ``Project.cover_image_filename`` so swap/delete operations don't
    touch the attachments list. Replaces any existing cover image — the
    prior file is deleted on disk before the new one lands so a stuck
    filesystem reference can't accumulate orphaned images.
    """
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    original_name = file.filename or "cover"
    ext = os.path.splitext(original_name)[1].lower()
    if ext not in COVER_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Cover image must be one of {sorted(COVER_EXTENSIONS)}",
        )

    attachments_dir = get_project_attachments_dir(project_id)
    attachments_dir.mkdir(parents=True, exist_ok=True)

    # Remove the previous cover-image file from disk first so we don't
    # accumulate orphans when users repeatedly replace it. Best-effort:
    # a missing/locked file shouldn't block a successful replacement.
    if project.cover_image_filename:
        old_path = attachments_dir / project.cover_image_filename
        if old_path.exists():
            try:
                os.remove(old_path)
            except OSError as e:
                logger.warning("Failed to delete old cover image %s: %s", old_path, e)

    unique_filename = f"cover_{uuid.uuid4().hex}{ext}"
    file_path = (
        attachments_dir / unique_filename
    )  # SEC-PATH-OK: unique_filename = 'cover_' + uuid4().hex + an extension validated against the cover-image allowlist just above
    try:
        with open(file_path, "wb") as f:
            content = await file.read()
            f.write(content)
    except OSError as e:
        logger.error("Failed to save cover image: %s", e)
        raise HTTPException(status_code=500, detail="Failed to save cover image") from e

    project.cover_image_filename = unique_filename
    db.add(project)
    await db.flush()

    return {
        "status": "success",
        "filename": unique_filename,
        "size": len(content),
    }


@router.get("/{project_id}/cover-image")
async def get_project_cover_image(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    _=RequireCameraStreamToken,
):
    """Stream the project's cover image (#1155).

    Browsers can't attach ``Authorization: Bearer ...`` to ``<img src>``
    requests, so this route accepts the same ``?token=`` stream
    credential as ``/archives/{id}/thumbnail``. The frontend wraps URLs
    via ``withStreamToken``.
    """
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not project.cover_image_filename:
        raise HTTPException(status_code=404, detail="No cover image set")

    file_path = get_project_attachments_dir(project_id) / project.cover_image_filename
    if not file_path.exists():
        # DB references a file that vanished from disk — clear the dangling
        # reference so future GETs get a clean 404 instead of repeatedly
        # touching the filesystem. ⚠️ RETURN the 404, never raise it: ``get_db``
        # rolls the request back on anything that escapes the handler, so a
        # raise would undo the very heal just performed and the next request
        # would find the same dangling name. (The product cover route's twin
        # learned this first.)
        logger.warning("Cover image file missing for project %s: %s", project_id, file_path)
        project.cover_image_filename = None
        await db.flush()
        return JSONResponse(status_code=404, content={"detail": "Cover image file not found"})

    ext = os.path.splitext(project.cover_image_filename)[1].lower()
    media_type = IMAGE_CONTENT_TYPES.get(ext, "application/octet-stream")
    # ⚠️ ``no-cache`` — REVALIDATE, not "do not store". This URL is stable
    # across the cover being replaced, so an age-based cache shows the old
    # picture after an upload; ``private`` alone still let a browser reuse a
    # heuristically fresh copy, which is what a cache-busting query param on
    # the frontend was working around. ``private``: token-gated user data,
    # never a shared cache's.
    return FileResponse(file_path, media_type=media_type, headers={"Cache-Control": "private, no-cache"})


@router.delete("/{project_id}/cover-image")
async def delete_project_cover_image(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    """Remove the project's cover image (#1155)."""
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    if project.cover_image_filename:
        file_path = get_project_attachments_dir(project_id) / project.cover_image_filename
        if file_path.exists():
            try:
                os.remove(file_path)
            except OSError as e:
                logger.warning("Failed to delete cover image file %s: %s", file_path, e)
        project.cover_image_filename = None
        db.add(project)
        await db.flush()

    return {"status": "success"}


# ============ Timeline ============


# An archive exists for every physical print — queue-driven, auto-queued, direct
# and printer-started alike (see the archive-is-print-history invariant), and the
# dispatcher stamps ``project_id`` onto it on every path. So the archive is the
# timeline's source for anything that reached a printer, and the two queue tables
# are asked only about work that has NOT reached one yet.
#
# The previous version took "print started" from ``PrintQueueItem`` instead, and
# turned archives into events only for ``completed`` / ``failed``. A print
# dispatched straight to a printer therefore appeared nowhere at all — no queue
# row to read, and a ``printing`` archive it ignored — while cancelled prints
# were invisible in every case.
_ARCHIVE_EVENT_BY_STATUS = {
    "printing": "print_started",
    "completed": "print_completed",
    "failed": "print_failed",
    "aborted": "print_cancelled",
    "cancelled": "print_cancelled",
    "stopped": "print_cancelled",
}

# English, and deliberately so: ``title`` is the API's own wording for callers
# that are not our frontend, which renders each event from ``event_type`` in the
# user's language and never shows these.
_EVENT_TITLES = {
    "print_started": "Print started",
    "print_completed": "Print completed",
    "print_failed": "Print failed",
    "print_cancelled": "Print cancelled",
    "queued": "Added to queue",
    "auto_queued": "Added to auto-queue",
    "project_created": "Project created",
}


def _archive_event_timestamp(archive: PrintArchive) -> datetime:
    """When the event being described actually happened.

    A finished print is placed at its end, a running one at its start. Falls back
    to ``created_at``, which every row has.
    """
    if _ARCHIVE_EVENT_BY_STATUS.get(archive.status) == "print_started":
        return archive.started_at or archive.created_at
    return archive.completed_at or archive.started_at or archive.created_at


def _queue_display_name(item) -> str:
    """Best-effort name for a queue row, which has no name of its own.

    Works for both queue tables: each references an archive and/or a library
    file, and neither carries a ``print_name`` column — reading one off the row
    itself is what used to 500 this endpoint.
    """
    return (
        (item.archive.print_name if item.archive else None)
        or (item.archive.filename if item.archive else None)
        or (item.library_file.filename if item.library_file else None)
        or "(unnamed queue item)"
    )


@router.get("/{project_id}/timeline", response_model=list[TimelineEvent])
async def get_project_timeline(
    project_id: int,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_READ),
):
    """Everything that happened to a project, newest first.

    Prints come from archives (running, finished, failed and cancelled alike);
    the two queue tables contribute only work still waiting, so nothing appears
    twice — a queue item that has been dispatched is no longer ``pending`` and
    its archive speaks for it from then on.

    Statuses are filtered **in SQL** rather than after the fetch. Filtering
    afterwards spent the limit on rows that were then discarded, so a project
    whose twenty most recent archives were all cancelled showed an empty
    timeline. Each source is ordered by the same value used as the event's
    timestamp, so taking the newest ``limit`` from each and cutting the merged
    list to ``limit`` yields exactly the newest ``limit`` overall.
    """
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    events: list[TimelineEvent] = []

    # Prints, in every state that says something happened.
    archive_order = func.coalesce(PrintArchive.completed_at, PrintArchive.started_at, PrintArchive.created_at)
    archives = (
        (
            await db.execute(
                select(PrintArchive)
                .where(PrintArchive.project_id == project_id)
                .where(PrintArchive.deleted_at.is_(None))
                .where(PrintArchive.status.in_(list(_ARCHIVE_EVENT_BY_STATUS)))
                .order_by(archive_order.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    archive_ids = {archive.id for archive in archives}

    for archive in archives:
        metadata: dict = {"archive_id": archive.id, "status": archive.status}
        if archive.print_time_seconds:
            metadata["print_time_hours"] = round(archive.print_time_seconds / 3600, 2)
        if archive.filament_used_grams:
            metadata["filament_grams"] = round(archive.filament_used_grams, 1)
        if archive.failure_reason:
            metadata["failure_reason"] = archive.failure_reason
        events.append(
            TimelineEvent(
                event_type=_ARCHIVE_EVENT_BY_STATUS[archive.status],
                timestamp=_archive_event_timestamp(archive),
                title=_EVENT_TITLES[_ARCHIVE_EVENT_BY_STATUS[archive.status]],
                description=archive.print_name or archive.filename,
                metadata=metadata,
            )
        )

    # Per-printer queue: only what is still waiting. A dispatched item has left
    # 'pending', and ``archive_id`` guards the overlap the status cannot — a
    # pending row that already points at one of the archives above (a reprint
    # queued from it) would otherwise be listed twice, once as work and once as
    # the print it produced.
    queued_items = (
        (
            await db.execute(
                select(PrintQueueItem)
                .options(selectinload(PrintQueueItem.archive), selectinload(PrintQueueItem.library_file))
                .where(PrintQueueItem.project_id == project_id)
                .where(PrintQueueItem.status == "pending")
                .order_by(PrintQueueItem.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    for item in queued_items:
        if item.archive_id and item.archive_id in archive_ids:
            continue
        events.append(
            TimelineEvent(
                event_type="queued",
                timestamp=item.created_at,
                title=_EVENT_TITLES["queued"],
                description=_queue_display_name(item),
                metadata={"queue_item_id": item.id},
            )
        )

    # Auto-queue: work not yet routed to any printer. Once routed the row turns
    # 'assigned' and a per-printer item takes over, so the two tables cannot both
    # claim the same job; ``assigned_to_item_id`` is belt and braces for a row
    # routed between the two queries.
    auto_items = (
        (
            await db.execute(
                select(AutoQueueItem)
                .options(selectinload(AutoQueueItem.archive), selectinload(AutoQueueItem.library_file))
                .where(AutoQueueItem.project_id == project_id)
                .where(AutoQueueItem.status == "pending")
                .where(AutoQueueItem.assigned_to_item_id.is_(None))
                .order_by(AutoQueueItem.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    for item in auto_items:
        if item.archive_id and item.archive_id in archive_ids:
            continue
        events.append(
            TimelineEvent(
                event_type="auto_queued",
                timestamp=item.created_at,
                title=_EVENT_TITLES["auto_queued"],
                description=_queue_display_name(item),
                metadata={"auto_queue_item_id": item.id, "target_model": item.target_model},
            )
        )

    events.append(
        TimelineEvent(
            event_type="project_created",
            timestamp=project.created_at,
            title=_EVENT_TITLES["project_created"],
            description=project.name,
        )
    )

    events.sort(key=lambda e: e.timestamp, reverse=True)

    return events[:limit]


# ---------- duplicate ----------


def _duplicate_name(base: str, taken: set[str]) -> str:
    """``"X" -> "X (Copy)"``, then ``"X (Copy 2)"`` and so on.

    Project names carry no unique constraint, so this is politeness rather
    than correctness — three rows all called "Voron (Copy)" are legal and
    unusable.
    """
    candidate = f"{base} (Copy)"
    if candidate not in taken:
        return candidate
    n = 2
    while f"{base} (Copy {n})" in taken:
        n += 1
    return f"{base} (Copy {n})"


async def _copy_attachment_files(source_id: int, new_id: int) -> bool:
    """Copy ``projects/<id>/attachments`` across. True when the copy stands.

    ``attachments`` and ``cover_image_filename`` name files inside a
    per-project directory, so copying the columns alone would give the new
    project a file list and a cover that resolve to nothing — and would tie
    its images to the source's lifetime, where deleting the source takes them.
    """
    src = get_project_attachments_dir(source_id)
    if not src.is_dir():
        return True  # nothing to carry; the columns will be empty anyway
    try:
        await asyncio.to_thread(shutil.copytree, src, get_project_attachments_dir(new_id), dirs_exist_ok=True)
        return True
    except OSError as e:
        logger.warning("Project %s: attachments could not be copied from %s: %s", new_id, source_id, e)
        return False


@router.post("/{project_id}/duplicate", response_model=ProjectResponse)
async def duplicate_project(
    project_id: int,
    data: ProjectDuplicate = Body(default_factory=ProjectDuplicate),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_CREATE),
):
    """A reorder: lines, customer, notes, attachments come across; history never does; status is active."""
    source = await _get_project(db, project_id)
    taken = set((await db.execute(select(Project.name))).scalars().all())
    copy = Project(
        name=data.name or _duplicate_name(source.name, taken),
        customer_id=source.customer_id,
        description=source.description,
        color=source.color,
        status="active",
        notes=source.notes,
        tags=source.tags,
        due_date=source.due_date,
        priority=source.priority,
        price=source.price,
        url=source.url,
    )
    for line in source.lines:
        copy.lines.append(
            ProjectLine(
                product_id=line.product_id,
                quantity=line.quantity,
                material=line.material,
                color=line.color,
                note=line.note,
                sort_order=line.sort_order,
            )
        )
    db.add(copy)
    await db.flush()
    if source.attachments or source.cover_image_filename:
        if await _copy_attachment_files(source.id, copy.id):
            copy.attachments = source.attachments
            copy.cover_image_filename = source.cover_image_filename
    return await _response(db, copy.id)


# ---------- the print plan (spec pass 3) ----------


def _counts(mapping: dict[int, int], names: dict[int, str]) -> list[PlanPartCount]:
    """``part_id → count`` as the wire's named list, in part-id order.

    The engine speaks in bare ids because it is pure; a name never enters it.
    ``"?"`` for an id whose part vanished between the two reads — the same
    placeholder ``_response`` uses for a missing product.
    """
    return [PlanPartCount(part_id=pid, name=names.get(pid, "?"), count=n) for pid, n in sorted(mapping.items())]


def _plan_response(plan: OrderPlan) -> OrderPlanResponse:
    """Name every id the engine returned — no SELECT, no walk.

    The engine builds the plan from an ``OrderContext`` that already holds every
    product and part of the order, so it hands the two name maps out beside the
    rows. Re-reading ``products`` and ``product_parts`` here was two queries for
    rows the request had just had in memory.
    """
    part_names = plan.part_names
    product_names = plan.product_names
    return OrderPlanResponse(
        lines=[
            LinePlanOut(
                line_id=line.line_id,
                product_id=line.product_id,
                product_name=product_names.get(line.product_id, "?"),
                material=line.material,
                outstanding_before=_counts(line.outstanding_before, part_names),
                rows=[
                    PlanRowOut(
                        plate_id=row.plate_id,
                        library_file_id=row.library_file_id,
                        plate_index=row.plate_index,
                        filename=row.filename,
                        count=row.count,
                        useful=_counts(row.useful, part_names),
                        print_time_seconds=row.print_time_seconds,
                        filament_used_grams=row.filament_used_grams,
                        cost=row.cost,
                        time_unknown=row.time_unknown,
                        printer_model=row.printer_model,
                        # ⚠️ The totals below are the PICKED plate's, and stay
                        # so. Switching a row to one of these is the block's
                        # what-if (``planMath.projectPlan``), asked of a plan
                        # the server has already answered — recomputing it here
                        # would mean sending a plan per combination of choices
                        # nobody has made yet.
                        alternatives=[
                            PlanAlternativeOut(
                                plate_id=alt.plate_id,
                                library_file_id=alt.library_file_id,
                                plate_index=alt.plate_index,
                                filename=alt.filename,
                                printer_model=alt.printer_model,
                                print_time_seconds=alt.print_time_seconds,
                                filament_used_grams=alt.filament_used_grams,
                                cost=alt.cost,
                                time_unknown=alt.time_unknown,
                            )
                            for alt in row.alternatives
                        ],
                    )
                    for row in line.rows
                ],
                surplus_after=_counts(line.surplus_after, part_names),
                # The count on an unsatisfiable part is what is still MISSING —
                # its outstanding figure, which no candidate plate yields.
                unsatisfiable=_counts(
                    {pid: line.outstanding_before.get(pid, 0) for pid in line.unsatisfiable}, part_names
                ),
                candidates=line.candidates,
                not_sliced=line.not_sliced,
            )
            for line in plan.lines
        ],
        totals=PlanTotalsOut(
            prints=plan.totals.prints,
            print_time_seconds=plan.totals.print_time_seconds,
            filament_used_grams=plan.totals.filament_used_grams,
            cost=plan.totals.cost,
        ),
        truncated=plan.truncated,
    )


@router.get("/{project_id}/plan", response_model=OrderPlanResponse)
async def get_order_plan(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_READ),
):
    """What to print next for every line of this order (spec pass 3).

    Computed on every read, never cached and never stored: a second call after
    enqueuing sees the new queue rows and plans that much less. Reads nothing
    about any printer — this is a question about parts, not about machines.
    """
    plan = await plan_for_order(db, project_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return _plan_response(plan)


class _ResolvedPlate(NamedTuple):
    """One validated item of an enqueue request, as plain scalars.

    Read out of the loaded rows BEFORE the first writer commits — a commit may
    expire every instance above it — which is why nothing here is an ORM object.
    """

    line_id: int
    plate_id: int
    library_file_id: int
    #: The slicer's 1-based plate index, or ``None`` for "the whole file".
    plate_number: int | None
    count: int
    #: The source file already carries swap macros (``LibraryFile.swap_compatible``).
    baked_swap_macros: bool
    #: The printer model the 3MF was sliced for, in the spelling the auto-queue
    #: routes on. ``None`` when the file names none.
    sliced_for_model: str | None


@router.post("/{project_id}/plan/enqueue", response_model=PlanEnqueueResponse)
async def enqueue_order_plan(
    project_id: int,
    data: PlanEnqueueRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.PROJECTS_UPDATE, Permission.QUEUE_CREATE),
):
    """Send plan rows to the auto-queue, or to one printer's queue.

    Both queue doors (``POST /queue/``, ``POST /auto-queue/``) require
    ``queue:create``; this one also changes what an order has coming, so it
    asks for ``projects:update`` too — ``RequirePermission`` demands ALL of the
    permissions it is given.

    ⚠️ **Routing is not dispatching.** Naming a printer says WHERE the work is
    filed, not whether the machine can take it now. The only thing this endpoint
    may ask about a printer's READINESS is that it exists and is not archived;
    plate clear, drying, stagger, filament remain ``check_queue``'s question,
    asked again at dispatch. Nothing here reads live printer state or ranks
    anything. Its model and its swap-mode setting are read, but only to fill the
    row being written — never to decide whether to write it.

    ⚠️ **The options come from the operator's saved profile**, the same row the
    print dialog reads before it builds a payload (``preference_options``). This
    door has no dialog in front of it, and until 2026-09-04 it therefore wrote
    the writers' own defaults — a farm configured to run swap macros printed
    without them. The profile is looked up **per model**: the printer's when one
    is named, otherwise the model each plate's file was sliced for, which is the
    same model the auto-queue will route it by.

    ⚠️ **Order of application, when a request body eventually carries its own
    options block: profile, then body, then the mute.** The body is somebody's
    explicit answer and outranks a saved default; the mute is not a default at
    all but a statement about what the machine and the file can physically do,
    so nothing sent over the wire may talk it out of firing. Today the body
    carries no such block, and the code is already shaped for one.

    Each item is one call to the existing writer with ``quantity = count``, and
    **the writers commit per call**: ``add_items_to_auto_queue`` and
    ``enqueue_batch_copies`` each end in their own ``commit()``, so no
    transaction spans the items. That is why every item is validated before any
    of them is written — and why a failure part-way through leaves what was
    already created in place, and returns what that was.

    The target's shape is the schema's business (``PlanEnqueueTarget``): a
    printer kind without an id, or an auto kind with one, never reaches here.
    """
    lines_by_id = {line.id: line for line in (await _get_project(db, project_id)).lines}

    printer_id: int | None = None
    printer_model: str | None = None
    printer_swap_on = False
    if data.target.kind == "printer":
        printer = await db.get(Printer, data.target.printer_id)
        # Exists and is not archived. That is the whole of it — see the warning
        # above before adding a third condition here.
        if printer is None or printer.archived:
            raise HTTPException(status_code=404, detail="Printer not found")
        printer_id = printer.id
        printer_model = printer.model
        printer_swap_on = bool(printer.swap_mode_enabled)

    # ⚠️ ONE load for the whole request, before the loop — this used to fetch a
    # product AND build its recipes inside it, per distinct product of the
    # items, i.e. two round trips per line on the write path. A line naming a
    # product that is gone simply has no plates, which the loop below answers
    # with the same 404 the missing-plate case gets.
    for item in data.items:
        if item.line_id not in lines_by_id:
            raise HTTPException(status_code=404, detail="Order line not found in this project")
    wanted = {lines_by_id[item.line_id].product_id for item in data.items}
    if not wanted:
        # No item names a product, so there is nothing to look up and nothing to
        # write. ``items`` carries ``min_length=1``, which makes this a guard
        # rather than a branch the API can be talked into — but an empty set
        # here would render as ``IN ()``, a full-table read that can only match
        # nothing, and the guard costs one comparison.
        return PlanEnqueueResponse(created=[])
    products = (
        (
            await db.execute(
                select(Product)
                .options(selectinload(Product.parts), selectinload(Product.plates))
                .where(Product.id.in_(wanted))
            )
        )
        .scalars()
        .all()
    )
    recipes_by_product = await recipes_for_products(db, products)
    # ``swap_compatible`` rather than the file: the loaded row is wanted for one
    # boolean, and carrying it would mean importing a model this module has no
    # other use for.
    plates_by_product: dict[int, dict[int, tuple[ProductPlate, bool, PlateRecipe]]] = {
        product_id: {plate.id: (plate, bool(file.swap_compatible), recipe) for plate, file, recipe in rows}
        for product_id, rows in recipes_by_product.items()
    }
    # Plain scalars, read BEFORE the first writer commits, because a commit may
    # expire every instance loaded above it.
    resolved: list[_ResolvedPlate] = []
    for item in data.items:
        line = lines_by_id[item.line_id]
        plates = plates_by_product.get(line.product_id, {})
        entry = plates.get(item.plate_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="Plate not found in this line's product")
        plate, baked_swap_macros, recipe = entry
        if not recipe.sliced:
            raise HTTPException(status_code=404, detail="Plate is not sliced")
        resolved.append(
            _ResolvedPlate(
                line_id=line.id,
                plate_id=plate.id,
                library_file_id=plate.library_file_id,
                # ⚠️ ``plate_index = 0`` means the whole file, which on a queue
                # row is no plate at all — that column carries the slicer's
                # 1-based index.
                plate_number=plate.plate_index or None,
                count=item.count,
                baked_swap_macros=baked_swap_macros,
                sliced_for_model=recipe.printer_model,
            )
        )

    # What the print dialog would have sent, per model. ⚠️ Read BEFORE the write
    # loop for the same reason ``resolved`` is: the first writer's commit may
    # expire what it read.
    #
    # ⚠️ **The model is the PRINTER's when one is named, and the FILE's when one
    # is not.** The auto-queue picks the machine later, but it picks it by the
    # model the 3MF was sliced for (``AutoQueueItem.target_model``, derived from
    # the same metadata) — so that model is known here, and a request whose
    # plates were sliced for two machines legitimately reads two profiles. A
    # file that names no model falls back to the operator's most recent row.
    profiles = {
        model: await preference_options(db, current_user, model)
        for model in ({printer_model} if printer_id is not None else {p.sliced_for_model for p in resolved})
    }

    created: list[PlanEnqueueCreated] = []

    def _partial(message: str) -> HTTPException:
        """A 500 that still says what landed.

        Every writer above committed its own item, so nothing here can be rolled
        back — a bare 500 would leave the operator with queue rows nobody told
        them about. ``detail`` carries the ``PlanEnqueueResponse`` shape beside
        the message, so the same client code can read it.
        """
        return HTTPException(
            status_code=500,
            detail={"message": message, "created": [c.model_dump() for c in created]},
        )

    for plate in resolved:
        profile = profiles[printer_model if printer_id is not None else plate.sliced_for_model]
        if printer_id is None:
            options = profile.for_auto_queue() if profile else {}
        else:
            options = profile.for_printer_queue() if profile else {}
        # ⚠️ A request body carrying its own options block merges HERE — the
        # explicit answer wins over the saved profile — and the mute below then
        # applies to the RESULT. That order is the point: the gate is about what
        # the machine and the file can physically do, so nothing anybody sends
        # may talk it out of muting. Today the body carries no such block.
        #
        # The rule every other queue door applies (``services/queue_add.py`` and
        # the two print-now routes): swap macros are meaningful only on a printer
        # with swap mode ON and a source file that does not already carry them
        # baked in by third-party tooling — otherwise the plate change fires
        # twice. ⚠️ UNCONDITIONAL, not "only when a profile turned them on":
        # ``AutoQueueItemCreate`` defaults ``execute_swap_macros`` to True, so a
        # ``swap_compatible`` file with no preference behind it would double-fire
        # on the auto target through the writer's own default.
        if plate.baked_swap_macros or (printer_id is not None and not printer_swap_on):
            options["execute_swap_macros"] = False
            options["swap_macro_events"] = None
        try:
            if printer_id is None:
                rows = await add_items_to_auto_queue(
                    db,
                    AutoQueueItemCreate(
                        library_file_id=plate.library_file_id,
                        plate_id=plate.plate_number,
                        quantity=plate.count,
                        project_id=project_id,
                        project_line_id=plate.line_id,
                        **options,
                    ),
                    current_user,
                )
            else:
                rows, _batch_id = await enqueue_batch_copies(
                    db,
                    printer_id=printer_id,
                    count=plate.count,
                    library_file_id=plate.library_file_id,
                    plate_id=plate.plate_number,
                    project_id=project_id,
                    project_line_id=plate.line_id,
                    created_by_id=current_user.id if current_user else None,
                    **options,
                )
        except Exception as exc:
            # ⚠️ ANY failure past the first committed item, not just a tidy one.
            # With nothing written yet there is nothing to report, so the
            # original error travels on untouched — that is what the first item
            # failing has always done.
            if not created:
                raise
            logger.exception("Plan enqueue for project %s failed after %s item(s)", project_id, len(created))
            raise _partial(str(exc) or exc.__class__.__name__) from exc
        if printer_id is not None and not rows:
            # The printer has no queue row at all — a broken install, not a
            # readiness verdict. Earlier items are already committed, so say
            # what landed instead of reporting an empty success.
            raise _partial("That printer has no queue")
        created.append(
            PlanEnqueueCreated(line_id=plate.line_id, plate_id=plate.plate_id, queue_item_ids=[r.id for r in rows])
        )
    return PlanEnqueueResponse(created=created)
