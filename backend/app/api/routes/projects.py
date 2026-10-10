"""Orders (projects): lines of products for a customer, figures from the archive.

Spec: docs/superpowers/specs/2026-09-02-projects-redesign-design.md.
Route handlers never commit — the get_db dependency does.
"""

import asyncio
import logging
import os
import shutil
import uuid
from collections.abc import Callable, Sequence
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import case, func, or_, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

# Aliased: a private name imported into a module this size could be shadowed
# by a local helper of the same name without anyone noticing.
from backend.app.api.routes._workshop_rights import (
    bind_workshop_credentials,
    ensure,
    ensure_coded,
    ensure_may_file,
    workshop_view,
)
from backend.app.api.routes.auto_queue import _to_response as auto_queue_row_response, auto_queue_item_load_options
from backend.app.api.routes.library import file_name_visible
from backend.app.api.routes.print_queue import _enrich_response as queue_row_response, queue_item_load_options
from backend.app.core.api_key_scope import key_printer_scope
from backend.app.core.auth import (
    RequestCredentials,
    RequireAnyPermission,
    RequirePermission,
    acting_user,
    library_name_scope,
    request_credentials,
    require_media_permission,
    require_ownership_permission,
    require_permission,
)
from backend.app.core.config import settings
from backend.app.core.database import LOCK_NOT_AVAILABLE, get_db, sqlstate
from backend.app.core.permissions import Permission
from backend.app.i18n.api_errors import json_error
from backend.app.models.archive import PrintArchive
from backend.app.models.archive_part import PrintArchivePart
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.customer import Customer, CustomerContact
from backend.app.models.finished_stock import StockItem
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.product import Product, ProductOrigin, ProductPart, ProductPlate
from backend.app.models.project import Project, ProjectEvent
from backend.app.models.project_line import ProjectLine, ProjectProcurement
from backend.app.models.stock_issue import StockIssue
from backend.app.models.user import User
from backend.app.schemas.archive import ArchivePartRow
from backend.app.schemas.auto_queue import AutoQueueItemCreate
from backend.app.schemas.farm_forecast import (
    EstimateReasonOut,
    FarmForecastOut,
    ForecastBatchOut,
    LineForecastOut,
    ModelHoursOut,
    OrderForecastDetailOut,
    OrderForecastOut,
    RowForecastOut,
)
from backend.app.schemas.filament_needs import FarmNeedsOut, FarmRowOut, NeedRowOut, OrderNeedsOut
from backend.app.schemas.listing import (
    AttentionOrder,
    DeadlineOrder,
    EtaMark,
    OrderBoard,
    OrderBoardColumn,
    OrderDeadlines,
    OrderListPage,
    OrderListTotals,
    OrdersSummary,
    OrderStageCounts,
    ProjectsNavBadges,
)
from backend.app.schemas.order_from_files import OrderFromFilesRequest
from backend.app.schemas.order_queue import OrderQueueOut, OrderQueuePrinting
from backend.app.schemas.project import (
    PROJECT_PRIORITIES,
    PROJECT_STAGES,
    PROJECT_STATUSES,
    BankSurplusResponse,
    BatchAddArchives,
    BatchAddQueueItems,
    BatchLinesIn,
    BatchLinesOut,
    BatchPartsLineIn,
    BatchProductLineIn,
    BatchStockIn,
    DroppedPartOut,
    FulfilmentIn,
    FulfilmentOut,
    FulfilmentStateOut,
    LineConfigurationImpact,
    LineConfigurationIn,
    LineConfigurationOut,
    LineIntakeOut,
    LinePlanOut,
    LineProductOut,
    LinePurchasedPartOut,
    LineStateOut,
    OrderAssigneeOut,
    OrderContactOut,
    OrderPlanResponse,
    OrderPrintDefectsIn,
    OrderPrintDefectsOut,
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
    ProductLineRef,
    ProjectCountsOut,
    ProjectCreate,
    ProjectDuplicate,
    ProjectFiguresOut,
    ProjectLineCreate,
    ProjectLineResponse,
    ProjectLineUpdate,
    ProjectListResponse,
    ProjectResponse,
    ProjectStageUpdate,
    ProjectUpdate,
    RebalanceOut,
    RecipientOut,
    StockMovedOut,
    StockOfferOut,
    StockPositionRefOut,
    TakeStockIn,
    TakeStockOut,
    TimelineEvent,
)
from backend.app.services import (
    archive_parts,
    farm_forecast,
    filament_needs,
    finished_stock,
    finished_stock_views,
    line_config,
    line_intake,
    order_filing,
    order_from_files,
    order_fulfilment,
    order_journal,
    part_images,
    part_stock,
    product_delete,
    product_gate as product_gate_module,
    queue_rebalance,
    stock_issues,
    stock_offers,
)
from backend.app.services.archive_defects import DefectsWrite, record_defects
from backend.app.services.archive_write_scope import archive_write_scope, lock_prints
from backend.app.services.auto_queue_add import add_items_to_auto_queue
from backend.app.services.configuration_views import configuration_out, groups_by_product
from backend.app.services.entity_codes import code_for, id_from_query
from backend.app.services.filament_intake import require_source_requirements
from backend.app.services.filament_requirements import PrintRequirementsCache
from backend.app.services.line_composition import LineConfig, default_options, load_line_configs
from backend.app.services.list_paging import (
    SortSpec,
    apply_sql_sort,
    page_meta,
    resolve_sort,
    slice_page,
    sort_computed,
)
from backend.app.services.order_deadlines import ATTENTION_ORDER, attention_reason, eta_is_late, server_wall_time
from backend.app.services.order_metrics import (
    attribute,
    composition_of,
    grouped_figures,
    load_order_context,
    procurement_figures,
    procurement_totals,
    project_figures,
)
from backend.app.services.order_queue import (
    RUNNING_STATUS,
    awaiting_auto_row_conditions,
    live_archive_conditions,
    queued_printer_row_conditions,
)
from backend.app.services.plan_engine import OrderPlan, counted_parts_by_line, plan_for_order, queued_yield_by_line
from backend.app.services.print_option_defaults import preference_options
from backend.app.services.product_composition import PlateRecipe, recipes_for_products
from backend.app.services.product_files import (
    ALLOWED_ATTACHMENT_EXTENSIONS,
    COVER_EXTENSIONS,
    IMAGE_CONTENT_TYPES,
    effective_cover,
)
from backend.app.services.product_gate import product_gate
from backend.app.services.queue_batch import enqueue_batch_copies

logger = logging.getLogger(__name__)

# ``Fф`` at a gate (WS-13 E13 O10): work this request creates under an order — either right files it.
_FILES_FUTURE = (Permission.ORDERS_UPDATE, Permission.ORDERS_FILE_PRINTS)

router = APIRouter(prefix="/projects", tags=["projects"], dependencies=[Depends(bind_workshop_credentials)])


# ---------- response building ----------


async def _get_project(db: AsyncSession, project_id: int, *, fresh: bool = False) -> Project:
    """``fresh`` — re-read behind a lock (WS-13 E1 BL2), not from the identity map."""
    statement = (
        select(Project)
        .options(selectinload(Project.lines), selectinload(Project.customer))
        .where(Project.id == project_id)
    )
    if fresh:
        statement = statement.execution_options(populate_existing=True)
    project = (await db.execute(statement)).scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _stage_out(project: Project) -> str | None:
    """The stage the operator sees (spec workshop-order-stage, rule 2): the
    column while active, «done» once completed, none once cancelled."""
    if project.status == "completed":
        return "done"
    if project.status == "cancelled":
        return None
    return project.stage


async def _check_responsible(db: AsyncSession, user_id: int | None) -> None:
    """Only an existing ACTIVE user may be made responsible (rule 9) — asked only when the value changes."""
    if user_id is None:
        return
    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=422, detail=f"User {user_id} not found or inactive")


async def _responsible_ref(db: AsyncSession, user_id: int | None) -> dict | None:
    """``{id, name}`` for the journal — a name snapshot, so the line survives a rename or a delete."""
    user = await db.get(User, user_id) if user_id else None
    return {"id": user.id, "name": user.username} if user else None


async def _configurations(db: AsyncSession, ctx) -> dict[int, LineConfigurationOut]:
    """Each line's configuration with names (spec workshop-product-variants, rule 20).

    One read of the order's groups; choices, standards and parts come off the
    context the figures were computed from, so the caption and the kit agree.
    """
    groups = await groups_by_product(db, {line.product_id for line in ctx.lines})
    return {
        line.id: configuration_out(
            groups.get(line.product_id, []),
            ctx.parts_by_product.get(line.product_id, []),
            ctx.config_by_line.get(line.id, LineConfig()),
            ctx.defaults_by_product.get(line.product_id, {}),
            line.mode,
        )
        for line in ctx.lines
    }


def _line_counters(line: ProjectLine, parts: dict) -> dict[str, int]:
    """The stock counters a line response carries (spec workshop-order-issue, rule 23) — a
    parts line sums its parts' (``project_line_part_stock``)."""
    if line.mode == "parts":
        rows = list(parts.values())
        return {
            "assembled": 0,
            "received": sum(row.received for row in rows),
            "issued": sum(row.issued for row in rows),
            "held": sum(part_stock.part_held(row) for row in rows),
            "written_off": sum(row.written_off for row in rows),
        }
    return {
        "assembled": line.assembled or 0,
        "received": line.received or 0,
        "issued": line.issued or 0,
        "held": finished_stock.held_units(line),
        "written_off": line.written_off or 0,
    }


def _line_product(product: Product | None) -> dict:
    """WS-13 E4 H01: what the line table shows of its product, off the loaded row.
    The cover is the EFFECTIVE one — the order lists' rule — never the column alone."""
    if product is None:
        return {"product_sku": None, "product_origin": "catalog", "product_has_cover": False}
    return {
        "product_sku": product.sku,
        "product_origin": product.origin,
        "product_has_cover": effective_cover(product) is not None,
    }


def _line_purchased(ctx, line: ProjectLine) -> list[LinePurchasedPartOut]:
    """WS-13 E4 H03: the line's purchased parts. ``need`` is per × the STORED
    quantity — ``procurement_figures``'s own expression, so the lines add up to the
    order's procurement row (a parts line's DTO quantity is its parts' sum, not 1)."""
    return [
        LinePurchasedPartOut(
            part_id=part.id,
            name=part.name,
            per=per,
            need=per * line.quantity,
            variant=part.variant_option_id is not None,
        )
        for part, per in composition_of(ctx, line)
        if part.kind == "purchased" and per > 0
    ]


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
    configurations = await _configurations(db, ctx)
    part_counters = await part_stock.line_part_stock(db, [line.id for line in ctx.lines if line.mode == "parts"])
    # WS-13 E4 H04: what each line already has waiting — ONE call of the plan's own
    # map over every line, recipes from one batch and the counted parts from the same
    # ``attribute`` pass, so the card and the plan subtract the same thing. The counted
    # set is the plan's own too (``per > 0``): a part the configuration dropped reads as
    # a ``per = 0`` row, and the plan does not subtract what a queued plate makes of it
    # (final review M1).
    queued = await queued_yield_by_line(
        db,
        await recipes_for_products(db, ctx.products_by_id.values()),
        ctx.lines,
        counted_parts_by_line({ctx.project.id: figs}),
    )
    variant_parts = {
        part.id for parts in ctx.parts_by_product.values() for part in parts if part.variant_option_id is not None
    }
    project = ctx.project
    customer = await db.get(Customer, project.customer_id) if project.customer_id else None
    contact = await db.get(CustomerContact, project.contact_id) if project.contact_id else None
    responsible = await db.get(User, project.responsible_id) if project.responsible_id else None
    # A contact's phone and email are the customer directory's (WS-13 E13 O12); the order
    # itself shows who receives it — the name and the role.
    sees_contacts = (await workshop_view()).customers
    lines = [
        ProjectLineResponse(
            id=line.id,
            product_id=line.product_id,
            product_name=ctx.products_by_id[line.product_id].name if line.product_id in ctx.products_by_id else "?",
            **_line_product(ctx.products_by_id.get(line.product_id)),
            # A parts line reads in parts (spec workshop-product-variants, rule 16).
            quantity=figs[line.id].quantity,
            material=line.material,
            color=line.color,
            note=line.note,
            sort_order=line.sort_order,
            units_printed=figs[line.id].units_printed,
            from_stock_units=figs[line.id].from_stock_units,
            from_finished=figs[line.id].from_finished,
            from_kit_units=figs[line.id].from_kit_units,
            **_line_counters(line, part_counters.get(line.id, {})),
            covered_units=figs[line.id].covered_units,
            progress=figs[line.id].progress,
            parts=[
                PartFiguresOut(
                    part_id=p.part_id,
                    name=p.name,
                    qty_per_unit=p.per,
                    need=p.need,
                    usable=p.usable,
                    in_progress=p.in_progress,
                    remaining=p.remaining,
                    surplus=p.surplus,
                    variant=p.part_id in variant_parts,
                    queued=queued.get(line.id, {}).get(p.part_id, 0),
                    bankable=p.bankable,
                )
                for p in figs[line.id].parts
            ],
            purchased=_line_purchased(ctx, line),
            archive_ids=list(figs[line.id].archive_ids),
            prints_in_progress=figs[line.id].prints_in_progress,
            prints_queued=figs[line.id].prints_queued,
            mode=line.mode,
            config_key=line.config_key,
            configuration=configurations[line.id],
        )
        for line in ctx.lines
    ]
    pf = project_figures(ctx, figs, other)
    procurement = procurement_figures(ctx)
    counters = [_line_counters(line, part_counters.get(line.id, {})) for line in ctx.lines]
    # CN1: the «Prints» tab lists ``/archives`` — the order's prints outside the trash,
    # whatever their status — and the badge counts exactly that.
    prints_count = await db.scalar(
        select(func.count())
        .select_from(PrintArchive)
        .where(PrintArchive.project_id == project.id, PrintArchive.deleted_at.is_(None))
    )
    issues_count = await db.scalar(
        select(func.count()).select_from(StockIssue).where(StockIssue.project_id == project.id)
    )
    return ProjectResponse(
        id=project.id,
        code=code_for("order", project.id),
        name=project.name,
        customer_id=project.customer_id,
        customer_name=customer.name if customer else None,
        contact_id=project.contact_id,
        contact=OrderContactOut(
            id=contact.id,
            code=code_for("contact", contact.id),
            name=contact.name,
            role=contact.role,
            phone=contact.phone if sees_contacts else None,
            email=contact.email if sees_contacts else None,
        )
        if contact
        else None,
        description=project.description,
        color=project.color,
        status=project.status,
        stage=_stage_out(project),
        responsible_id=project.responsible_id,
        responsible_name=responsible.username if responsible else None,
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
        procurement=[ProcurementOut(**p.__dict__) for p in procurement],
        figures=ProjectFiguresOut(
            **pf.__dict__,
            issued_units=sum(c["issued"] for c in counters),
            held_units=sum(c["held"] for c in counters),
            **procurement_totals(procurement, pf.total_cost, project.price),
        ),
        counts=ProjectCountsOut(prints=prints_count or 0, issues=issues_count or 0),
        other_archive_ids=[a.id for a in other],
    )


# ---------- CRUD ----------


# low < normal < high < urgent — a CASE so the column sorts by rank, not alphabet.
_PRIORITY_RANK = case(
    (Project.priority == "low", 0),
    (Project.priority == "normal", 1),
    (Project.priority == "high", 2),
    else_=3,
)
# prep < printing < qc < completed («done») < cancelled (spec workshop-order-stage, rule 27).
_STAGE_RANK = case(
    (Project.status == "cancelled", 4),
    (Project.status == "completed", 3),
    (Project.stage == "prep", 0),
    (Project.stage == "printing", 1),
    else_=2,
)
_ORDER_SORT = SortSpec(
    sql={
        "updated": (Project.updated_at, False),
        "created": (Project.created_at, False),
        "name": (func.lower(Project.name), False),
        "due": (Project.due_date, True),
        "priority": (_PRIORITY_RANK, False),
        "customer": (func.lower(Customer.name), True),
        "stage": (_STAGE_RANK, False),
    },
    computed={"progress", "remaining", "printing", "queued", "ready", "hours"},
    default="updated-desc",
)
_ORDER_COMPUTED = {
    "progress": lambda r: r.progress,
    "remaining": lambda r: r.remaining,
    "printing": lambda r: r.prints_in_progress,
    "queued": lambda r: r.prints_queued,
}
# Figures of the farm forecast (spec workshop-lists, rule 7): no row carries
# them, so a request sorting by one runs ONE simulation walk over the active
# orders of the filtered set. A value of None (closed order, incomplete or no
# estimate) sorts last in both directions — ``sort_computed``'s rule.
_ORDER_FORECAST = {
    "ready": lambda f: f.now_eta if f.eta_complete else None,
    "hours": lambda f: f.machine_seconds,
}


def _order_search(query, q: str):
    """``q`` on the order name, its customer's name, its tags or its code; the caller has joined Customer."""
    needle = f"%{q.strip()}%"
    conditions = [Project.name.ilike(needle), Customer.name.ilike(needle), Project.tags.ilike(needle)]
    if (order_id := id_from_query("order", q)) is not None:
        conditions.append(Project.id == order_id)
    return query.where(or_(*conditions))


def _order_filters(
    query, *, customer_id: int | None, product_id: int | None, responsible_id: int | None, stage: str | None
):
    """The list's filters but ``status`` and ``q`` — one copy for the rows, the tabs and the stage counts."""
    if customer_id is not None:
        query = query.where(Project.customer_id == customer_id)
    if product_id is not None:
        # "Where is this product ordered?" — a subquery over the lines rather
        # than a join, so an order carrying two lines of the same product is
        # still one row. Composes with the other filters.
        query = query.where(Project.id.in_(select(ProjectLine.project_id).where(ProjectLine.product_id == product_id)))
    if responsible_id is not None:
        query = query.where(Project.responsible_id == responsible_id)
    if stage is not None:
        # «done» is not a stored stage — it is a completed order (spec workshop-order-stage, rule 2).
        if stage == "done":
            query = query.where(Project.status == "completed")
        elif stage in PROJECT_STAGES:
            query = query.where(Project.status == "active", Project.stage == stage)
        else:
            raise HTTPException(status_code=400, detail="Invalid stage")
    return query


async def _order_totals(
    db: AsyncSession,
    *,
    customer_id: int | None,
    product_id: int | None,
    q: str | None,
    responsible_id: int | None = None,
    stage: str | None = None,
) -> OrderListTotals:
    """Tab counts under the current filters WITHOUT status, so the tabs tell the
    truth under the chosen customer or search — not the whole farm's numbers.

    ``stages`` counts the ACTIVE orders per stage under every filter but status
    and stage itself (spec workshop-order-stage, rule 27) — the columns of the
    board, which must not collapse to the one stage being looked at."""
    base = select(Project.status, func.count(Project.id)).group_by(Project.status)
    base = _order_filters(
        base, customer_id=customer_id, product_id=product_id, responsible_id=responsible_id, stage=stage
    )
    by_stage = select(Project.stage, func.count(Project.id)).where(Project.status == "active").group_by(Project.stage)
    by_stage = _order_filters(
        by_stage, customer_id=customer_id, product_id=product_id, responsible_id=responsible_id, stage=None
    )
    if q:
        base = _order_search(base.outerjoin(Customer, Customer.id == Project.customer_id), q)
        by_stage = _order_search(by_stage.outerjoin(Customer, Customer.id == Project.customer_id), q)
    counts = dict((await db.execute(base)).all())
    stage_counts = dict((await db.execute(by_stage)).all())
    return OrderListTotals(
        active=counts.get("active", 0),
        completed=counts.get("completed", 0),
        cancelled=counts.get("cancelled", 0),
        all=sum(counts.values()),
        stages=OrderStageCounts(**{name: stage_counts.get(name, 0) for name in PROJECT_STAGES}),
    )


@router.get("", response_model=list[ProjectListResponse] | OrderListPage)
@router.get("/", response_model=list[ProjectListResponse] | OrderListPage)
async def list_projects(
    status: str | None = None,
    customer_id: int | None = None,
    product_id: int | None = None,
    responsible_id: int | None = None,
    stage: str | None = Query(None, description="prep | printing | qc (active orders) or done (completed)"),
    q: str | None = Query(
        None, description="With page set: ilike on the order name, its customer's name or its tags, or an OR code"
    ),
    sort_by: str | None = Query(
        None, description="With page set: '<key>-<asc|desc>'; unknown → updated-desc; ready/hours run the forecast"
    ),
    page: int | None = Query(None, ge=1, description="Omit entirely for the legacy flat-array response"),
    per_page: int = Query(24, ge=1, le=200),
    all: bool = Query(False, description="With page set, skip pagination and return every matching row"),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ORDERS_READ),
):
    """The orders list. ``page`` is the compat switch (the inventory's contract).

    Without it the answer is the flat array every existing reader takes — the
    customer and product pages, the order candidates — byte for byte as before.
    With it: ``{items, meta, totals}``, ``q`` and ``sort_by`` apply, and
    ``totals`` counts the tabs under every filter but ``status``.

    A SQL sort key pages in the database, and the figures below are computed
    for that page only. A computed key (``progress``, ``remaining``,
    ``printing``, ``queued``) is a figure no column carries: the filtered set
    is loaded whole, its figures computed as always, sorted here and sliced —
    exactly the cost of the unpaged list, which is what it replaces.
    ``ready`` / ``hours`` sort by the farm forecast: one ``forecast_projects``
    walk over the set's active orders per request.
    """
    paged = page is not None
    key, direction, computed = resolve_sort(_ORDER_SORT, sort_by)
    query = _row_query()
    if not paged:
        query = query.order_by(Project.updated_at.desc())
    if status:
        # Same answer ``update_project`` gives an unknown status: an empty list
        # reads as "no orders like that" and hides the typo — most cruelly for
        # ``archived``, which m158 retired and which a stale bookmark still asks for.
        if status not in PROJECT_STATUSES:
            raise HTTPException(status_code=400, detail="Invalid status")
        query = query.where(Project.status == status)
    query = _order_filters(
        query, customer_id=customer_id, product_id=product_id, responsible_id=responsible_id, stage=stage
    )
    total = 0
    totals: OrderListTotals | None = None
    if paged:
        # One join for both needs: the search reads the customer's name, and so
        # does the ``customer`` sort key.
        if q or key == "customer":
            query = query.outerjoin(Customer, Customer.id == Project.customer_id)
        if q:
            query = _order_search(query, q)
        totals = await _order_totals(
            db, customer_id=customer_id, product_id=product_id, q=q, responsible_id=responsible_id, stage=stage
        )
        if not computed:
            total = await db.scalar(select(func.count()).select_from(query.subquery())) or 0
            query = apply_sql_sort(query, _ORDER_SORT, key, direction, Project.id)
            if not all:
                query = query.limit(per_page).offset((page - 1) * per_page)
    projects = (await db.execute(query)).scalars().all()
    out = await _list_rows(db, projects)
    if not paged:
        return await _with_product_lines(db, projects, out, product_id)
    if computed:
        if key in _ORDER_FORECAST:
            # The cost is honest: one full walk of the simulation over the active
            # orders under the filter, on every request that sorts by it — no
            # cache (owner, 2026-09-25: «поки серверний прогін»). A closed order
            # is never planned, so it has no value.
            active_ids = [row.id for row in out if row.status == "active"]
            forecasts = (await farm_forecast.forecast_projects(db, active_ids, _utc_now()))[1] if active_ids else {}
            value_of = _ORDER_FORECAST[key]

            def key_fn(row):
                forecast = forecasts.get(row.id)
                return value_of(forecast) if forecast is not None else None
        else:
            key_fn = _ORDER_COMPUTED[key]
        out = sort_computed(out, key_fn, direction, id_fn=lambda r: r.id)
        total = len(out)
        out = slice_page(out, page, per_page, all)
    out = await _with_product_lines(db, projects, out, product_id)
    return OrderListPage(items=out, meta=page_meta(total, page, per_page, all), totals=totals)


async def _with_product_lines(
    db: AsyncSession, projects: Sequence[Project], rows: list[ProjectListResponse], product_id: int | None
) -> list[ProjectListResponse]:
    """WS-13 E9 A02 — each row's lines of ``product_id``, in line order, for the rows
    given (the page, after any computed cut). One batched pass: the product's groups,
    parts and standard, and the lines' configurations — never a read per row. Without
    ``product_id`` nothing is read and the rows keep ``None``."""
    if product_id is None or not rows:
        return rows
    wanted = {row.id for row in rows}
    lines_by_order = {
        project.id: sorted(
            (line for line in project.lines if line.product_id == product_id),
            key=lambda line: (line.sort_order, line.id),
        )
        for project in projects
        if project.id in wanted
    }
    line_ids = [line.id for lines in lines_by_order.values() for line in lines]
    configs = await load_line_configs(db, line_ids) if line_ids else {}
    groups = (await groups_by_product(db, [product_id])).get(product_id, [])
    parts = list((await db.execute(select(ProductPart).where(ProductPart.product_id == product_id))).scalars())
    defaults = (await default_options(db, [product_id])).get(product_id, {})
    for row in rows:
        row.product_lines = [
            ProductLineRef(
                line_id=line.id,
                mode=line.mode,
                quantity=line.quantity,
                configuration=configuration_out(groups, parts, configs.get(line.id, LineConfig()), defaults, line.mode),
            )
            for line in lines_by_order.get(row.id, [])
        ]
    return rows


def _row_query():
    """A ``Project`` select loaded the way ``_list_rows`` reads it."""
    return select(Project).options(
        selectinload(Project.lines), selectinload(Project.customer), selectinload(Project.responsible)
    )


async def _list_rows(db: AsyncSession, projects: Sequence[Project]) -> list[ProjectListResponse]:
    """The one builder of the list row (spec workshop-order-views, rule 20): the
    list, the board and the deadlines view send the same row, with the same
    figures batch (``grouped_figures``) asked once for all the rows given.
    ``projects`` must be loaded with ``lines``, ``customer`` and ``responsible``
    (``_row_query``)."""
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
    # «issued X of Y» (spec workshop-order-issue, rule 23): a product line's column, a
    # parts line's parts — one read for every parts line of the page.
    part_counters = await part_stock.line_part_stock(
        db, [line.id for project in projects for line in project.lines if line.mode == "parts"]
    )
    out: list[ProjectListResponse] = []
    for project in projects:
        pf = figures.get(project.id)
        if pf is None:  # deleted between the two statements; nothing to report
            continue
        # ``(sort_order, id)`` — the order every figure path puts the lines in. The
        # relationship's own order is the database's, so the cover strip on the card
        # would otherwise be free to differ from the line list on the order page it opens.
        lines = sorted(project.lines, key=lambda line: (line.sort_order, line.id))
        out.append(
            ProjectListResponse(
                id=project.id,
                code=code_for("order", project.id),
                name=project.name,
                customer_id=project.customer_id,
                customer_name=project.customer.name if project.customer else None,
                color=project.color,
                status=project.status,
                stage=_stage_out(project),
                responsible_id=project.responsible_id,
                responsible_name=project.responsible.username if project.responsible else None,
                due_date=project.due_date,
                priority=project.priority,
                price=project.price,
                tags=project.tags,
                cover_image_filename=project.cover_image_filename,
                created_at=project.created_at,
                lines_count=len(project.lines),
                ordered=pf.ordered,
                printed=pf.printed,
                covered_units=pf.covered_units,
                remaining=pf.remaining,
                # Off the same batch as ``ordered``/``printed``/``progress``,
                # already capped per line by ``project_figures`` — no second
                # query and no second copy of the cap rule.
                from_stock_units=pf.from_stock_units,
                issued_units=sum(
                    _line_counters(line, part_counters.get(line.id, {}))["issued"] for line in project.lines
                ),
                prints_in_progress=pf.prints_in_progress,
                prints_queued=pf.prints_queued,
                bankable_surplus=pf.bankable_surplus,
                progress=pf.progress,
                line_products=[
                    LineProductOut(product_id=line.product_id, has_cover=line.product_id in covered) for line in lines
                ],
                materials=list(
                    dict.fromkeys(line.material for line in lines if line.material and line.material.strip())
                ),
                products=[
                    LineProductOut(product_id=product_id, has_cover=product_id in covered)
                    for product_id in dict.fromkeys(line.product_id for line in lines)
                ],
            )
        )
    return out


@router.get("/summary", response_model=OrdersSummary)
async def orders_summary(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ORDERS_READ),
):
    """The orders page's tiles (spec workshop-lists, rules 1–2): the farm's
    ACTIVE orders, whatever the list below them is filtered by. Declared above
    ``/{project_id}``, or ``summary`` would be parsed as an id.

    The figures are the list rows' own (``grouped_figures``), so «queued» counts
    the auto-queue tier exactly as the cards do. «Overdue» is the card's rule on
    the server's clock — the one the daily digest lives by: due before the start
    of today (rule 11); a deadline of today is not overdue yet.
    """
    rows = (
        await db.execute(
            select(Project.id, Project.due_date, Project.priority, Project.stage).where(Project.status == "active")
        )
    ).all()
    start_of_today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    figures = await grouped_figures(db, project_ids=[row.id for row in rows]) if rows else []
    return OrdersSummary(
        active=len(rows),
        overdue=sum(1 for row in rows if row.due_date is not None and row.due_date < start_of_today),
        urgent=sum(1 for row in rows if row.priority == "urgent"),
        printing=sum(f.prints_in_progress for f in figures),
        queued=sum(f.prints_queued for f in figures),
        remaining=sum(f.remaining for f in figures),
        all_covered=sum(1 for f in figures if f.all_printed),
        qc=sum(1 for row in rows if row.stage == "qc"),
    )


@router.get("/nav-badges", response_model=ProjectsNavBadges)
async def projects_nav_badges(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequireAnyPermission(Permission.ORDERS_READ, Permission.PRODUCTS_READ, Permission.STOCK_READ),
):
    """The sidebar badges of the Projects section (spec workshop-nav, rule 9):
    asked from every page of the app, so one COUNT per badge and nothing that
    loads an order — ``/summary`` computes every active order's figures and is
    the tiles', not the menu's. Declared above ``/{project_id}``.

    Three domains in one answer: each count needs its own domain's read and is null
    without it (WS-13 E13 O12) — the route opens to any of the three."""
    view = await workshop_view()
    active = (
        await db.scalar(select(func.count(Project.id)).where(Project.status == "active")) or 0 if view.orders else None
    )
    drafts = (
        (
            await db.scalar(
                select(func.count(Product.id)).where(
                    Product.origin == ProductOrigin.CATALOG.value,
                    Product.is_active.is_(True),
                    Product.status == "draft",
                )
            )
            or 0
        )
        if view.products
        else None
    )
    below_min = (
        await db.scalar(select(func.count(StockItem.id)).where(finished_stock_views.BELOW_MIN)) or 0
        if view.stock
        else None
    )
    return ProjectsNavBadges(active_orders=active, draft_products=drafts, stock_below_min=below_min)


_BOARD_LIMIT = 50
_BOARD_DONE = 6


def _board_filters(query, *, customer_id: int | None, responsible_id: int | None, q: str | None):
    """The list's filters a board keeps (spec workshop-order-views, rule 2): no status, no stage, no page."""
    query = _order_filters(query, customer_id=customer_id, product_id=None, responsible_id=responsible_id, stage=None)
    if q:
        query = _order_search(query.outerjoin(Customer, Customer.id == Project.customer_id), q)
    return query


@router.get("/board", response_model=OrderBoard)
async def get_order_board(
    customer_id: int | None = None,
    responsible_id: int | None = None,
    q: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ORDERS_READ),
):
    """The kanban in one request (spec workshop-order-views, rule 5): active orders
    by their manual stage — the most urgent first, at most ``_BOARD_LIMIT`` each —
    and the latest ``_BOARD_DONE`` completed ones; every column with its total.
    Cancelled orders are not on the board. One figures batch for every card.
    Declared above ``/{project_id}``, or ``board`` would be parsed as an id."""
    filters = {"customer_id": customer_id, "responsible_id": responsible_id, "q": q}
    picked: dict[str, tuple[list[Project], int]] = {}
    for stage in PROJECT_STAGES:
        query = _board_filters(_row_query(), **filters).where(Project.status == "active", Project.stage == stage)
        total = await db.scalar(select(func.count()).select_from(query.subquery())) or 0
        ordered = query.order_by(_PRIORITY_RANK.desc(), Project.due_date.is_(None), Project.due_date, Project.id).limit(
            _BOARD_LIMIT
        )
        picked[stage] = (list((await db.execute(ordered)).scalars().all()), total)
    done = _board_filters(_row_query(), **filters).where(Project.status == "completed")
    done_total = await db.scalar(select(func.count()).select_from(done.subquery())) or 0
    latest = done.order_by(Project.updated_at.desc(), Project.id.desc()).limit(_BOARD_DONE)
    picked["done"] = (list((await db.execute(latest)).scalars().all()), done_total)
    rows = {row.id: row for row in await _list_rows(db, [p for projects, _t in picked.values() for p in projects])}
    return OrderBoard(
        **{
            key: OrderBoardColumn(items=[rows[p.id] for p in projects if p.id in rows], total=total)
            for key, (projects, total) in picked.items()
        }
    )


@router.get("/deadlines", response_model=OrderDeadlines)
async def get_order_deadlines(
    # The upper bound keeps ``start + days`` inside the calendar: past it Python
    # raises OverflowError, which would answer a hand-typed date with a 500.
    start: date = Query(..., le=date(9999, 11, 1), description="The first day of the window, YYYY-MM-DD"),
    days: int = Query(14, ge=1, le=42),
    customer_id: int | None = None,
    responsible_id: int | None = None,
    q: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ORDERS_READ),
):
    """The deadlines board (spec workshop-order-views, rules 14–16). The forecast is
    one ``forecast_projects`` walk over the active orders under the filters, on
    every request — no cache (owner, 2026-09-26). An ETA counts only when the
    simulation is complete (the «Ready» sort's rule); «late» and the attention
    reasons come from ``services/order_deadlines``, and so does the calendar: the
    window, the deadline days and «today» are the server's own days, the ETA a
    naive-UTC instant turned into them (``server_wall_time``). Only the orders
    that are shown load as full rows; the rest of the active set is read as
    id / name / deadline. Declared above ``/{project_id}``."""
    window_start = datetime.combine(start, datetime.min.time())
    window_end = window_start + timedelta(days=days)
    filters = {"customer_id": customer_id, "responsible_id": responsible_id, "q": q}
    due_projects = list(
        (
            await db.execute(
                _board_filters(_row_query(), **filters)
                .where(Project.status.in_(("active", "completed")))
                .where(Project.due_date >= window_start, Project.due_date < window_end)
                .order_by(Project.due_date, Project.id)
            )
        )
        .scalars()
        .all()
    )
    active = (
        await db.execute(
            _board_filters(select(Project.id, Project.name, Project.due_date), **filters).where(
                Project.status == "active"
            )
        )
    ).all()
    forecasts = (await farm_forecast.forecast_projects(db, [p.id for p in active], _utc_now()))[1] if active else {}
    eta = {pid: forecast.now_eta if forecast.eta_complete else None for pid, forecast in forecasts.items()}
    # WS-13 E7 H01: the estimate's reasons — ``eta_complete=False`` always carries
    # ``unknown_time`` / ``unroutable``, and an admitted ETA may still carry
    # ``no_plate`` & co. One rule: an estimate with reasons is «partial».
    estimate = {
        pid: [EstimateReasonOut(code=code, count=count) for code, count in forecast.incomplete_reasons]
        for pid, forecast in forecasts.items()
    }
    start_of_today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    reasons = {
        p.id: attention_reason(p.due_date, eta.get(p.id), start_of_today, partial=bool(estimate.get(p.id)))
        for p in active
    }
    flagged = sorted(
        (p for p in active if reasons[p.id]),
        key=lambda p: (ATTENTION_ORDER.index(reasons[p.id]), p.due_date is None, p.due_date or datetime.max, p.id),
    )
    due_ids = {p.id for p in due_projects}
    extra_ids = [p.id for p in flagged if p.id not in due_ids]
    extra = (await db.execute(_row_query().where(Project.id.in_(extra_ids)))).scalars().all() if extra_ids else []
    rows = {r.id: r for r in await _list_rows(db, [*due_projects, *extra])}
    marks = [
        EtaMark(id=p.id, code=code_for("order", p.id), name=p.name, eta=eta[p.id])
        for p in active
        if p.id not in due_ids
        and eta.get(p.id) is not None
        and window_start <= server_wall_time(eta[p.id]) < window_end
    ]
    return OrderDeadlines(
        start=start,
        days=days,
        due=[
            DeadlineOrder(
                order=rows[p.id],
                eta=eta.get(p.id),
                late=eta_is_late(eta.get(p.id), p.due_date),
                estimate_reasons=estimate.get(p.id) if p.status == "active" else None,
            )
            for p in due_projects
            if p.id in rows
        ],
        eta_marks=sorted(marks, key=lambda m: (m.eta, m.id)),
        attention=[
            AttentionOrder(
                order=rows[p.id], reason=reasons[p.id], eta=eta.get(p.id), estimate_reasons=estimate.get(p.id, [])
            )
            for p in flagged
            if p.id in rows
        ],
    )


@router.get("/assignees", response_model=list[OrderAssigneeOut])
async def list_order_assignees(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ORDERS_READ),
):
    """Who may be made responsible for an order (spec workshop-order-stage, rule 26):
    every active user, by name. Under ``orders:read`` — the administrative user
    list is not something everyone who works with orders may read. Declared above
    ``/{project_id}``, or ``assignees`` would be parsed as an id."""
    rows = (
        await db.execute(select(User.id, User.username).where(User.is_active.is_(True)).order_by(User.username))
    ).all()
    return [OrderAssigneeOut(id=row.id, username=row.username) for row in rows]


async def _check_customer(db: AsyncSession, customer_id: int | None) -> None:
    if customer_id is not None and await db.get(Customer, customer_id) is None:
        raise HTTPException(status_code=404, detail="Customer not found")


async def _check_contact(db: AsyncSession, contact_id: int | None, customer_id: int | None) -> None:
    """The order's contact must be a contact of the customer the order has after
    this request (spec workshop-customers, rule 17) — no customer, no contact."""
    if contact_id is None:
        return
    contact = await db.get(CustomerContact, contact_id)
    if contact is None or customer_id is None or contact.customer_id != customer_id:
        raise HTTPException(status_code=422, detail=f"Contact {contact_id} does not belong to this order's customer")


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


_PARTS_LINE_NO_STOCK = "A parts line takes nothing from the shelf"
_LINE_MOVED = "This line's stock has moved; take more from stock instead"
# WS-13 E1 BL6: the order row is held by another door right now — the delete does not wait.
_ORDER_BUSY = "This order is being changed right now — try again"


def _product_busy() -> HTTPException:
    """A one-off product's delete met a busy footprint (WS-13 E1 BL5) — the product
    routes' own 409 body, never a wait and never a 500."""
    return HTTPException(status_code=409, detail={"error": "product_busy", "message": product_gate_module.PRODUCT_BUSY})


def _check_line_create(data: ProjectLineCreate) -> None:
    """A parts line has no kits and no ready units, so nothing to take off a shelf
    (rule 16) — the same answer its PATCH gives."""
    if data.mode == "parts" and (data.from_stock_units or data.from_finished):
        raise HTTPException(status_code=422, detail=_PARTS_LINE_NO_STOCK)


async def _release(db: AsyncSession, line: ProjectLine, note: str, actor: User | None = None) -> None:
    """Everything under the order becomes free stock (line deleted, order cancelled or
    deleted — spec workshop-order-issue, rule 14): the units the line holds on the shelf,
    a parts line's parts, and the kits nobody assembled. What was issued stays issued."""
    try:
        await finished_stock.give_back_for_line(db, line, actor=actor)
        await part_stock.return_parts_for_line(db, line, created_by=actor.id if actor is not None else None)
        await part_stock.release_for_line(db, line, note=note)
    except (part_stock.PartStockError, finished_stock.FinishedStockError) as e:
        raise HTTPException(status_code=409, detail=str(e)) from e


def _consumed_its_stock(status: str | None) -> bool:
    """Has this order already EATEN the kits it took off the shelf (Ruling 25)?

    A completed order shipped them: the kits left the building inside the units
    the customer got, and the movements that took them off the shelf are the
    record of it. Cancelling that order afterwards, deleting one of its lines or
    deleting the whole thing must therefore NOT hand them back — the shelf would
    grow by parts nobody can find on it, and the next order would be planned
    against stock that is in a box on a lorry.

    Every other status still owns them, so every other status releases.
    """
    return status == "completed"


@router.post("", response_model=ProjectResponse)
@router.post("/", response_model=ProjectResponse)
@part_images.attach
async def create_project(
    data: ProjectCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.ORDERS_CREATE),
    creds: RequestCredentials = Depends(request_credentials),
):
    specs = [_spec_of(line) for line in data.lines]
    await _intake_rights(creds, specs)
    await _check_customer(db, data.customer_id)
    await _check_contact(db, data.contact_id, data.customer_id)
    # Every product exists before the order does: a refused line creates nothing.
    for line in data.lines:
        await _check_product(db, line.product_id)
    for line in data.lines:
        _check_line_create(line)
    if "responsible_id" in data.model_fields_set:
        await _check_responsible(db, data.responsible_id)
        responsible_id = data.responsible_id
    else:
        # Rule 10 of spec workshop-order-stage: an order without the field
        # belongs to whoever created it.
        responsible_id = current_user.id if current_user else None
    # The gates of every product before the order is inserted (WS-13 E1 BL3); the
    # intake below re-enters them.
    await product_gate(db, [line.product_id for line in data.lines])
    project = Project(**data.model_dump(exclude={"lines", "responsible_id"}), responsible_id=responsible_id)
    # Set BEFORE the flush: on a pending row the collection starts empty without a
    # query, and the intake below appends to it — touching it after the flush would
    # be a lazy load, which async SQLAlchemy refuses.
    project.lines = []
    db.add(project)
    await db.flush()
    await order_journal.record(db, project.id, "order_created", {"source": "manual"}, actor=current_user)
    # An order created WITH its lines takes the same road as a line added later
    # (spec workshop-add-to-order, rule 12) — configuration, stock and journal —
    # so the same dialog never means something else on the path that creates most lines.
    await _intake(db, project, specs, current_user, _no_library_file)
    return await _response(db, project.id)


@router.post("/from-files", response_model=ProjectResponse)
@part_images.attach
async def create_project_from_files(
    data: OrderFromFilesRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.ORDERS_CREATE),
    creds: RequestCredentials = Depends(request_credentials),
):
    """Product + order out of library files, with nobody authoring either
    (spec 2026-09-06, Slice C). One request, one transaction: a refusal after
    the product was created rolls the product back with it. Three shapes by
    ``kind``: ``job`` (the wizard, targets per part), ``catalog`` (the wizard
    over the one catalogue product linking every file), ``plates`` (the print
    dialog, copies per plate). The files are the caller's to name — the library's
    library's own reading rule (:func:`_library_visible`)."""
    if data.kind == "catalog":
        # An order of a catalog product names the catalog (WS-13 E13 O06); a job or plates
        # order makes its own one-off products, which belong to the order.
        await ensure(creds, Permission.PRODUCTS_READ)
    visible = await _library_visible(request, db, current_user)
    try:
        if data.kind == "job":
            project = await order_from_files.create_job_order(
                db, name=data.name, file_ids=data.file_ids, targets=data.targets, visible=visible
            )
        elif data.kind == "catalog":
            project = await order_from_files.create_catalog_order(
                db,
                name=data.name,
                product_id=data.product_id,
                file_ids=data.file_ids,
                quantity=data.quantity,
                visible=visible,
            )
        else:
            project = await order_from_files.create_plates_order(
                db,
                library_file_id=data.library_file_id,
                plates=[(p.plate_index, p.copies) for p in data.plates],
                name=data.name,
                visible=visible,
            )
    except order_from_files.FileNotFound:
        raise HTTPException(status_code=404, detail="Library file not found")
    except order_from_files.NotPlannable:
        raise HTTPException(status_code=400, detail="Only 3MF files can be planned")
    except order_from_files.NoTargets:
        raise HTTPException(status_code=400, detail="No part has a target")
    except order_from_files.UnknownPartKey as e:
        raise HTTPException(status_code=400, detail=f"Unknown part key: {e.key}")
    except order_from_files.PlateNotFound:
        raise HTTPException(status_code=404, detail="Plate not found")
    except order_from_files.DuplicatePlate:
        raise HTTPException(status_code=400, detail="Duplicate plate")
    except order_from_files.NotACatalogProduct:
        raise HTTPException(status_code=400, detail="Product is not a catalogue product")
    except order_from_files.FilesNotLinked:
        raise HTTPException(status_code=400, detail="Every file must be linked to the product")
    # Rule 10 of spec workshop-order-stage: the author is responsible.
    project.responsible_id = current_user.id if current_user else None
    await order_journal.record(db, project.id, "order_created", {"source": "files"}, actor=current_user)
    return await _response(db, project.id)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _order_forecast_fields(f: farm_forecast.OrderForecast) -> dict:
    return {
        "project_id": f.project_id,
        "now_eta": f.now_eta,
        "now_seconds": f.now_seconds,
        "after_eta": f.after_eta,
        "after_seconds": f.after_seconds,
        "machine_seconds": f.machine_seconds,
        "unknown_prints": f.unknown_prints,
        "unroutable_prints": f.unroutable_prints,
        "eta_complete": f.eta_complete,
        "ahead_count": f.ahead_count,
        "assumptions": list(f.assumptions),
        "incomplete_reasons": [EstimateReasonOut(code=code, count=count) for code, count in f.incomplete_reasons],
        "late": f.late,
    }


@router.get("/forecast", response_model=ForecastBatchOut)
async def get_orders_forecast(
    ids: str | None = Query(None, description="Comma-separated order ids, at most 200"),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ORDERS_READ),
):
    """«Ready by» for a page of orders (spec 2026-09-06, Slice B). Advisory:
    reads the database only, gates nothing. An unknown id is absent; a closed
    one answers the empty forecast.

    ``isdecimal`` rather than ``isdigit``: the latter accepts a superscript
    «²», which ``int()`` then refuses with a 500. The upper bound is the
    column's — an id past int32 is not an id, and asyncpg raises on it rather
    than answering «no such order».
    """
    parsed: list[int] = []
    for part in (ids or "").split(","):
        part = part.strip()
        if not part.isdecimal():
            continue
        value = int(part)
        if 0 < value < 2**31 and value not in parsed:
            parsed.append(value)
    if not parsed:
        raise HTTPException(status_code=400, detail="ids is required")
    if len(parsed) > 200:
        raise HTTPException(status_code=400, detail="at most 200 ids")
    now = _utc_now()
    farm, orders = await farm_forecast.forecast_projects(db, parsed, now)
    return ForecastBatchOut(
        farm=FarmForecastOut.of(now, farm),
        orders=[OrderForecastOut(**_order_forecast_fields(orders[pid])) for pid in parsed if pid in orders],
    )


def _need_row_fields(r: filament_needs.NeedRow) -> dict:
    return {
        "material": r.material,
        "colour": r.colour,
        "need_g": r.need_g,
        "have_g": r.have_g,
        "have_type_g": r.have_type_g,
        "short_g": r.short_g,
        "unknown_prints": r.unknown_prints,
    }


@router.get("/filament", response_model=FarmNeedsOut)
async def get_orders_filament(
    db: AsyncSession = Depends(get_db), _: User | None = RequirePermission(Permission.ORDERS_READ)
):
    """What every active order still needs, per material and colour, against the shelf."""
    farm = await filament_needs.needs_of_farm(db)
    return FarmNeedsOut(
        rows=[FarmRowOut(**_need_row_fields(r), orders_count=r.orders_count) for r in farm.rows],
        orders_count=farm.orders_count,
        unknown_prints=farm.unknown_prints,
        stock_unavailable=farm.stock_unavailable,
        assumptions=list(filament_needs.ASSUMPTIONS),
    )


@router.get("/{project_id}", response_model=ProjectResponse)
@part_images.attach
async def get_project(
    project_id: int, db: AsyncSession = Depends(get_db), _: User | None = RequirePermission(Permission.ORDERS_READ)
):
    return await _response(db, project_id)


# Journaled as «order details changed» (spec workshop-order-stage, rule 17):
# column → the field code the frontend translates. Status and the responsible
# user have journal lines of their own.
_JOURNAL_FIELDS = {
    "name": "name", "customer_id": "customer", "contact_id": "contact", "description": "description",
    "color": "color", "notes": "notes", "tags": "tags", "due_date": "due_date", "priority": "priority",
    "price": "price", "url": "url",
}  # fmt: skip


@router.patch("/{project_id}", response_model=ProjectResponse)
@part_images.attach
async def update_project(
    project_id: int,
    data: ProjectUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.ORDERS_UPDATE),
):
    project = await _get_project(db, project_id)
    completing = data.status == "completed" and project.status != "completed"
    gated = {line.product_id for line in project.lines}
    if completing:
        # Completing may create positions (closing to stock): the gates of the order's
        # products come first, before the order row (WS-13 E1 BL3).
        await product_gate(db, sorted(gated))
    # The order row before anything is written or locked (WS-13 E1 BL0 / BL6): a cancel
    # or a completion goes on to the order's positions and lines, and the row taken
    # later — by the autoflushed UPDATE — would sit after them.
    await order_fulfilment.lock_order(db, project.id)
    project = await _get_project(db, project_id, fresh=True)
    lines = list(project.lines)
    if completing and not {line.product_id for line in lines} <= gated:
        # A line of another product arrived between the read and the gates (BL2): its
        # gate would come after the order row, so the whole request starts over.
        raise HTTPException(status_code=409, detail=order_fulfilment.ORDER_CHANGED)
    # Read BEFORE the fields are written: cancelling asks what the order was,
    # not what it is about to become (Ruling 25).
    was_completed = _consumed_its_stock(project.status)
    if data.status is not None and data.status not in PROJECT_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid status")
    if data.priority is not None and data.priority not in PROJECT_PRIORITIES:
        raise HTTPException(status_code=400, detail="Invalid priority")
    if "customer_id" in data.model_fields_set:
        await _check_customer(db, data.customer_id)
    moves_customer = "customer_id" in data.model_fields_set and data.customer_id != project.customer_id
    if "contact_id" in data.model_fields_set:
        target = data.customer_id if "customer_id" in data.model_fields_set else project.customer_id
        await _check_contact(db, data.contact_id, target)
    # Checked only when the value CHANGES (spec workshop-order-stage, rule 9): an
    # order whose responsible was deactivated since must stay editable.
    responsible_change = None
    if "responsible_id" in data.model_fields_set and data.responsible_id != project.responsible_id:
        await _check_responsible(db, data.responsible_id)
        responsible_change = {
            "from": await _responsible_ref(db, project.responsible_id),
            "to": await _responsible_ref(db, data.responsible_id),
        }
    if data.status == "active" and project.status == "cancelled" and await order_fulfilment.moved_line_ids(db, lines):
        # What it gave back is free stock now and may be gone; its prints stay filed under
        # it, and a reactivated order would count them as coverage a second time (rule 15).
        raise HTTPException(status_code=409, detail="This order's stock has moved; duplicate it instead")
    if data.status == "active" and project.status == "completed" and await order_fulfilment.stocked_line_ids(db, lines):
        # Closed to stock (spec workshop-order-issue-followups, rule 41): what it held is free
        # stock now and its prints stay filed under it — as a cancelled order's.
        raise HTTPException(status_code=409, detail="This order's goods went to free stock; duplicate it instead")
    closing_to_stock = False
    if data.status == "completed" and project.status != "completed":
        # An order completes only when everything it ordered went out (spec
        # workshop-order-issue, rule 12) — or, without a customer, is on the shelf (followups,
        # rule 36) — refused before anything is written. Judged by the customer the order
        # ends this request with.
        customer_after = data.customer_id if "customer_id" in data.model_fields_set else project.customer_id
        closing_to_stock = customer_after is None
        if not (await order_fulfilment.state(db, project, to_stock=closing_to_stock)).can_complete:
            raise HTTPException(
                status_code=409,
                detail="Receive everything the order needs before closing it to stock"
                if closing_to_stock
                else "Issue everything the order holds before completing it",
            )
    # Read before the write: the journal records what actually changed, not what was sent.
    before = {column: getattr(project, column) for column in _JOURNAL_FIELDS}
    status_before = project.status
    # Every field keys off model_fields_set: an explicit null CLEARS, an absent
    # field leaves the column alone (the tags/due_date/#2536 lesson, applied to all).
    for field_name in data.model_fields_set:
        setattr(project, field_name, getattr(data, field_name))
    if moves_customer and "contact_id" not in data.model_fields_set:
        # The contact belonged to the customer the order just left (spec workshop-customers, rule 17).
        project.contact_id = None
    if project.status != status_before:
        await order_journal.record(
            db, project.id, "status_changed", {"from": status_before, "to": project.status}, actor=current_user
        )
    if responsible_change:
        await order_journal.record(db, project.id, "responsible_changed", responsible_change, actor=current_user)
    if project.status == "completed" and status_before != "completed":
        # Everything went out through the issue dialog — or, without a customer, goes to free
        # stock now; kits nobody assembled go back on the shelf, as the dialog's own completion
        # does (spec workshop-order-issue, rule 12; followups, rule 37).
        try:
            await order_fulfilment.close(
                db, project, lines, to_stock=closing_to_stock, actor=await acting_user(request, db, current_user)
            )
        except order_fulfilment.FulfilmentError as e:
            raise HTTPException(status_code=e.status, detail=str(e)) from e
        except (part_stock.PartStockError, finished_stock.FinishedStockError) as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
    changed = [label for column, label in _JOURNAL_FIELDS.items() if getattr(project, column) != before[column]]
    if changed:
        await order_journal.record(db, project.id, "fields_changed", {"fields": changed}, actor=current_user)
    if data.status == "cancelled" and not was_completed:
        # Cancelling gives the shelf its kits back (pass 8, Decision 4) — the
        # order will never consume them. COMPLETING deliberately does not: the
        # stock was consumed, and the movements stand as the record of it.
        #
        # ⚠️ And cancelling an order that was already COMPLETED gives nothing
        # back either (Ruling 25): the kits went out with it. The PREVIOUS
        # status is what decides, which is why it is read before the fields are
        # written above.
        # Cancelling an already-cancelled order releases nothing a second time,
        # because the release reads what the ledger still holds and finds zero.
        #
        # ⚠️ Ruling 18: REACTIVATING a cancelled order does NOT restore its
        # reservations, and deliberately so. The kits went back on the shelf
        # and another order may have taken them since; silently taking them
        # again would be this route deciding, minutes or months later, that
        # this order still outranks whoever is holding them now. The operator
        # re-enters the number in the line dialog, which asks the shelf afresh.
        # Positions, lines, parts of every line before the first is released (WS-13 E1 BL0).
        try:
            await finished_stock.lock_lines_with_parts(db, lines)
        except finished_stock.FinishedStockError as e:
            raise HTTPException(status_code=e.status, detail=str(e)) from e
        for line in lines:
            await _release(db, line, part_stock.NOTE_ORDER_CANCELLED, await acting_user(request, db, current_user))
    return await _response(db, project.id)


@router.put("/{project_id}/stage", response_model=ProjectResponse)
@part_images.attach
async def set_project_stage(
    project_id: int,
    data: ProjectStageUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.ORDERS_UPDATE),
):
    """Set the order's stage by hand — the only way it changes (spec workshop-order-stage, rule 4).

    Whoever is on the farm with ``orders:update`` may set it, and the journal
    says who. The same stage again writes nothing; a closed order has none.
    """
    project = await _get_project(db, project_id)
    if project.status != "active":
        raise HTTPException(status_code=409, detail="Only an active order has a stage")
    if data.stage != project.stage:
        before = project.stage
        project.stage = data.stage
        await order_journal.record(
            db, project.id, "stage_changed", {"from": before, "to": data.stage}, actor=current_user
        )
    return await _response(db, project.id)


# ---------- issuing the order in batches (spec workshop-order-issue, rules 11, 18, 19) ----------


def _recipient_out(recipient: stock_issues.Recipient) -> RecipientOut:
    return RecipientOut(
        name=recipient.name,
        phone=recipient.phone,
        delivery_method=recipient.delivery_method,
        delivery_details=recipient.delivery_details,
    )


async def _line_positions(db: AsyncSession, lines) -> dict[int, StockPositionRefOut]:
    """Each product line's stock position — its (product, configuration key) — in one
    read whatever the number of lines (WS-13 E6 H04). A parts line has none."""
    product_lines = [line for line in lines if line.mode != "parts"]
    if not product_lines:
        return {}
    rows = await db.execute(
        select(StockItem.id, StockItem.product_id, StockItem.config_key, StockItem.location).where(
            StockItem.product_id.in_({line.product_id for line in product_lines})
        )
    )
    by_key = {(product_id, key): (item_id, location) for item_id, product_id, key, location in rows}
    out: dict[int, StockPositionRefOut] = {}
    for line in product_lines:
        found = by_key.get((line.product_id, line.config_key or ""))
        if found is not None:
            item_id, location = found
            out[line.id] = StockPositionRefOut(id=item_id, code=code_for("stock_item", item_id), location=location)
    return out


@router.get("/{project_id}/fulfilment", response_model=FulfilmentStateOut)
@part_images.attach
async def get_fulfilment(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ORDERS_READ),
):
    """What each line can assemble, receive and issue now — the numbers the issue dialog
    shows and the ones ``POST`` checks against (one arithmetic, ``order_fulfilment.state``)
    — and the recipient it starts from."""
    project = await _get_project(db, project_id)
    # WS-13 E6 H04: one context for the numbers AND the captions, read once.
    ctx = await order_fulfilment.load_context(db, project)
    state = await order_fulfilment.state(db, project, ctx=ctx)
    configurations = await _configurations(db, ctx)
    view = await workshop_view()
    positions = await _line_positions(db, ctx.lines)
    if not view.stock:
        # The shelf a position lies on is the stock's; its code stays as the line's label.
        positions = {line_id: ref.model_copy(update={"location": None}) for line_id, ref in positions.items()}
    # The recipient's phone and address — for whoever keeps the contacts or ships (O25).
    recipient = None
    if view.recipient:
        recipient = _recipient_out(
            await stock_issues.default_recipient(db, project=project, customer_id=project.customer_id)
            if project.customer_id is not None
            else stock_issues.Recipient()
        )
    return FulfilmentStateOut(
        lines=[
            LineStateOut(
                **asdict(row),
                configuration=configurations.get(row.line_id),
                stock_position=positions.get(row.line_id),
            )
            for row in state.lines
        ],
        ordered=state.ordered,
        issued=state.issued,
        held=state.held,
        fully_issued=state.fully_issued,
        can_assemble=state.can_assemble,
        can_receive=state.can_receive,
        can_issue=state.can_issue,
        closes_to_stock=state.closes_to_stock,
        can_complete=state.can_complete,
        recipient=recipient,
    )


@router.post("/{project_id}/fulfilment", response_model=FulfilmentOut)
@part_images.attach
async def fulfil_order(
    project_id: int,
    data: FulfilmentIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.ORDERS_UPDATE),
    creds: RequestCredentials = Depends(request_credentials),
):
    """One «Виконати» of the issue dialog: assemble, receive and issue, one issue for the
    whole batch, and — asked and everything issued — the order completed. A number above
    what the order allows is refused with its sentence and nothing is written. Moving goods
    asks ``stock:move``; a write-off corrects the books, so it asks ``stock:adjust`` too; a
    batch that moves nothing and only completes is the order's own right (WS-13 E13 O06/O23)."""
    moves = any(
        line.assemble
        or line.receive
        or line.issue
        or line.write_off
        or any(part.receive or part.issue or part.write_off for part in line.parts)
        for line in data.lines
    )
    if moves:
        await ensure(creds, Permission.STOCK_MOVE)
    if any(line.write_off or any(part.write_off for part in line.parts) for line in data.lines):
        await ensure(creds, Permission.STOCK_ADJUST)
    project = await _get_project(db, project_id)
    requests = []
    for line in data.lines:
        parts: dict[int, tuple[int, int]] = {}
        parts_write_off: dict[int, int] = {}
        for part in line.parts:
            if part.part_id in parts:
                raise HTTPException(status_code=422, detail="A part is named twice")
            parts[part.part_id] = (part.receive, part.issue)
            if part.write_off:
                parts_write_off[part.part_id] = part.write_off
        requests.append(
            order_fulfilment.LineRequest(
                line_id=line.line_id,
                assemble=line.assemble,
                receive=line.receive,
                issue=line.issue,
                write_off=line.write_off,
                parts=parts,
                parts_write_off=parts_write_off,
            )
        )
    try:
        issue = await order_fulfilment.apply(
            db,
            project,
            requests,
            recipient=stock_issues.Recipient(**data.recipient.model_dump()),
            waybill=data.waybill,
            note=data.note,
            complete=data.complete,
            actor=await acting_user(request, db, current_user),
            write_off_note=data.write_off_note,
        )
    except (order_fulfilment.FulfilmentError, stock_issues.StockIssueError, finished_stock.FinishedStockError) as e:
        raise HTTPException(status_code=e.status, detail=str(e)) from e
    except part_stock.PartStockError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return FulfilmentOut(
        order=await _response(db, project.id),
        issue_id=issue.id if issue is not None else None,
        issue_code=code_for("dispatch_note", issue.id) if issue is not None else None,
        issue_units=issue.units if issue is not None else None,
    )


@router.get("/{project_id}/stock-offers", response_model=list[StockOfferOut])
async def get_stock_offers(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ORDERS_READ, Permission.STOCK_READ),
):
    """What the shelves could cover of what this order has not printed, is not printing
    and has not queued (spec workshop-order-issue, rule 17) — an active order only."""
    project = await _get_project(db, project_id)
    return [StockOfferOut(**asdict(offer)) for offer in await stock_offers.offers(db, project)]


@router.post("/{project_id}/take-stock", response_model=TakeStockOut)
@part_images.attach
async def take_stock(
    project_id: int,
    request: Request,
    data: TakeStockIn | None = Body(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.ORDERS_UPDATE, Permission.STOCK_MOVE),
):
    """Take the offers — the numbers the banner showed, clamped to the offer now and to
    the shelf; the answer says what each line asked and got (rule 20)."""
    project = await _get_project(db, project_id)
    shown = None
    if data is not None and data.lines is not None:
        shown = {row.line_id: (row.from_finished, row.kits) for row in data.lines}
    try:
        taken = await stock_offers.take(db, project, shown, actor=await acting_user(request, db, current_user))
    except (stock_offers.StockOfferError, finished_stock.FinishedStockError) as e:
        raise HTTPException(status_code=e.status, detail=str(e)) from e
    except part_stock.PartStockError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return TakeStockOut(order=await _response(db, project.id), results=[LineIntakeOut(**asdict(t)) for t in taken])


@router.delete("/{project_id}")
async def delete_project(
    project_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.ORDERS_DELETE),
):
    """Archives and queue rows survive, unlinked (SET NULL done explicitly — SQLite enforces nothing)."""
    project = await _get_project(db, project_id)
    gated = {line.product_id for line in project.lines}
    # The gates of its products first (WS-13 E1 BL3): the cascade below may delete a
    # one-off product, which is a product writer.
    await product_gate(db, sorted(gated))
    # …and the order read again behind them (BL2): a line added meanwhile is released
    # and detached like the others — or, of a product not gated, the request starts over.
    project = await _get_project(db, project_id, fresh=True)
    if not {line.product_id for line in project.lines} <= gated:
        raise HTTPException(status_code=409, detail=order_fulfilment.ORDER_CHANGED)
    # Its lines go with it, so they go through the same two steps a single
    # deleted line does (Ruling 10): the kits come back to the shelf, and the
    # history that named the line stops naming an id that will be reused.
    #
    # ⚠️ A COMPLETED order releases nothing (Ruling 25) — its kits shipped. The
    # DETACH still runs whatever the status: the line row is going away either
    # way, and a movement left naming a deleted line id would be handed to
    # whichever line SQLite gives that rowid to next.
    #
    # The same answer decides the un-filing credit below (Ruling 32), which is
    # why it is one variable and not two reads of the status.
    still_owns_its_stock = not _consumed_its_stock(project.status)
    # An order whose lines received, assembled or issued anything has USED its prints:
    # the parts are finished units on the shelf or with the customer, so un-filing must
    # not credit them to the free parts shelf a second time — Ruling 32's reason, reached
    # without completing (spec workshop-order-issue; final review C1). Read before the
    # parts lines' counters go with their lines below.
    prints_are_free = still_owns_its_stock and not await order_fulfilment.moved_line_ids(db, list(project.lines))
    actor = await acting_user(request, db, current_user)
    if still_owns_its_stock:
        # Positions, lines, parts of every line before the first is released (WS-13 E1 BL0).
        try:
            await finished_stock.lock_lines_with_parts(db, project.lines)
        except finished_stock.FinishedStockError as e:
            raise HTTPException(status_code=e.status, detail=str(e)) from e
    for line in list(project.lines):
        if still_owns_its_stock:
            await _release(db, line, part_stock.NOTE_PROJECT_DELETED, actor)
        await part_stock.detach_line(db, line.id)
        await line_config.forget_line(db, line.id)
    await finished_stock.detach_project(db, project_id)
    # Its issues stay with the customer (spec workshop-order-issue, rule 14).
    await stock_issues.detach_project(db, project_id)
    # Read before the un-filing: after the UPDATE below, no archive names this
    # order any more and there is nothing left to look them up by.
    unfiled = (await db.execute(select(PrintArchive).where(PrintArchive.project_id == project_id))).scalars().all()
    for model in (PrintArchive, PrintQueueItem, AutoQueueItem):
        await db.execute(
            update(model).where(model.project_id == project_id).values(project_id=None, project_line_id=None)
        )
    # ⚠️ Deleting an order UN-FILES its prints, and un-filing is un-filing
    # (Ruling 26): the parts those prints made are on a shelf and now belong to
    # no order, exactly as they do when «Прибрати архіви» or the archive editor
    # lets one go. Without this, deleting an order was the one door that lost
    # them. Bounded by this order's own archives, idempotent per part, and
    # ordinarily a no-op for anything that did not finish.
    #
    # ⚠️ …unless the order was COMPLETED (Ruling 32), which is the same
    # ``_consumed_its_stock`` question the release above asks, answered the same
    # way twelve lines later: those prints SHIPPED inside the units the customer
    # got. Deleting the paperwork afterwards must not put them back on a shelf
    # nobody can find them on — that is the identical mistake as handing back a
    # completed order's reservation, made from the other end.
    #
    # The two explicit un-filing doors — the archive editor NULLing
    # ``project_id`` and «Прибрати архіви» — keep crediting whatever the status,
    # deliberately: there the operator is saying "this print was not part of
    # that shipment", which is a statement about the print. Deleting an order
    # says nothing at all about any one print.
    #
    # The two columns are cleared on the loaded rows as well as by the statement
    # above: ``credit_unfiled_print`` refuses an archive whose ``project_id`` it
    # can still see, and whether a bulk UPDATE happens to reach the identity map
    # is a synchronisation strategy, not a promise.
    for archive in unfiled:
        archive.project_id = None
        archive.project_line_id = None
        if prints_are_free:
            await part_stock.credit_if_unfiled(db, archive, note=part_stock.NOTE_PROJECT_DELETED)
    line_products = {line.product_id for line in project.lines}
    # The journal goes with the order — in code, SQLite runs no CASCADE.
    await order_journal.delete_for_project(db, project_id)
    # The order row itself goes LAST and without waiting (WS-13 E1 BL6): its FOR UPDATE
    # collides with every insert that merely references the order — a journal entry, a
    # line, an issue — and waiting here while holding positions and lines would close a
    # cycle with such a door. PostgreSQL refuses at once instead; SQLite has no row lock.
    try:
        await db.execute(select(Project.id).where(Project.id == project_id).with_for_update(nowait=True))
    except DBAPIError as e:
        if sqlstate(e) == LOCK_NOT_AVAILABLE:
            raise HTTPException(status_code=409, detail=_ORDER_BUSY) from e
        raise
    await db.delete(project)
    await db.flush()
    # Decision 5: an adhoc product lives exactly as long as a line references it. Its
    # delete takes its footprint without waiting (BL5): busy is a 409, not a 500.
    try:
        await product_delete.delete_orphaned_adhoc_products(db, line_products)
    except product_gate_module.ProductBusy as e:
        raise _product_busy() from e
    return {"message": "Project deleted"}


# ---------- lines ----------


def _spec_of(data: ProjectLineCreate) -> BatchProductLineIn | BatchPartsLineIn:
    """The batch shape of a single-line request: its numbers are the operator's.

    ``model_construct`` on purpose: ``ProjectLineCreate`` was already validated by
    its own rules, and re-validating it under the batch's would change what the
    old route accepts (a parts line with no counts is ``line_config``'s 422 to give)."""
    common = {"material": data.material, "color": data.color, "note": data.note, "product_id": data.product_id}
    if data.mode == "parts":
        return BatchPartsLineIn.model_construct(kind="parts", part_counts=data.part_counts, **common)
    return BatchProductLineIn.model_construct(
        kind="product",
        quantity=data.quantity,
        choices=data.choices,
        part_counts=data.part_counts,
        stock=BatchStockIn.model_construct(from_finished=data.from_finished, from_kits=data.from_stock_units),
        **common,
    )


async def _library_visible(request: Request, db: AsyncSession, user: User | None) -> Callable[[LibraryFile], bool]:
    """The library's own reading rule for an order route that names library files
    (WS-13 E13 B07): ``library_name_scope`` + ``file_name_visible``, as everywhere a
    file's name is shown. A JWT with ``library:read_own`` uses only its own files,
    without a library read right no file at all, its own included; an API key by
    its library scope and its owner's rights. Another user's file is the same 404
    as a missing one."""
    scope = await library_name_scope(request, db, user)
    return lambda f: file_name_visible(f, user, scope)


def _no_library_file(_file: LibraryFile) -> bool:
    """For a route whose lines cannot name a library file (a catalog product's
    line): fail closed, so it never needs the library's rights."""
    return False


def _takes_stock(spec) -> bool:
    """A product or plate line that asks the shelf — ``auto`` or a number above nothing."""
    if getattr(spec, "kind", None) not in ("product", "plate"):
        return False
    stock = spec.stock
    return stock == "auto" or stock.from_finished > 0 or stock.from_kits > 0


async def _intake_rights(creds: RequestCredentials, specs) -> None:
    """What lines ask beside the order's own right (WS-13 E13 O06), before anything is
    written: a product or parts line names the catalog (``products:read``); a line that
    takes from the shelf moves stock — ``auto`` included, so a client without the right
    sends ``none``."""
    if any(getattr(spec, "kind", None) in ("product", "parts") for spec in specs):
        await ensure(creds, Permission.PRODUCTS_READ)
    if any(_takes_stock(spec) for spec in specs):
        await ensure_coded(creds, Permission.STOCK_MOVE, "stock_move_required")


async def _intake(
    db: AsyncSession,
    project: Project,
    specs,
    actor: User | None,
    visible: Callable[[LibraryFile], bool],
) -> list[line_intake.Intake]:
    try:
        return await line_intake.add_lines(db, project, specs, actor=actor, visible=visible)
    except line_intake.LineIntakeError as e:
        raise HTTPException(status_code=e.status, detail=str(e)) from e
    except (part_stock.PartStockError, finished_stock.FinishedStockError) as e:
        raise HTTPException(status_code=409, detail=str(e)) from e


@router.post("/{project_id}/lines/batch", response_model=BatchLinesOut)
@part_images.attach
async def add_lines_batch(
    project_id: int,
    data: BatchLinesIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.ORDERS_UPDATE),
    creds: RequestCredentials = Depends(request_credentials),
):
    """Add many lines in one transaction (spec workshop-add-to-order, rule 11): products
    with their configuration and stock, parts of a product, one-offs from a file plate.
    Any refused line refuses the whole batch. The stock is taken as far as the shelf
    goes; ``results`` says what each line asked and got."""
    await _intake_rights(creds, data.lines)
    project = await _get_project(db, project_id)
    intakes = await _intake(db, project, data.lines, current_user, await _library_visible(request, db, current_user))
    return BatchLinesOut(
        order=await _response(db, project.id),
        results=[
            LineIntakeOut(
                line_id=i.line.id,
                asked_finished=i.asked_finished,
                got_finished=i.got_finished,
                asked_kits=i.asked_kits,
                got_kits=i.got_kits,
            )
            for i in intakes
        ],
    )


@router.post("/{project_id}/lines", response_model=ProjectResponse)
@part_images.attach
async def add_line(
    project_id: int,
    data: ProjectLineCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.ORDERS_UPDATE),
    creds: RequestCredentials = Depends(request_credentials),
):
    spec = _spec_of(data)
    await _intake_rights(creds, [spec])
    project = await _get_project(db, project_id)
    _check_line_create(data)
    await _intake(db, project, [spec], current_user, _no_library_file)
    return await _response(db, project.id)


async def _get_line(db: AsyncSession, project_id: int, line_id: int, *, fresh: bool = False) -> ProjectLine:
    """``fresh`` — re-read behind a lock (WS-13 E1 BL2), not from the identity map."""
    line = await db.get(ProjectLine, line_id, populate_existing=fresh)
    if line is None or line.project_id != project_id:
        raise HTTPException(status_code=404, detail="Order line not found")
    return line


@router.patch("/{project_id}/lines/{line_id}", response_model=ProjectResponse)
@part_images.attach
async def update_line(
    project_id: int,
    line_id: int,
    data: ProjectLineUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.ORDERS_UPDATE),
    creds: RequestCredentials = Depends(request_credentials),
):
    line = await _get_line(db, project_id, line_id)
    if line.mode == "parts":
        # Rules 15–16: one set of parts, never kits.
        if "quantity" in data.model_fields_set and data.quantity != 1:
            raise HTTPException(status_code=422, detail="A parts line always has quantity 1")
        if data.from_stock_units or data.from_finished:
            raise HTTPException(status_code=422, detail=_PARTS_LINE_NO_STOCK)
    if line.mode != "parts" and data.model_fields_set & {"quantity", "from_finished", "from_stock_units"}:
        # The line's positions, then the line, before a field of it is written (WS-13 E1
        # BL6): the write would take the line first, and the issue dialog takes them the
        # other way round.
        try:
            await finished_stock.lock_line(db, line)
        except finished_stock.FinishedStockError as e:
            raise HTTPException(status_code=e.status, detail=str(e)) from e
    if line.mode != "parts" and finished_stock.moved(line):
        # After the first movement the ready units are only added to — through «take from
        # stock» — the kits only come DOWN (spec workshop-order-issue-followups, rule 42: back
        # onto the shelf, the prints already made take their place), and the quantity stays
        # above what went out and what is held.
        if "from_finished" in data.model_fields_set:
            raise HTTPException(status_code=409, detail=_LINE_MOVED)
        if "from_stock_units" in data.model_fields_set:
            live = await part_stock.reserved_units_for_line(db, line)
            if data.from_stock_units is None or data.from_stock_units > live:
                raise HTTPException(status_code=409, detail=_LINE_MOVED)
        if "quantity" in data.model_fields_set:
            await finished_stock.lock_line(db, line)
            floor = (line.issued or 0) + finished_stock.held_units(line)
            if data.quantity is not None and data.quantity < floor:
                raise HTTPException(
                    status_code=409,
                    detail=f"The quantity cannot go below what is issued and held for this order ({floor})",
                )
    wants_finished = data.from_finished is not None
    if wants_finished:
        # Ready units are taken only by an ACTIVE order (spec workshop-add-to-order,
        # rule 7): a completed one already shipped what it held, and a reservation
        # made now would hang on the shelf for ever. Unlike the kits (Ruling 33) —
        # correcting them on a completed order puts parts back where they are.
        status = await db.scalar(select(Project.status).where(Project.id == project_id))
        if status != "active":
            raise HTTPException(status_code=409, detail="Only an active order takes finished goods from stock")
    # Read before the write: the journal says what changed, not what was sent.
    tracked = ("quantity", "material", "color", "note")
    before = {field_name: getattr(line, field_name) for field_name in tracked}
    stock_before = await part_stock.reserved_units_for_line(db, line)
    finished_before = await finished_stock.held_for_line(db, line.id)
    # Taking MORE off a shelf moves stock; giving back is the edit's own consequence
    # (WS-13 E13 O23). Asked here — under the line's locks, before the first write (O22).
    takes_more = (wants_finished and data.from_finished > (line.from_finished or 0)) or (
        data.from_stock_units is not None and data.from_stock_units > stock_before
    )
    if takes_more:
        await ensure(creds, Permission.STOCK_MOVE)
    for field_name in data.model_fields_set - {"from_stock_units", "from_finished"}:
        setattr(line, field_name, getattr(data, field_name))
    try:
        if wants_finished:
            # Ready units first (rule 6); the kits below are fitted into what is left.
            # The number is the line's total, issued units included (rule 1).
            await finished_stock.reserve_for_line(db, line, min(data.from_finished, line.quantity), actor=current_user)
        elif line.from_finished > line.quantity and await finished_stock.held_for_line(db, line.id):
            # The quantity came down under the ready units the line still holds: they
            # are fitted too, whether or not a kits number came with it (final review
            # I3). Only downwards — a quantity going up does not help itself to more.
            await finished_stock.reserve_for_line(db, line, line.quantity, actor=current_user)
    except finished_stock.FinishedStockError as e:
        raise HTTPException(status_code=e.status, detail=str(e)) from e
    if data.from_stock_units is not None:
        # ⚠️ The fourth reservation door, and the one that is deliberately NOT
        # gated on :func:`_consumed_its_stock` (Ruling 33). Cancelling, deleting
        # a line and deleting the order all withhold the release from a
        # completed order because none of them says anything about the kits —
        # they dispose of paperwork, and the parts shipped. Typing a new number
        # into this field DOES say something about them: it is the operator
        # correcting what the order took off the shelf, and refusing it on a
        # completed order would leave a mistake with no door to fix it through.
        #
        # Rewritten, never adjusted by a difference: ``reserve_for_line``
        # releases what this line holds and takes the new number off the shelf
        # again, in this same transaction. Editing 3 → 3 must therefore still
        # end at 3, which is why the release comes first — the product's
        # balance already has this line's own kits subtracted from it.
        # Fitted under the line's ready units — held and issued alike (rule 6).
        await _reserve(db, line, min(data.from_stock_units, max(0, line.quantity - line.from_finished)), current_user)
    else:
        # Ruling 16, now over both shelves (spec workshop-add-to-order, rule 6): what
        # the line takes never exceeds its quantity. The quantity came down, or the
        # ready units grew, past it: the KITS go back first — the ready units were
        # fitted above. Only ever downwards.
        kits = await part_stock.reserved_units_for_line(db, line)
        # What the shelf already gave the line, assembled and received included, less what was
        # written off (it is needed again — spec workshop-order-issue-followups, rule 47): the
        # same room both «take from stock» doors fill (final review I1).
        fitted = max(0, line.quantity - finished_stock.covered_units(line))
        if kits > fitted:
            await _reserve(db, line, fitted, current_user)
    changes = {name: [before[name], getattr(line, name)] for name in tracked if getattr(line, name) != before[name]}
    stock_after = await part_stock.reserved_units_for_line(db, line)
    if stock_after != stock_before:
        changes["from_stock"] = [stock_before, stock_after]
    finished_after = await finished_stock.held_for_line(db, line.id)
    if finished_after != finished_before:
        changes["from_finished"] = [finished_before, finished_after]
    if changes:
        product = await db.get(Product, line.product_id)
        await order_journal.record(
            db,
            project_id,
            "line_changed",
            {"line_id": line.id, "product": product.name if product else None, "changes": changes},
            actor=current_user,
        )
    return await _response(db, project_id)


@router.put(
    "/{project_id}/lines/{line_id}/configuration",
    response_model=ProjectResponse | LineConfigurationImpact,
)
@part_images.attach
async def configure_line(
    project_id: int,
    line_id: int,
    data: LineConfigurationIn,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.ORDERS_READ),
    creds: RequestCredentials = Depends(request_credentials),
):
    """Change a line's options and counts (spec workshop-product-variants, rules 11–14).

    The reservation follows the new kit in this transaction and the change is
    journaled; with ``dry_run`` nothing is written and the answer is what the
    change would do — the parts that drop out and what of them is already
    printed or queued, and the reservation before and after.

    The preview is a read (``orders:read``; its shelf figures need ``stock:read``); the
    change is an edit, and a line that holds stock moves it to the new kit — ``stock:move``
    too, asked under the line's locks (WS-13 E13 O22).
    """
    if not data.dry_run:
        await ensure(creds, Permission.ORDERS_UPDATE)
    line = await _get_line(db, project_id, line_id)
    # The product's gate before the configuration is written (WS-13 E1 BL3 / BL4): a variant
    # group added meanwhile is then seen, not overwritten. A preview writes nothing and is a
    # reader's — it takes no writer's lock (WS-13 E13 final review).
    if not data.dry_run:
        await product_gate(db, [line.product_id])
        line = await _get_line(db, project_id, line_id, fresh=True)
    # A completed order answers with its own refusal (``line_config``); an active one
    # whose line has moved stock keeps the line's configuration (spec workshop-order-issue, rule 13).
    status = await db.scalar(select(Project.status).where(Project.id == project_id))
    if not data.dry_run and status != "completed" and await order_fulfilment.moved_line_ids(db, [line]):
        raise HTTPException(status_code=409, detail=_LINE_MOVED)
    if not data.dry_run and line.mode == "product" and status != "completed":
        # The positions of the old configuration AND the new one, then the line — before
        # a byte of it is written (WS-13 E1 BL0): the reservation moves between them.
        try:
            key = await line_config.target_key(db, line, choices=data.choices, counts=data.part_counts)
            await finished_stock.lock_line(db, line, also_keys=[key])
        except line_config.LineConfigError as e:
            raise HTTPException(status_code=e.status, detail=str(e)) from e
        except finished_stock.FinishedStockError as e:
            raise HTTPException(status_code=e.status, detail=str(e)) from e
    old_key = line.config_key
    finished_before = await finished_stock.held_for_line(db, line.id)
    if not data.dry_run and (finished_before or await part_stock.reserved_units_for_line(db, line)):
        await ensure(creds, Permission.STOCK_MOVE)
    try:
        outcome = await line_config.set_configuration(
            db, line, choices=data.choices, counts=data.part_counts, actor=current_user, dry_run=data.dry_run
        )
    except line_config.LineConfigError as e:
        raise HTTPException(status_code=e.status, detail=str(e)) from e
    except part_stock.PartStockError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    if data.dry_run:
        finished_after = 0
        if finished_before:
            position = await finished_stock.position_for_key(db, line.product_id, outcome.new_key)
            free = position.on_hand - position.reserved if position is not None else 0
            if outcome.new_key == old_key:
                free += finished_before  # the same position: the line keeps its own
            finished_after = min(finished_before, line.quantity, free)
        # What the new configuration would get depends on the shelf — the stock's to show.
        sees_stock = (await workshop_view()).stock
        return LineConfigurationImpact(
            reserved_before=outcome.reserved_before,
            reserved_after=outcome.reserved_after if sees_stock else None,
            finished_before=finished_before,
            finished_after=finished_after if sees_stock else None,
            dropping=[
                DroppedPartOut(
                    part_id=d.part_id,
                    name=d.name,
                    per_before=d.per_before,
                    per_after=d.per_after,
                    printed=d.printed,
                    queued=d.queued,
                )
                for d in outcome.dropping
            ],
        )
    if line.config_key != old_key and finished_before:
        # The ready units follow the line into the position of its new configuration
        # (spec workshop-add-to-order, rule 8), in this same transaction.
        before, after = await finished_stock.move_for_line(db, line, actor=current_user)
        product = await db.get(Product, line.product_id)
        await order_journal.record(
            db,
            project_id,
            "line_changed",
            {
                "line_id": line.id,
                "product": product.name if product else None,
                "changes": {"from_finished": [before, after]},
            },
            actor=current_user,
        )
    return await _response(db, project_id)


@router.delete("/{project_id}/lines/{line_id}", response_model=ProjectResponse)
@part_images.attach
async def delete_line(
    project_id: int,
    line_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.ORDERS_UPDATE),
):
    line = await _get_line(db, project_id, line_id)
    # The gate of its product first (WS-13 E1 BL3): the cascade below may delete it.
    await product_gate(db, [line.product_id])
    project = await _get_project(db, project_id)
    # Release BEFORE detaching, or the reservation becomes invisible to the
    # query that hands it back and the kits stay off the shelf for good.
    #
    # ⚠️ Unless the order is COMPLETED (Ruling 25): those kits shipped inside
    # its units, and deleting the paperwork afterwards does not bring them back
    # to the shelf. The detach below still runs — the line row goes either way.
    if not _consumed_its_stock(project.status):
        await _release(db, line, part_stock.NOTE_LINE_DELETED, await acting_user(request, db, current_user))
    # The prints stay, and stay in the order: only the line they were filed
    # under goes. Done explicitly because SQLite enforces nothing — this
    # codebase never sets ``PRAGMA foreign_keys = ON``, so the ON DELETE SET
    # NULL these three FKs declare is honoured by PostgreSQL alone. The stock
    # ledger is the fourth table with that FK, and its history survives the
    # line: the parts are on the shelf whatever happened to the paperwork.
    for model in (PrintArchive, PrintQueueItem, AutoQueueItem):
        await db.execute(update(model).where(model.project_line_id == line_id).values(project_line_id=None))
    await part_stock.detach_line(db, line_id)
    await finished_stock.detach_line(db, line_id)
    await line_config.forget_line(db, line_id)
    product = await db.get(Product, line.product_id)
    await order_journal.record(
        db,
        project_id,
        "line_removed",
        {"product": product.name if product else None, "quantity": line.quantity},
        actor=current_user,
    )
    project.lines.remove(line)  # delete-orphan turns this into the DELETE
    await db.flush()
    try:
        await product_delete.delete_orphaned_adhoc_products(db, [line.product_id])
    except product_gate_module.ProductBusy as e:
        raise _product_busy() from e
    return await _response(db, project_id)


# ---------- free stock ----------


@router.post("/{project_id}/bank-surplus", response_model=BankSurplusResponse)
async def bank_surplus(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.ORDERS_UPDATE, Permission.STOCK_MOVE),
):
    """Move this order's overprint onto the product's shelf (pass 8, Decision 2).

    Never automatic, and that is the whole point of the button: a surplus is
    sometimes shipped with the order and sometimes scrapped, and only the
    operator knows which. Pressing it a second time moves only what has
    appeared since — ``surplus`` as ``order_metrics`` computes it (``usable −
    qty_per_unit × quantity`` per counted part, defective already excluded by
    ``row_quantity``, and measured against the FULL quantity rather than the
    reservation-reduced need — Ruling 24) minus what this line has already
    banked. So the ledger holds the line's surplus once however many times the
    button is pressed, and a later print that grows it is still bankable.

    A CANCELLED order banks too: the parts came off a bed regardless of what
    happened to the order afterwards, and they are exactly the ones most worth
    keeping.

    ⚠️ **What moves is ``PartFigures.bankable``** — the surplus this line has
    not banked yet, computed in ``order_metrics`` off the same grouped ledger
    read the order page renders from (Ruling 30). This route used to run its own
    ``SELECT`` for the "already banked" half, which was a second answer to the
    question the BUTTON is enabled on; the button read ``surplus``, which
    banking never lowers, and so stayed lit over an order with nothing left.
    """
    # ⚠️ Lock before the figures are LOADED, not after (finding M5): the
    # "already banked" half is read inside ``load_order_context``, so a lock
    # taken afterwards would leave the read-decide-write of two operators
    # pressing the button at once free to interleave. The order's products are
    # therefore resolved first, in one cheap statement, and every counted part
    # of them is locked in id order.
    product_ids = (
        (await db.execute(select(ProjectLine.product_id).where(ProjectLine.project_id == project_id).distinct()))
        .scalars()
        .all()
    )
    await part_stock.lock_parts(db, await part_stock.counted_parts_of(db, list(product_ids)))

    ctx = await load_order_context(db, project_id)
    if ctx is None:
        raise HTTPException(status_code=404, detail="Project not found")
    figures, _other = attribute(ctx)

    # Per part, not per line: two lines of the same product feed one shelf, and
    # the toast says what landed on it.
    moved: dict[int, StockMovedOut] = {}
    for line in ctx.lines:
        for pf in figures[line.id].parts:
            if pf.bankable <= 0:
                continue
            try:
                movement = await part_stock.move(
                    db,
                    part_id=pf.part_id,
                    delta=pf.bankable,
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
    if moved:
        await order_journal.record(
            db,
            project_id,
            "surplus_banked",
            {"parts": sum(entry.delta for entry in moved.values())},
            actor=current_user,
        )
    # No commit here: ``get_db`` closes the transaction once, after the response
    # is built (finding M6). The response is built from the movement rows above,
    # before any commit could expire them.
    return BankSurplusResponse(moved=list(moved.values()), nothing_to_bank=not moved)


# ---------- procurement ----------


@router.patch("/{project_id}/procurement/{part_id}", response_model=ProjectResponse)
@part_images.attach
async def update_procurement(
    project_id: int,
    part_id: int,
    data: ProcurementUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.ORDERS_UPDATE),
):
    project = await _get_project(db, project_id)
    part = await db.get(ProductPart, part_id)
    if part is None or part.kind != "purchased" or part.product_id not in {ln.product_id for ln in project.lines}:
        raise HTTPException(status_code=404, detail="Purchased part not found in this order")
    row = await db.get(ProjectProcurement, {"project_id": project_id, "product_part_id": part_id})
    acquired_before = row.quantity_acquired if row is not None else 0
    if row is None:
        db.add(
            ProjectProcurement(project_id=project_id, product_part_id=part_id, quantity_acquired=data.quantity_acquired)
        )
    else:
        row.quantity_acquired = data.quantity_acquired
    if data.quantity_acquired != acquired_before:
        await order_journal.record(
            db,
            project_id,
            "procurement_updated",
            {"part": part.name, "from": acquired_before, "to": data.quantity_acquired},
            actor=current_user,
        )
    return await _response(db, project_id)


# ---------- archives & queue ----------


@router.get("/{project_id}/archives")
async def list_project_archives(
    project_id: int,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    user: User | None = RequirePermission(Permission.ORDERS_READ),
    creds: RequestCredentials = Depends(request_credentials),
):
    """List archives in a project.

    A print the caller may not read as an archive (WS-13 E13 O12) is a minimal row —
    what the order page shows of it — marked ``restricted``.

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

    reads_all, reads_own = await _owner_reads(creds, _archives_read_all, _archives_read_own)

    def visible(archive: PrintArchive) -> bool:
        return reads_all or (reads_own and user is not None and archive.created_by_id == user.id)

    out = []
    for archive in archives:
        row = archive_to_response(archive)
        if not visible(archive):
            row = {key: row.get(key) for key in _ARCHIVE_SUMMARY} | {"restricted": True}
        out.append(row)
    return out


# The queue's own update right, asked of the rows ``add-queue`` re-files (WS-13 E13 ORD-26).
_queue_update = require_ownership_permission(Permission.QUEUE_UPDATE_ALL, Permission.QUEUE_UPDATE_OWN)
# The archive and queue sections' own reads, asked of an order's rows (WS-13 E13 O12).
_archives_read_all = require_permission(Permission.ARCHIVES_READ_ALL)
_archives_read_own = require_permission(Permission.ARCHIVES_READ_OWN)
_queue_read_all = require_permission(Permission.QUEUE_READ_ALL)
_queue_read_own = require_permission(Permission.QUEUE_READ_OWN)
# What an order page shows of a print it lists; the rest of the archive is the archive
# section's, for a caller who may read that archive.
_ARCHIVE_SUMMARY = (
    "id",
    "printer_id",
    "project_id",
    "project_line_id",
    "library_file_id",
    "filename",
    "print_name",
    "plate_index",
    "status",
    "quantity",
    "defective_count",
    "created_by_id",
    "created_at",
    "started_at",
    "completed_at",
)
# A queue row's operator, slots and settings are the queue section's.
_QUEUE_PRIVATE = (
    "created_by_id",
    "created_by_username",
    "ams_mapping",
    "nozzle_mapping",
    "nozzle_rack_choice",
    "filament_routing",
    "selected_macro_ids",
    "swap_macro_events",
    "error_message",
)


async def _owner_reads(creds: RequestCredentials, all_gate, own_gate):
    """``(reads every row, reads own rows)`` through the section's own gates."""
    if await creds.allows(all_gate):
        return True, True
    return False, await creds.allows(own_gate)


@router.post("/{project_id}/add-archives")
async def add_archives_to_project(
    project_id: int,
    data: BatchAddArchives,
    db: AsyncSession = Depends(get_db),
    # ``F(print)`` is asked below, per print (WS-13 E13 O21): the gate lets in either half of it.
    current_user: User | None = RequireAnyPermission(Permission.ORDERS_UPDATE, Permission.ORDERS_FILE_PRINTS),
    creds: RequestCredentials = Depends(request_credentials),
):
    """File existing prints under this order, optionally under one of its lines — or move them
    to another line of it. ``F(print)`` for every print, an open order (409 ``order_closed``),
    and C1 for every order a print leaves; all before the first print moves."""
    await _get_project(db, project_id)  # 404s an order that is not there
    archives = [archive for archive_id in data.archive_ids if (archive := await db.get(PrintArchive, archive_id))]
    await ensure_may_file(creds, archives)
    # Every order the prints leave, and this one, locked as receiving locks them before C1 is
    # read; then the prints themselves, ascending, read again behind the locks (WS-13 E13 V04 —
    # orders before prints, the order the archive editor and the trash keep too). A print that
    # moved to an order not locked here meanwhile is the same «changed, try again».
    locked = {project_id, *(archive.project_id for archive in archives)}
    await order_fulfilment.lock_orders(db, locked)
    fresh = {archive.id: archive for archive in await lock_prints(db, [archive.id for archive in archives])}
    archives = [fresh[archive.id] for archive in archives if archive.id in fresh]
    if any(archive.project_id not in locked and archive.project_id is not None for archive in archives):
        raise HTTPException(status_code=409, detail=order_fulfilment.ORDER_CHANGED)
    # A print in the trash, as it stands under its own lock, refuses the whole batch before
    # any print, shelf or journal row moves (WS-13 E13 V08) — never filed, never skipped.
    if any(archive.deleted_at is not None for archive in archives):
        raise HTTPException(status_code=409, detail=order_filing.PRINT_IN_TRASH)
    await order_filing.resolve_link(db, project_id, data.project_line_id)
    # A print taken from another order leaves it by that order's rules — the same
    # judge as the two other exits (WS-13 E13 B05), asked of every order a print
    # leaves before the first print moves, so a refusal moves nothing.
    leaving: dict[int, list[int]] = {}
    for archive in archives:
        if archive.project_id is not None and archive.project_id != project_id:
            leaving.setdefault(archive.project_id, []).append(archive.id)
    for old_project, ids in leaving.items():
        try:
            await order_fulfilment.ensure_prints_can_leave(db, old_project, ids)
        except order_fulfilment.FulfilmentError as e:
            raise HTTPException(status_code=e.status, detail=str(e)) from e
    updated = 0
    # The journal (spec workshop-order-stage, rule 17): filed here, and taken
    # out of whichever order held them before.
    filed: list[int] = []
    relined: list[int] = []
    left: dict[int, list[int]] = {}
    for archive in archives:
        # Same rule as the archive editor's project change (pass 8,
        # Decision 3): a print that was free stock stops being free stock
        # the moment an order counts it. Read before the assignment, which
        # is where ``project_id`` stops being what it was.
        was_unfiled = archive.project_id is None
        if archive.project_id != project_id:
            filed.append(archive.id)
            if archive.project_id is not None:
                left.setdefault(archive.project_id, []).append(archive.id)
        elif archive.project_line_id != data.project_line_id:
            # Another line of the same order: the order's coverage moves between its lines.
            relined.append(archive.id)
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
    for old_project, ids in left.items():
        await order_journal.record(
            db, old_project, "prints_unfiled", {"count": len(ids), "archive_ids": ids}, actor=current_user
        )
    if filed:
        await order_journal.record(
            db, project_id, "prints_filed", {"count": len(filed), "archive_ids": filed}, actor=current_user
        )
    if relined:
        await order_journal.record(
            db,
            project_id,
            "prints_relined",
            await _relined_payload(db, relined, data.project_line_id),
            actor=current_user,
        )
    return {"message": f"Added {updated} archives to project"}


async def _relined_payload(db: AsyncSession, archive_ids: list[int], line_id: int | None) -> dict:
    """``prints_relined``: which prints, and the line they went to with its product's name as a
    snapshot (``None`` — the order's other prints, or a product since deleted)."""
    line = await db.get(ProjectLine, line_id) if line_id is not None else None
    product = await db.get(Product, line.product_id) if line is not None and line.product_id is not None else None
    return {
        "count": len(archive_ids),
        "archive_ids": archive_ids,
        "line_id": line_id,
        "product": product.name if product is not None else None,
    }


@router.post("/{project_id}/remove-archives")
async def remove_archives_from_project(
    project_id: int,
    data: BatchAddArchives,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequireAnyPermission(Permission.ORDERS_UPDATE, Permission.ORDERS_FILE_PRINTS),
    creds: RequestCredentials = Depends(request_credentials),
):
    """Unfile prints from this order — the line goes with the order, never alone. ``F(print)``
    and C1; a closed order still lets a print go (it is not a new link)."""
    # The order locked as receiving locks it, then its prints, ascending, before C1 is read
    # (WS-13 E13 V04 — orders before prints, as every exit keeps).
    await order_fulfilment.lock_orders(db, [project_id])
    in_order = {archive.id: archive for archive in await lock_prints(db, data.archive_ids, filed_under=project_id)}
    await ensure_may_file(creds, list(in_order.values()))
    try:
        await order_fulfilment.ensure_prints_can_leave(db, project_id, data.archive_ids)
    except order_fulfilment.FulfilmentError as e:
        raise HTTPException(status_code=e.status, detail=str(e)) from e
    updated = 0
    removed: list[int] = []
    for archive_id in dict.fromkeys(data.archive_ids):
        archive = in_order.get(archive_id)
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
            removed.append(archive.id)
            updated += 1
    if removed:
        await order_journal.record(
            db, project_id, "prints_unfiled", {"count": len(removed), "archive_ids": removed}, actor=current_user
        )
    return {"message": f"Removed {updated} archives from project"}


async def _order_print(db: AsyncSession, project_id: int, archive_id: int) -> PrintArchive:
    """One of this order's prints — filed under it, not trashed — or 404."""
    archive = (
        await db.execute(
            PrintArchive.active().where(PrintArchive.id == archive_id, PrintArchive.project_id == project_id)
        )
    ).scalar_one_or_none()
    if archive is None:
        raise HTTPException(status_code=404, detail="Print not found in this order")
    return archive


def _print_defects_out(archive: PrintArchive, rows: list[PrintArchivePart]) -> OrderPrintDefectsOut:
    return OrderPrintDefectsOut(
        archive_id=archive.id,
        quantity=int(archive.quantity or 0),
        defective_count=int(archive.defective_count or 0),
        parts=[ArchivePartRow.from_row(r) for r in rows],
    )


@router.get("/{project_id}/archives/{archive_id}/parts", response_model=OrderPrintDefectsOut)
async def get_order_print_parts(
    project_id: int,
    archive_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ORDERS_READ),
):
    """The print's part rows for the order page's defects dialog.

    The archives LIST never carries rows (no N+1 per page), and the order page
    reads under the order's permission, not the archive's — so the rows come
    from here rather than from ``GET /archives/{id}``.
    """
    archive = await _order_print(db, project_id, archive_id)
    return _print_defects_out(archive, await archive_parts.load_rows(db, archive.id))


@router.post("/{project_id}/archives/{archive_id}/defects", response_model=OrderPrintDefectsOut)
async def record_order_print_defects(
    project_id: int,
    archive_id: int,
    data: OrderPrintDefectsIn,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.ORDERS_UPDATE),
):
    """Record what came out bad on one of this order's prints (spec 2026-09-11 §4).

    Under ``orders:update`` and scoped to a print FILED under this order: the
    defects change the order's figures, so the order's permission is the right
    one, and an operator who may edit the order need not hold
    ``archives:update_all`` for a print somebody else started. The writer is the
    same one the archive editor uses (``services/archive_defects``).
    """
    # Enter before _order_print's authoritative read.  This is the third
    # public writer of archive defect facts, alongside the archive editor and
    # the completion actions.
    async with archive_write_scope(db, archive_id):
        archive = await _order_print(db, project_id, archive_id)
        result = await record_defects(
            db,
            archive,
            DefectsWrite(parts=tuple((p.id, p.defective) for p in data.parts or ()), flat=data.defective_count),
            actor_id=current_user.id if current_user else None,
        )
    # No ledger-refusal report here, and none is possible: a print reachable
    # through this route is FILED under an order (``_order_print`` requires
    # ``project_id == project_id``), and ``adjust_unfiled_print`` returns an
    # empty result on exactly that condition. The refusal is reported where it
    # can happen — the two plate answers and the Telegram prompt.
    return _print_defects_out(archive, result.parts)


@router.post("/{project_id}/add-queue")
async def add_queue_items_to_project(
    project_id: int,
    data: BatchAddQueueItems,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequireAnyPermission(*_FILES_FUTURE),
    creds: RequestCredentials = Depends(request_credentials),
):
    """Batch add queue items to a project.

    A line only ever travels with the order it belongs to — the same rule
    ``add-archives``, ``remove-archives`` and ``archives.update_archive``
    follow. Re-filing an item under another order therefore drops the line it
    carries: keeping it would credit this order's work to a line of the old one.

    The work is not printed yet, so it is the Workshop's filing right for future
    work (``Fф``) beside the queue's own update right — any row with
    ``queue:update_all``, the caller's own with ``update_own`` (WS-13 E13 ORD-26).
    Only pending work moves (409), into an open order; every order a row leaves is
    journaled. Everything is asked before the first row moves.
    """
    await _get_project(db, project_id)
    await order_filing.resolve_link(db, project_id, None)
    items = [item for item_id in dict.fromkeys(data.queue_item_ids) if (item := await db.get(PrintQueueItem, item_id))]
    user, can_update_all = await creds.check(_queue_update)
    if not can_update_all and any(user is None or item.created_by_id != user.id for item in items):
        raise HTTPException(status_code=403, detail="You can only update your own queue items")
    if any(item.status != "pending" for item in items):
        raise HTTPException(status_code=409, detail="Only pending queue jobs can be filed under an order")

    updated = 0
    filed = 0
    left: dict[int, int] = {}
    for item in items:
        if item.project_line_id is not None:
            stale = await db.get(ProjectLine, item.project_line_id)
            if stale is None or stale.project_id != project_id:
                item.project_line_id = None
        # Journaled only when it actually moves here — as ``add-archives`` does.
        if item.project_id != project_id:
            filed += 1
            if item.project_id is not None:
                left[item.project_id] = left.get(item.project_id, 0) + 1
        item.project_id = project_id
        updated += 1

    for old_project, count in left.items():
        await order_journal.record(db, old_project, "queue_items_unfiled", {"count": count}, actor=current_user)
    if filed:
        await order_journal.record(db, project_id, "queue_items_filed", {"count": filed}, actor=current_user)
    return {"message": f"Added {updated} queue items to project"}


# ---------- attachments ----------


def get_project_attachments_dir(project_id: int) -> Path:
    """``<DATA_DIR>/projects/<id>/attachments`` - its own root since m177, not a child of the archive."""
    return Path(settings.projects_dir) / str(project_id) / "attachments"


@router.post("/{project_id}/attachments")
async def upload_attachment(
    project_id: int,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.ORDERS_UPDATE),
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
    # Before this route's own commit below, or the journal line would be lost with the session.
    await order_journal.record(db, project_id, "attachment_added", {"filename": original_name}, actor=current_user)

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
    _: User | None = RequirePermission(Permission.ORDERS_READ),
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
    current_user: User | None = RequirePermission(Permission.ORDERS_UPDATE),
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
    await order_journal.record(
        db,
        project_id,
        "attachment_removed",
        {"filename": attachment.get("original_name") or filename},
        actor=current_user,
    )

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
    current_user: User | None = RequirePermission(Permission.ORDERS_UPDATE),
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
    await order_journal.record(db, project_id, "cover_changed", {"action": "set"}, actor=current_user)
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
    _=Depends(require_media_permission(Permission.ORDERS_READ)),
):
    """Stream the project's cover image (#1155).

    Browsers can't attach ``Authorization: Bearer ...`` to ``<img src>``
    requests, so this route takes a media token in ``?token=`` (or the
    ordinary headers) under ``orders:read`` — audit D9 a2; it used to take
    the camera stream token, which cost ``camera:view``. The frontend wraps
    URLs via ``withMediaToken``.
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
        return json_error(404, "Cover image file not found")

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
    current_user: User | None = RequirePermission(Permission.ORDERS_UPDATE),
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
        await order_journal.record(db, project_id, "cover_changed", {"action": "removed"}, actor=current_user)
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


def _queue_display_name(item, name_visible) -> str:
    """Best-effort name for a queue row, which has no name of its own.

    Works for both queue tables: each references an archive and/or a library
    file, and neither carries a ``print_name`` column — reading one off the row
    itself is what used to 500 this endpoint. A library file's name only when
    ``name_visible`` says the library would show it (WS-13 E13 O12, as ``/plan``).
    """
    library_name = item.library_file.filename if item.library_file and name_visible(item.library_file) else None
    return (
        (item.archive.print_name if item.archive else None)
        or (item.archive.filename if item.archive else None)
        or library_name
        or "(unnamed queue item)"
    )


@router.get("/{project_id}/timeline", response_model=list[TimelineEvent])
async def get_project_timeline(
    project_id: int,
    request: Request,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    user: User | None = RequirePermission(Permission.ORDERS_READ),
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
                .where(PrintQueueItem.project_id == project_id, *queued_printer_row_conditions())
                .order_by(PrintQueueItem.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    name_scope = await library_name_scope(request, db, user)

    def name_visible(f) -> bool:
        return file_name_visible(f, user, name_scope)

    for item in queued_items:
        if item.archive_id and item.archive_id in archive_ids:
            continue
        events.append(
            TimelineEvent(
                event_type="queued",
                timestamp=item.created_at,
                title=_EVENT_TITLES["queued"],
                description=_queue_display_name(item, name_visible),
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
                .where(AutoQueueItem.project_id == project_id, *awaiting_auto_row_conditions())
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
                description=_queue_display_name(item, name_visible),
                metadata={"auto_queue_item_id": item.id, "target_model": item.target_model},
            )
        )

    # The order journal (spec workshop-order-stage, rule 20): its newest ``limit``
    # merged like the other sources. Its ``kind`` is the event type the frontend
    # translates; the payload and the author ride in ``metadata``.
    journal = (
        (
            await db.execute(
                select(ProjectEvent)
                .where(ProjectEvent.project_id == project_id)
                .order_by(ProjectEvent.created_at.desc(), ProjectEvent.id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    for entry in journal:
        events.append(
            TimelineEvent(
                event_type=entry.kind,
                timestamp=entry.created_at,
                title=order_journal.TITLES.get(entry.kind, entry.kind),
                metadata={**(entry.payload or {}), "user_id": entry.user_id, "user_name": entry.user_name},
            )
        )

    # An order from before the journal has no «created» line of its own.
    created_logged = await db.scalar(
        select(ProjectEvent.id)
        .where(ProjectEvent.project_id == project_id, ProjectEvent.kind == "order_created")
        .limit(1)
    )
    if created_logged is None:
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


#: ``projects.name`` is ``String(255)`` — the generated copy name must fit it too.
_NAME_MAX = 255


def _fit_name(base: str, suffix: str) -> str:
    """``base`` + ``suffix`` within the column: the base gives way, the suffix never."""
    return f"{base[: _NAME_MAX - len(suffix)].rstrip()}{suffix}"


def _duplicate_name(base: str, taken: set[str]) -> str:
    """``"X" -> "X (Copy)"``, then ``"X (Copy 2)"`` and so on.

    Project names carry no unique constraint, so this is politeness rather
    than correctness — three rows all called "Voron (Copy)" are legal and
    unusable.

    WS-13 E6 G02 (R05): the suffix is added AFTER the request was validated, so
    it reserves its own room — the collision number included — and the base is
    cut to fit; a 255-character original still copies.
    """
    candidate = _fit_name(base, " (Copy)")
    if candidate not in taken:
        return candidate
    n = 2
    while _fit_name(base, f" (Copy {n})") in taken:
        n += 1
    return _fit_name(base, f" (Copy {n})")


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
@part_images.attach
async def duplicate_project(
    project_id: int,
    data: ProjectDuplicate = Body(default_factory=ProjectDuplicate),
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.ORDERS_CREATE, Permission.ORDERS_READ),
):
    """A reorder: lines, customer, notes, attachments come across; history never does; status is active."""
    source = await _get_project(db, project_id)
    gated = {line.product_id for line in source.lines}
    # The gates of every product of the source before the copy is inserted (WS-13 E1
    # BL3): the copied configurations are read behind them — and so are the lines (BL2).
    await product_gate(db, sorted(gated))
    source = await _get_project(db, project_id, fresh=True)
    if not {line.product_id for line in source.lines} <= gated:
        raise HTTPException(status_code=409, detail=order_fulfilment.ORDER_CHANGED)
    taken = set((await db.execute(select(Project.name))).scalars().all())
    copy = Project(
        name=data.name or _duplicate_name(source.name, taken),
        customer_id=source.customer_id,
        contact_id=source.contact_id,
        description=source.description,
        color=source.color,
        status="active",
        notes=source.notes,
        tags=source.tags,
        due_date=source.due_date,
        priority=source.priority,
        price=source.price,
        url=source.url,
        # A reorder keeps who runs it; the stage starts over (column default).
        responsible_id=source.responsible_id,
    )
    pairs: list[tuple[ProjectLine, ProjectLine]] = []
    for line in source.lines:
        new_line = ProjectLine(
            product_id=line.product_id,
            quantity=line.quantity,
            material=line.material,
            color=line.color,
            note=line.note,
            sort_order=line.sort_order,
            mode=line.mode,
        )
        copy.lines.append(new_line)
        pairs.append((line, new_line))
    db.add(copy)
    await db.flush()
    # Mode, choices and counts travel with each line, through the one writer.
    for line, new_line in pairs:
        await line_config.copy_configuration(db, line, new_line)
    # The copy's journal starts with where it came from; the source's own history stays with the source.
    await order_journal.record(
        db, copy.id, "order_created", {"source": "copy", "from_code": code_for("order", source.id)}, actor=current_user
    )
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


async def _plan_file_names(db: AsyncSession, file_ids: set[int], user: User | None, scope: str) -> dict[int, str]:
    """``library_file_id → name`` for the plan's files this caller may see named
    (WS-13 E1 LV5) — ONE batched read over every row's and alternative's file.

    The name comes from this read alone, never from the plan's own copy of the
    row: a file trashed or deleted since the engine read it is absent here, and
    absent means hidden. No ids, or a scope that shows no file at all, reads
    nothing. The engine never asks: the plan is the same for every reader.
    """
    if not file_ids or scope == "none":
        return {}
    rows = await db.execute(
        select(LibraryFile.id, LibraryFile.filename, LibraryFile.deleted_at, LibraryFile.created_by_id).where(
            LibraryFile.id.in_(file_ids)
        )
    )
    # ``file_name_visible`` reads ``deleted_at`` and ``created_by_id`` off whatever
    # it is handed — the four columns, not the whole row with its metadata.
    return {row.id: row.filename for row in rows if file_name_visible(row, user, scope)}


def _plan_file_ids(plan: OrderPlan) -> set[int]:
    return {
        file_id
        for line in plan.lines
        for row in line.rows
        for file_id in (row.library_file_id, *(alt.library_file_id for alt in row.alternatives))
    }


def _plan_response(plan: OrderPlan, pending_auto: dict[int, int], file_names: dict[int, str]) -> OrderPlanResponse:
    """Name every id the engine returned — no SELECT, no walk.

    The engine builds the plan from an ``OrderContext`` that already holds every
    product and part of the order, so it hands the two name maps out beside the
    rows. Re-reading ``products`` and ``product_parts`` here was two queries for
    rows the request had just had in memory. File names are the one exception,
    read by the caller (``_plan_file_names``): which of them a reader may see is
    the library's rule, not the engine's.
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
                        filename=file_names.get(row.library_file_id),
                        hidden=row.library_file_id not in file_names,
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
                                filename=file_names.get(alt.library_file_id),
                                hidden=alt.library_file_id not in file_names,
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
                pending_auto_prints=pending_auto.get(line.line_id, 0),
            )
            for line in plan.lines
        ],
        totals=PlanTotalsOut(
            prints=plan.totals.prints,
            print_time_seconds=plan.totals.print_time_seconds,
            filament_used_grams=plan.totals.filament_used_grams,
            cost=plan.totals.cost,
            rows=sum(len(line.rows) for line in plan.lines),
        ),
        truncated=plan.truncated,
    )


async def _pending_auto_prints(db: AsyncSession, line_ids: list[int]) -> dict[int, int]:
    """``line_id → pending, unassigned auto-queue rows`` — one grouped query for the whole plan."""
    if not line_ids:
        return {}
    rows = (
        await db.execute(
            select(AutoQueueItem.project_line_id, func.count())
            .where(AutoQueueItem.project_line_id.in_(line_ids), *awaiting_auto_row_conditions())
            .group_by(AutoQueueItem.project_line_id)
        )
    ).all()
    return {line_id: int(n) for line_id, n in rows}


@router.get("/{project_id}/plan", response_model=OrderPlanResponse)
@part_images.attach
async def get_order_plan(
    project_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User | None = RequirePermission(Permission.ORDERS_READ),
):
    """What to print next for every line of this order (spec pass 3).

    Computed on every read, never cached and never stored: a second call after
    enqueuing sees the new queue rows and plans that much less.  Recipe choice
    accounts for a read-only snapshot of usable farm capacity; it neither
    claims a printer nor changes dispatch readiness. A file the library would not
    show the caller keeps its plate in the plan without its name (WS-13 E1 LV5).
    """
    plan = await plan_for_order(db, project_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Project not found")
    scope = await library_name_scope(request, db, user)
    return _plan_response(
        plan,
        await _pending_auto_prints(db, [line.line_id for line in plan.lines]),
        await _plan_file_names(db, _plan_file_ids(plan), user, scope),
    )


@router.get("/{project_id}/queue", response_model=OrderQueueOut)
async def get_order_queue(
    project_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User | None = RequirePermission(Permission.ORDERS_READ),
    creds: RequestCredentials = Depends(request_credentials),
):
    """The order's live work in both queue tiers (spec workshop-order-queue): the
    archives printing now, the printer-queue rows waiting, and the auto-queue rows
    the distributor has not handed out — through the SAME conditions the order's
    tiles count (``services/order_queue``). Rows are built by the queue page's own
    builders. A key limited to some printers sees its printers only and no
    auto-queue, which is closed to it (as ``/auto-queue`` is)."""
    if await db.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="Project not found")
    scope = key_printer_scope(request)
    printing_q = (
        select(PrintArchive, Printer.name)
        .outerjoin(Printer, Printer.id == PrintArchive.printer_id)
        .where(PrintArchive.project_id == project_id, PrintArchive.status == RUNNING_STATUS, *live_archive_conditions())
        .order_by(PrintArchive.created_at, PrintArchive.id)
    )
    pending_q = (
        select(PrintQueueItem)
        .options(*queue_item_load_options())
        .where(PrintQueueItem.project_id == project_id, *queued_printer_row_conditions())
        .order_by(PrintQueueItem.queue_id, PrintQueueItem.position, PrintQueueItem.id)
    )
    if scope is not None:
        printing_q = printing_q.where(PrintArchive.printer_id.in_(scope))
        pending_q = pending_q.where(PrintQueueItem.queue_id.in_(scope))
    printing = [
        OrderQueuePrinting(
            archive_id=archive.id,
            printer_id=archive.printer_id,
            printer_name=printer_name,
            name=archive.print_name or archive.filename,
            project_line_id=archive.project_line_id,
        )
        for archive, printer_name in (await db.execute(printing_q)).all()
    ]
    # A row the caller may not read as a queue row loses its operator, slots and settings;
    # a library file's name shows only to whom the library shows it (WS-13 E13 O12).
    reads_all, reads_own = await _owner_reads(creds, _queue_read_all, _queue_read_own)
    name_scope = await library_name_scope(request, db, user)

    def projected(item, row):
        update = {}
        if getattr(row, "library_file_name", None) and not file_name_visible(item.library_file, user, name_scope):
            update["library_file_name"] = None
        if not (reads_all or (reads_own and user is not None and item.created_by_id == user.id)):
            update.update(dict.fromkeys((f for f in _QUEUE_PRIVATE if f in type(row).model_fields), None))
        return row.model_copy(update=update) if update else row

    pending = [projected(item, queue_row_response(item)) for item in (await db.execute(pending_q)).scalars().all()]
    awaiting = []
    if scope is None:
        awaiting_q = (
            select(AutoQueueItem)
            .options(*auto_queue_item_load_options())
            .where(AutoQueueItem.project_id == project_id, *awaiting_auto_row_conditions())
            .order_by(AutoQueueItem.position, AutoQueueItem.id)
        )
        awaiting = [
            projected(item, auto_queue_row_response(item)) for item in (await db.execute(awaiting_q)).scalars().all()
        ]
    return OrderQueueOut(printing=printing, pending=pending, awaiting=awaiting)


@router.get("/{project_id}/forecast", response_model=OrderForecastDetailOut)
async def get_order_forecast(
    project_id: int, db: AsyncSession = Depends(get_db), _: User | None = RequirePermission(Permission.ORDERS_READ)
):
    """One order's «ready by», with its lines and the farm's proposed split per row."""
    await _get_project(db, project_id)
    now = _utc_now()
    _farm, orders = await farm_forecast.forecast_projects(db, [project_id], now)
    f = orders[project_id]
    return OrderForecastDetailOut(
        **_order_forecast_fields(f),
        by_model=[ModelHoursOut(**row) for row in f.by_model],
        lines=[
            LineForecastOut(
                line_id=line.line_id,
                now_eta=line.now_eta,
                now_seconds=line.now_seconds,
                after_eta=line.after_eta,
                after_seconds=line.after_seconds,
                unknown_prints=line.unknown_prints,
                unroutable_prints=line.unroutable_prints,
                eta_complete=line.eta_complete,
                rows=[RowForecastOut(plate_id=r.plate_id, proposed_split=r.proposed_split) for r in line.rows],
            )
            for line in f.lines
        ],
    )


@router.get("/{project_id}/filament", response_model=OrderNeedsOut)
async def get_order_filament(
    project_id: int, db: AsyncSession = Depends(get_db), _: User | None = RequirePermission(Permission.ORDERS_READ)
):
    await _get_project(db, project_id)
    needs = (await filament_needs.needs_of_orders(db, [project_id]))[project_id]
    return OrderNeedsOut(
        project_id=project_id,
        rows=[NeedRowOut(**_need_row_fields(r)) for r in needs.rows],
        unknown_prints=needs.unknown_prints,
        stock_unavailable=needs.stock_unavailable,
        assumptions=list(filament_needs.ASSUMPTIONS),
    )


class _ResolvedPlate(NamedTuple):
    """One validated item of an enqueue request, as plain scalars.

    Read out of the loaded rows BEFORE the first writer commits — a commit may
    expire every instance above it — which is why nothing here is an ORM object.
    """

    line_id: int
    plate_id: int
    library_file_id: int
    #: The resolved slicer plate; the recipe can still name the whole file.
    plate_number: int
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
    current_user: User | None = RequireAnyPermission(*_FILES_FUTURE),
    _queue: User | None = RequirePermission(Permission.QUEUE_CREATE),
):
    """Send plan rows to the auto-queue, or to one printer's queue.

    Both queue doors (``POST /queue/``, ``POST /auto-queue/``) require
    ``queue:create``; this one also files the work under the order, so it asks
    the Workshop's filing right too (``Fф``: ``orders:update`` or
    ``orders:file_prints``, WS-13 E13 ORD-33), and the order must be open.

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
    await order_filing.resolve_link(db, project_id, None)

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
    # Keep the already-loaded source row for strict plate validation and its
    # baked swap-macro flag; source validation must finish before any writer.
    plates_by_product: dict[int, dict[int, tuple[ProductPlate, LibraryFile, PlateRecipe]]] = {
        product_id: {plate.id: (plate, file, recipe) for plate, file, recipe in rows}
        for product_id, rows in recipes_by_product.items()
    }
    # Plain scalars, read BEFORE the first writer commits, because a commit may
    # expire every instance loaded above it.
    resolved: list[_ResolvedPlate] = []
    requirements_cache = PrintRequirementsCache()
    for item in data.items:
        line = lines_by_id[item.line_id]
        plates = plates_by_product.get(line.product_id, {})
        entry = plates.get(item.plate_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="Plate not found in this line's product")
        plate, source_file, recipe = entry
        if not recipe.sliced:
            raise HTTPException(status_code=404, detail="Plate is not sliced")
        requirements = await require_source_requirements(
            requirements_cache,
            library_file=source_file,
            plate_id=plate.plate_index,
            product_plate_id=plate.id,
        )
        resolved.append(
            _ResolvedPlate(
                line_id=line.id,
                plate_id=plate.id,
                library_file_id=plate.library_file_id,
                # Recipe index zero remains unchanged; only the job is resolved.
                plate_number=requirements.resolved_plate_id,
                count=item.count,
                baked_swap_macros=bool(source_file.swap_compatible),
                sliced_for_model=requirements.model or recipe.printer_model,
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
                    requirements_cache=requirements_cache,
                )
            else:
                rows, _batch_id = await enqueue_batch_copies(
                    db,
                    printer_id=printer_id,
                    count=plate.count,
                    requirements_cache=requirements_cache,
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
    prints = sum(len(entry.queue_item_ids) for entry in created)
    if prints:
        await order_journal.record(db, project_id, "plan_enqueued", {"prints": prints}, actor=current_user)
    return PlanEnqueueResponse(created=created)


@router.post("/{project_id}/lines/{line_id}/rebalance", response_model=RebalanceOut)
async def rebalance_order_line(
    project_id: int,
    line_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequireAnyPermission(*_FILES_FUTURE),
    _queue: User | None = RequirePermission(Permission.QUEUE_UPDATE_ALL),
):
    """Move this line's still-pending auto-queue prints to idle printers of another
    model where that finishes sooner (spec 2026-09-10) — the setting and the
    cooldown do not apply to a button.

    ``queue:update_all`` beside the Workshop's filing right (``Fф``, WS-13 E13
    ORD-34): this rewrites router rows whoever queued them, and files the extra
    prints under the line. The order must be open. The handler does not commit — ``get_db`` does — but the
    writer that creates the extra prints commits per call, exactly as the plan's
    enqueue door does.
    """
    project = await _get_project(db, project_id)
    if line_id not in {line.id for line in project.lines}:
        raise HTTPException(status_code=404, detail="Order line not found in this project")
    await order_filing.resolve_link(db, project_id, line_id)
    # Read before the writer, which commits per call and expires what is loaded.
    line_product_id = next(ln.product_id for ln in project.lines if ln.id == line_id)
    result = await queue_rebalance.rebalance(db, line_ids=[line_id], force=True, current_user=current_user)
    if result.converted or result.created:
        product = await db.get(Product, line_product_id)
        await order_journal.record(
            db,
            project_id,
            "line_rebalanced",
            {"line_id": line_id, "product": product.name if product else None},
            actor=current_user,
        )
    return result.as_response()
