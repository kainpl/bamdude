"""The Stock tab: the farm-wide shelf and its journal, read-only.

Everything here reads the ledger through ``services/part_stock.py``'s batch
readers — one grouped query per question for the whole page, the pass-6
discipline — and nothing here writes (``inv-stock-ledger-single-writer``).
"""

from dataclasses import asdict
from typing import Literal, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.api.routes._workshop_rights import bind_workshop_credentials, ensure
from backend.app.core.auth import (
    RequestCredentials,
    RequireAnyPermission,
    RequirePermission,
    acting_user,
    request_credentials,
)
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.customer import Customer
from backend.app.models.finished_stock import StockItem, StockItemChoice
from backend.app.models.product import Product, ProductOrigin, ProductPart
from backend.app.models.product_variant import ProductVariantGroup, ProductVariantOption
from backend.app.models.project import Project
from backend.app.models.project_line import ProjectLine
from backend.app.models.user import User
from backend.app.schemas.finished_stock import (
    StockAssembleIn,
    StockItemDetail,
    StockItemOut,
    StockItemParamsIn,
    StockItemsPage,
    StockItemsSummary,
    StockJournalPage,
    StockJournalProduct,
    StockLookupOut,
    StockMoveIn,
    StockMoveOut,
    StockSuggestIn,
    StockSuggestLineOut,
    StockSuggestOut,
)
from backend.app.schemas.listing import StockFigures, StockListPage
from backend.app.schemas.product import StockBalanceOut
from backend.app.schemas.stock import (
    StockCatalogGroup,
    StockCatalogOption,
    StockCatalogProduct,
    StockListItem,
    StockMovementRowOut,
    StockMovementsPageOut,
    StockProductOut,
    StockReason,
    StockReservationOut,
    StockSummaryOut,
)
from backend.app.services import (
    finished_stock,
    finished_stock_views,
    line_config,
    part_images,
    part_stock,
    stock_issues,
    stock_journal,
    stock_pick,
    stock_views,
)
from backend.app.services.configuration_views import configuration_out, groups_by_product
from backend.app.services.entity_codes import code_for, id_from_query
from backend.app.services.line_composition import (
    LineConfig,
    composition,
    default_options,
    line_composition,
    load_line_configs,
    per_by_line,
    standard_composition,
    standard_per,
)
from backend.app.services.list_paging import (
    SortSpec,
    apply_sql_sort,
    like_contains,
    page_meta,
    resolve_sort,
    slice_page,
    sort_computed,
)
from backend.app.services.product_files import effective_cover
from backend.app.services.product_gate import product_gate
from backend.app.services.stock_views import movement_out, orders_of_lines

router = APIRouter(prefix="/stock", tags=["stock"], dependencies=[Depends(bind_workshop_credentials)])

# All the keys are computed: the ``with_stock`` filter needs the balances, so
# the set is built in Python whole before it can be sorted or cut (spec
# workshop-lists, rule 9). ``kits-desc`` is the flat answer's own order. ``shelf``
# (WS-13 E1 ST4) reads ``parts_on_shelf``, already in hand before the cut.
_STOCK_SORT = SortSpec(sql={}, computed={"kits", "name", "reserved", "shelf"}, default="kits-desc")
_STOCK_KEYS = {
    "kits": lambda r: r.kits_available,
    "name": lambda r: r.name.lower(),
    "reserved": lambda r: r.reserved_kits,
    "shelf": lambda r: r.parts_on_shelf,
}


def _stock_word(word: str):
    """One search word against a shelf row: the product's name, its SKU or its code —
    ``%`` and ``_`` taken literally (WS-13 E1 ST4)."""
    needle = like_contains(word)
    fields = [Product.name.ilike(needle, escape="\\"), Product.sku.ilike(needle, escape="\\")]
    if (product_id := id_from_query("product", word)) is not None:
        fields.append(Product.id == product_id)
    return or_(*fields)


async def _stock_rows(
    db: AsyncSession, *, q: str | None, with_stock: bool, loaded: dict | None = None
) -> list[StockProductOut]:
    """Every product with a shelf: its kits, its counted parts' balances, and
    the ACTIVE orders' lines holding its kits in reserve — kits descending,
    then name. The flat answer's rows, and the source of the paged list and
    the tiles alike, so a tile and the table cannot disagree.

    ``with_stock`` keeps a product only while its shelf holds anything — a
    counted part above zero, or a live reservation: kits out on loan are still
    the shelf's business, and a product whose whole stock is reserved reads as
    zero balances with a reservation, never as absent.

    The product select is pre-filtered in SQL to those with at least one
    counted printed part — the EXISTS keeps part-less one-off products (adhoc
    plate products are created with no parts) out of the load.
    """
    stmt = (
        select(Product).options(selectinload(Product.parts)).where(Product.parts.any(part_stock.counted_part_clause()))
    )
    for word in (q or "").split():
        stmt = stmt.where(_stock_word(word))
    products = [p for p in (await db.execute(stmt)).scalars().all() if any(part_stock.is_counted(pt) for pt in p.parts)]
    if not products:
        return []

    ids = [p.id for p in products]
    balances = await part_stock.balances_for_products(db, ids)
    if loaded is not None:
        # What the page's enrichment needs after the cut, without reading it again.
        loaded.update(products={p.id: p for p in products}, balances=balances)

    # Reservations: the lines of ACTIVE orders on these products, then ONE
    # ledger read for all of them — the same read the order pages use.
    line_rows = (
        await db.execute(
            select(ProjectLine.id, ProjectLine.product_id, Project.id, Project.name)
            .join(Project, Project.id == ProjectLine.project_id)
            .where(ProjectLine.product_id.in_(ids), Project.status == "active")
        )
    ).all()
    # Each line's reservation is read through ITS composition (spec
    # workshop-product-variants): a line holding angled tails holds no straight.
    defaults = await default_options(db, ids)
    parts_of = {p.id: list(p.parts) for p in products}
    line_objs = (
        (await db.execute(select(ProjectLine).where(ProjectLine.id.in_([row[0] for row in line_rows])))).scalars().all()
        if line_rows
        else []
    )
    configs = await load_line_configs(db, [line.id for line in line_objs])
    compositions = {
        line.id: line_composition(
            parts_of.get(line.product_id, []), line.mode, configs[line.id], defaults.get(line.product_id, {})
        )
        for line in line_objs
    }
    reads = await part_stock.line_ledger_reads(db, [row[0] for row in line_rows], per_by_line(compositions))
    reservations: dict[int, list[StockReservationOut]] = {}
    for line_id, product_id, order_id, order_name in line_rows:
        kits = reads.reserved_units.get(line_id, 0)
        if kits > 0:
            reservations.setdefault(product_id, []).append(
                StockReservationOut(
                    line_id=line_id,
                    order_id=order_id,
                    order_code=code_for("order", order_id),
                    order_name=order_name,
                    kits=kits,
                )
            )

    out: list[StockProductOut] = []
    for p in products:
        part_balances = balances.get(p.id, {})
        held = sorted(reservations.get(p.id, []), key=lambda r: (-r.kits, r.order_name.lower()))
        if with_stock and not any(v > 0 for v in part_balances.values()) and not held:
            continue
        out.append(
            StockProductOut(
                id=p.id,
                name=p.name,
                is_active=p.is_active,
                origin=p.origin,
                kits_available=part_stock.kits_of(
                    part_balances, standard_composition(list(p.parts), set(defaults.get(p.id, {}).values()))
                ),
                parts=[
                    StockBalanceOut(
                        part_id=pt.id, name=pt.name, qty_per_unit=standard_per(pt), balance=part_balances[pt.id]
                    )
                    for pt in sorted(p.parts, key=lambda pt: (pt.sort_order, pt.id))
                    if pt.id in part_balances
                ],
                reservations=held,
                sku=p.sku,
                parts_on_shelf=sum(part_balances.values()),
            )
        )
    out.sort(key=lambda row: (-row.kits_available, row.name.lower()))
    return out


@router.get("", response_model=StockSummaryOut | StockListPage)
@part_images.attach
async def stock_summary(
    q: str | None = Query(None, max_length=200),
    with_stock: bool = Query(True),
    sort_by: str | None = Query(None, description="With page set: '<key>-<asc|desc>'; unknown → kits-desc"),
    page: int | None = Query(None, ge=1, description="Omit entirely for the flat {products} answer"),
    per_page: int = Query(24, ge=1, le=200),
    all: bool = Query(False, description="With page set, skip pagination and return every matching row"),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.STOCK_READ),
):
    """Every product with a shelf (see ``_stock_rows``). ``page`` is the compat
    switch every list of the section has (spec projects-lists-parity rule 1,
    workshop-lists rule 9): without it the flat ``{products}`` exactly as
    before; with it the envelope, whose rows also carry ``reserved_kits``."""
    loaded: dict = {}
    rows = await _stock_rows(db, q=q, with_stock=with_stock, loaded=loaded)
    if page is None:
        return StockSummaryOut(products=rows)
    key, direction, _computed = resolve_sort(_STOCK_SORT, sort_by)
    items = [StockListItem(**row.model_dump(), reserved_kits=sum(r.kits for r in row.reservations)) for row in rows]
    items = sort_computed(items, _STOCK_KEYS[key], direction, id_fn=lambda r: r.id)
    page_items = slice_page(items, page, per_page, all)
    # Z3: the per-option kits and the parts' options are read for the page alone.
    if page_items:
        products = [loaded["products"][row.id] for row in page_items]
        by_option = await stock_views.kits_by_option(db, products, loaded["balances"])
        labels = await stock_views.option_labels(
            db, {pt.variant_option_id for p in products for pt in p.parts if pt.variant_option_id is not None}
        )
        option_of = {pt.id: pt.variant_option_id for p in products for pt in p.parts}
        for row in page_items:
            row.kits_by_option = by_option.get(row.id, [])
            for part in row.parts:
                option = option_of.get(part.part_id)
                part.variant = labels.get(option) if option is not None else None
    return StockListPage(items=page_items, meta=page_meta(len(items), page, per_page, all))


@router.get("/figures", response_model=StockFigures)
async def stock_figures(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.STOCK_READ),
):
    """The stock page's tiles — the whole shelf, never the list's search or its
    «only with stock» (spec workshop-lists, rules 1, 4). The same rows the list
    reads, so a tile and the table cannot disagree."""
    rows = await _stock_rows(db, q=None, with_stock=False)
    return StockFigures(
        kits=sum(r.kits_available for r in rows),
        kit_products=sum(1 for r in rows if r.kits_available > 0),
        parts=sum(b.balance for r in rows for b in r.parts),
        reserved_kits=sum(res.kits for r in rows for res in r.reservations),
        incomplete=sum(1 for r in rows if r.kits_available == 0 and any(b.balance > 0 for b in r.parts)),
    )


@router.get("/movements", response_model=StockMovementsPageOut)
@part_images.attach
async def stock_movements(
    product_id: int | None = Query(None),
    part_id: int | None = Query(None),
    reason: StockReason | None = Query(None),
    before_id: int | None = Query(None, ge=1),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.STOCK_READ),
):
    """The farm's ledger, newest first, one keyset page at a time.

    ``next_before_id`` is set only when the page came back full — a short page
    IS the end, and the client stops asking. A filter naming nothing yields an
    empty page, not a 404: a journal filter is not a lookup.
    """
    rows = await part_stock.movements_across(
        db,
        product_id=product_id,
        part_id=part_id,
        reason=reason.value if reason is not None else None,
        before_id=before_id,
        limit=limit,
    )
    names = {movement.product_part_id: part_name for movement, part_name, _pid, _pname in rows}
    orders = await orders_of_lines(db, {m.project_line_id for m, *_ in rows if m.project_line_id is not None})
    items = [
        StockMovementRowOut(**movement_out(movement, names, orders).model_dump(), product_id=pid, product_name=pname)
        for movement, _part_name, pid, pname in rows
    ]
    return StockMovementsPageOut(items=items, next_before_id=items[-1].id if len(items) == limit else None)


# ---------- finished goods (spec workshop-finished-goods, rules 16–21) ----------

_ITEM_SORT = SortSpec(
    sql={
        "product": (func.lower(Product.name), False),
        "code": (StockItem.id, False),
        "location": (func.lower(StockItem.location), True),
        "on_hand": (StockItem.on_hand, False),
        "reserved": (StockItem.reserved, False),
        "available": (StockItem.on_hand - StockItem.reserved, False),
        "min": (StockItem.min_qty, False),
    },
    default="product-asc",
)
# One definition for the stock page and the catalog (WS-13 E1 PC1).
_BELOW_MIN = finished_stock_views.BELOW_MIN
_TRACKED = finished_stock_views.TRACKED
_MODES = {"tracked": _TRACKED, "low": _BELOW_MIN, "reserved": StockItem.reserved > 0, "all": None}


def _item_word_matches(word: str):
    """One search word against a position: product name, SKU, location, the
    name of a chosen option, or the position's code."""
    needle = like_contains(word)

    def like(column):
        return column.ilike(needle, escape="\\")

    fields = [
        like(Product.name),
        like(Product.sku),
        like(StockItem.location),
        exists().where(
            StockItemChoice.item_id == StockItem.id,
            ProductVariantOption.id == StockItemChoice.option_id,
            like(ProductVariantOption.name),
        ),
    ]
    if (item_id := id_from_query("stock_item", word)) is not None:
        fields.append(StockItem.id == item_id)
    return or_(*fields)


def _raise(e: Exception) -> NoReturn:
    raise HTTPException(status_code=getattr(e, "status", 409), detail=str(e)) from e


async def _choices_from_options(db: AsyncSession, product_id: int, options: list[int]) -> dict[int, int]:
    """``{group: option}`` for the picked options — a foreign one is 422."""
    if not options:
        return {}
    rows = dict(
        (
            await db.execute(
                select(ProductVariantOption.id, ProductVariantOption.group_id)
                .join(ProductVariantGroup, ProductVariantGroup.id == ProductVariantOption.group_id)
                .where(ProductVariantOption.id.in_(options), ProductVariantGroup.product_id == product_id)
            )
        ).all()
    )
    if any(option_id not in rows for option_id in options):
        raise HTTPException(status_code=422, detail="That option does not belong to this product")
    choices = {rows[option_id]: option_id for option_id in set(options)}
    if len(choices) < len(set(options)):
        # Two options of one group are not a configuration; the last one must not
        # silently win.
        raise HTTPException(status_code=422, detail="Pick one option per group")
    return choices


def _parse_options(options: str | None) -> list[int]:
    try:
        return [int(item) for item in (options or "").split(",") if item.strip()]
    except ValueError as e:
        raise HTTPException(status_code=422, detail="Options and counts must be numbers") from e


async def _item_or_404(db: AsyncSession, item_id: int) -> StockItem:
    item = await db.get(StockItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Stock position not found")
    return item


async def _resolve_item(
    db: AsyncSession, *, item_id: int | None, product_id: int | None, options: list[int], create: bool
) -> StockItem:
    if item_id is not None:
        return await _item_or_404(db, item_id)
    if product_id is None:
        raise HTTPException(status_code=422, detail="Name a stock position or a product")
    if create:
        # A position may be created: the product's gate before its configuration is
        # read (WS-13 E1 BL3 / BL4).
        await product_gate(db, [product_id])
    choices = await _choices_from_options(db, product_id, options)
    try:
        item = await finished_stock.item_for(db, product_id, choices, create=create)
    except finished_stock.FinishedStockError as e:
        _raise(e)
    if item is None:
        raise HTTPException(status_code=404, detail="Stock position not found")
    return item


async def _fresh_out(db: AsyncSession, item: StockItem) -> StockItemOut:
    await db.flush()
    await db.refresh(item)
    return (await finished_stock_views.items_out(db, [item]))[0]


def _counted(data: StockMoveIn) -> int:
    return data.counted if data.counted is not None else -1


@router.get("/items", response_model=StockItemsPage)
async def list_stock_items(
    mode: Literal["tracked", "low", "reserved", "all"] = Query("tracked"),
    q: str | None = Query(None, max_length=200),
    product_id: int | None = Query(None),
    sort_by: str | None = Query(None, description="'<key>-<asc|desc>'; unknown → product-asc"),
    page: int = Query(1, ge=1),
    per_page: int = Query(24, ge=1, le=200),
    all: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.STOCK_READ),
):
    """The finished-goods positions — filtered, searched, sorted and paged in SQL."""
    query = select(StockItem).join(Product, Product.id == StockItem.product_id)
    if _MODES[mode] is not None:
        query = query.where(_MODES[mode])
    if product_id is not None:
        query = query.where(StockItem.product_id == product_id)
    for word in (q or "").split():
        query = query.where(_item_word_matches(word))
    total = await db.scalar(select(func.count()).select_from(query.subquery())) or 0
    key, direction, _computed = resolve_sort(_ITEM_SORT, sort_by)
    query = apply_sql_sort(query, _ITEM_SORT, key, direction, StockItem.id)
    if not all:
        query = query.limit(per_page).offset((page - 1) * per_page)
    items = (await db.execute(query)).scalars().all()
    return StockItemsPage(
        items=await finished_stock_views.items_out(db, items), meta=page_meta(total, page, per_page, all)
    )


@router.get("/catalog", response_model=list[StockCatalogProduct])
async def stock_catalog(
    q: str | None = Query(None, description="Name or SKU"),
    product_id: int | None = Query(None, description="One product, whatever its origin (a dialog opened for it)"),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequireAnyPermission(Permission.STOCK_READ, Permission.PRODUCTS_READ),
):
    """The products a stock dialog may pick, with their variant groups (WS-13 E13 O12, STK-10):
    a storekeeper picks a product and its options without the catalog's read — no prices, no
    part shelf, no order counts. The catalog's own products, as the dialogs always listed."""
    # A dialog opened for one product names it by id — a one-off too (its assembly rule needs the
    # origin); the pick list is the catalog's own products (WS-13 E13 T17).
    if product_id is not None:
        query = select(Product).where(Product.id == product_id)
    else:
        query = select(Product).where(Product.origin == ProductOrigin.CATALOG.value)
    if q and q.strip():
        # A `%` or `_` the person typed is a letter, never a pattern.
        needle = like_contains(q.strip())
        query = query.where(or_(Product.name.ilike(needle, escape="\\"), Product.sku.ilike(needle, escape="\\")))
    products = (await db.execute(query.order_by(Product.name, Product.id))).scalars().all()
    groups: dict[int, list[ProductVariantGroup]] = {}
    if products:
        rows = await db.execute(
            select(ProductVariantGroup)
            .options(selectinload(ProductVariantGroup.options))
            .where(ProductVariantGroup.product_id.in_([p.id for p in products]))
            .order_by(ProductVariantGroup.position, ProductVariantGroup.id)
        )
        for group in rows.scalars().all():
            groups.setdefault(group.product_id, []).append(group)
    return [
        StockCatalogProduct(
            id=p.id,
            code=code_for("product", p.id),
            name=p.name,
            sku=p.sku,
            origin=p.origin,
            is_active=bool(p.is_active),
            has_cover=effective_cover(p) is not None,
            variant_groups=[
                StockCatalogGroup(
                    id=g.id,
                    name=g.name,
                    default_option_id=g.default_option_id,
                    options=[StockCatalogOption(id=o.id, name=o.name) for o in g.options],
                )
                for g in groups.get(p.id, [])
            ],
        )
        for p in products
    ]


@router.get("/items/summary", response_model=StockItemsSummary)
async def stock_items_summary(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.STOCK_READ),
):
    """The finished-goods tiles — the whole farm, never the list's filters."""
    on_hand, reserved = (
        await db.execute(
            select(func.coalesce(func.sum(StockItem.on_hand), 0), func.coalesce(func.sum(StockItem.reserved), 0))
        )
    ).one()
    tracked = await db.scalar(select(func.count(StockItem.id)).where(_TRACKED)) or 0
    low = await db.scalar(select(func.count(StockItem.id)).where(_BELOW_MIN)) or 0
    return StockItemsSummary(
        on_hand=int(on_hand),
        reserved=int(reserved),
        available=int(on_hand) - int(reserved),
        tracked=tracked,
        below_min=low,
    )


@router.get("/items/lookup", response_model=StockLookupOut)
@part_images.attach
async def lookup_stock_item(
    product_id: int = Query(...),
    options: str | None = Query(
        None, description="Chosen option ids, comma-separated; other groups take their standard"
    ),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.STOCK_READ),
):
    """What a dialog shows for a product and its options before anything moves."""
    choices = await _choices_from_options(db, product_id, _parse_options(options))
    try:
        item = await finished_stock.item_for(db, product_id, choices, create=False)
    except finished_stock.FinishedStockError as e:
        _raise(e)
    shelf = await part_stock.balances(db, product_id)
    if item is not None:
        row = (await finished_stock_views.items_out(db, [item]))[0]
        kit = await finished_stock.item_composition(db, item)
        return StockLookupOut(
            item=row,
            configuration=row.configuration,
            can_assemble=row.can_assemble,
            parts=finished_stock_views.parts_out(kit, shelf),
        )
    try:
        _key, new_choices, new_counts = await line_config.resolve(db, product_id, choices, {})
    except line_config.LineConfigError as e:
        _raise(e)
    parts = (await db.execute(select(ProductPart).where(ProductPart.product_id == product_id))).scalars().all()
    groups = (await groups_by_product(db, [product_id])).get(product_id, [])
    defaults = (await default_options(db, [product_id])).get(product_id, {})
    kit = composition(list(parts), "product", set({**defaults, **new_choices}.values()), new_counts)
    return StockLookupOut(
        item=None,
        configuration=configuration_out(groups, list(parts), LineConfig(new_choices, new_counts), defaults),
        can_assemble=part_stock.kits_of(shelf, kit),
        parts=finished_stock_views.parts_out(kit, shelf),
    )


async def _choices_for_items(db: AsyncSession, items: list[tuple[int, list[int]]]) -> list[dict[int, int]]:
    """``{group: option}`` for every item's options — one statement for the whole list.

    The products are looked up first (final review M2): an unknown product is a
    404 whatever options came with it, not a 422 about options it cannot have."""
    product_ids = sorted({pid for pid, _options in items})
    known = set((await db.execute(select(Product.id).where(Product.id.in_(product_ids)))).scalars())
    if len(known) < len(product_ids):
        raise HTTPException(status_code=404, detail="Product not found")
    wanted = sorted({option_id for _pid, options in items for option_id in options})
    rows = (
        {
            option_id: (group_id, product_id)
            for option_id, group_id, product_id in (
                await db.execute(
                    select(ProductVariantOption.id, ProductVariantOption.group_id, ProductVariantGroup.product_id)
                    .join(ProductVariantGroup, ProductVariantGroup.id == ProductVariantOption.group_id)
                    .where(ProductVariantOption.id.in_(wanted))
                )
            ).all()
        }
        if wanted
        else {}
    )
    out = []
    for product_id, options in items:
        if any(rows.get(option_id, (None, None))[1] != product_id for option_id in options):
            raise HTTPException(status_code=422, detail="That option does not belong to this product")
        choices = {rows[option_id][0]: option_id for option_id in set(options)}
        if len(choices) < len(set(options)):
            raise HTTPException(status_code=422, detail="Pick one option per group")
        out.append(choices)
    return out


@router.post("/suggest", response_model=StockSuggestOut)
async def suggest_stock(
    data: StockSuggestIn,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.STOCK_READ),
    creds: RequestCredentials = Depends(request_credentials),
):
    """What each line would take from stock — ready units of its configuration first,
    then kits of free parts, the rest to print (spec workshop-add-to-order, rules 5, 10).
    Writes nothing. An item naming an order line reads that order (WS-13 E13)."""
    if any(item.line_id is not None for item in data.items):
        await ensure(creds, Permission.ORDERS_READ)
    choices = await _choices_for_items(db, [(item.product_id, item.options) for item in data.items])
    requests = [
        stock_pick.PickRequest(item.product_id, chosen, item.part_counts, item.quantity, item.line_id)
        for item, chosen in zip(data.items, choices, strict=True)
    ]
    try:
        answers = await stock_pick.suggest(db, requests)
    except stock_pick.StockPickError as e:
        raise HTTPException(status_code=e.status, detail=str(e)) from e
    return StockSuggestOut(items=[StockSuggestLineOut(**asdict(answer)) for answer in answers])


_JOURNAL_PAGE_AND_CURSOR = "Use either page or cursor, not both"


@router.get("/journal/products", response_model=list[StockJournalProduct])
async def get_stock_journal_products(
    book: Literal["both", "finished", "parts"] = Query("both"),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.STOCK_READ),
):
    """The journal's product filter: products the chosen books moved (WS-13 E1 ST2)."""
    return await stock_journal.journal_products(db, book)


@router.get("/journal", response_model=StockJournalPage)
@part_images.attach
async def get_stock_journal(
    book: Literal["both", "finished", "parts"] = Query("both"),
    product_id: int | None = Query(None),
    item_id: int | None = Query(None),
    kind: str | None = Query(None, max_length=32),
    cursor: str | None = Query(None, max_length=100),
    limit: int = Query(50, ge=1, le=200),
    page: int | None = Query(None, ge=1, description="Numbered pages instead of the cursor"),
    per_page: int = Query(50, ge=1, le=200),
    sort_by: str | None = Query(None, description="With page set: date-desc (the default) or date-asc"),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.STOCK_READ),
):
    """Both stock ledgers as one feed, newest first, one keyset page at a time — or,
    with ``page`` set, by numbered pages with a total (WS-13 E1 ST1); never both."""
    if page is not None:
        if cursor:
            raise HTTPException(status_code=422, detail=_JOURNAL_PAGE_AND_CURSOR)
        return await stock_journal.journal_page(
            db,
            book=book,
            product_id=product_id,
            item_id=item_id,
            kind=kind,
            page=page,
            per_page=per_page,
            ascending=sort_by == "date-asc",
        )
    try:
        return await stock_journal.journal(
            db, book=book, product_id=product_id, item_id=item_id, kind=kind, cursor=cursor, limit=limit
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail="Unreadable journal cursor") from e


@router.get("/items/{item_id}", response_model=StockItemDetail)
@part_images.attach
async def get_stock_item(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.STOCK_READ),
):
    return await finished_stock_views.item_detail(db, await _item_or_404(db, item_id))


@router.patch("/items/{item_id}", response_model=StockItemOut)
async def update_stock_item(
    item_id: int,
    data: StockItemParamsIn,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.STOCK_ADJUST),
):
    """Комірка й мінімум — parameters of the position, not stock."""
    item = await _item_or_404(db, item_id)
    try:
        await finished_stock.set_params(db, item, {k: getattr(data, k) for k in data.model_fields_set})
    except finished_stock.FinishedStockError as e:
        _raise(e)
    return await _fresh_out(db, item)


@router.post("/moves", response_model=StockMoveOut)
async def move_stock(
    data: StockMoveIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequireAnyPermission(Permission.STOCK_MOVE, Permission.STOCK_ADJUST),
    creds: RequestCredentials = Depends(request_credentials),
):
    """One movement of a position: receipt, stocktake, reserve, release or issue.

    A receipt, and a count of more than nothing, create the position; a count of
    0 of a configuration with no position moves nothing and leaves nothing behind
    (a position appears with its first movement). ``moved`` is False when the
    count matched the shelf.

    A stocktake corrects the books — ``stock:adjust``; every other kind moves goods —
    ``stock:move`` (WS-13 E13). Asked before the position may be created."""
    await ensure(creds, Permission.STOCK_ADJUST if data.kind == "stocktake" else Permission.STOCK_MOVE)
    item = await _resolve_item(
        db,
        item_id=data.item_id,
        product_id=data.product_id,
        options=data.options,
        create=data.kind == "receipt" or (data.kind == "stocktake" and _counted(data) > 0),
    )
    actor = await acting_user(request, db, current_user)
    if data.customer_id is not None and await db.get(Customer, data.customer_id) is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    if data.kind == "issue" and data.customer_id is None:
        # Every issue is an issue TO somebody (spec workshop-order-issue, rule 16).
        raise HTTPException(status_code=422, detail="An issue names its customer")
    qty = data.qty if data.qty is not None else 0
    moved = True
    issue = None
    try:
        if data.kind == "receipt":
            await finished_stock.receive(db, item, qty, note=data.note, actor=actor)
        elif data.kind == "stocktake":
            moved = await finished_stock.stocktake(db, item, _counted(data), note=data.note, actor=actor) is not None
        elif data.kind == "reserve":
            await finished_stock.reserve(db, item, qty, note=data.note, actor=actor)
        elif data.kind == "release":
            await finished_stock.release(db, item, qty, note=data.note, actor=actor)
        else:
            recipient = (
                stock_issues.Recipient(**data.recipient.model_dump())
                if data.recipient is not None
                else await stock_issues.default_recipient(db, project=None, customer_id=data.customer_id)
            )
            issue = await stock_issues.create(
                db,
                customer_id=data.customer_id,
                project_id=None,
                recipient=recipient,
                waybill=data.waybill,
                note=data.note,
                actor=actor,
            )
            await finished_stock.issue(
                db,
                item,
                qty,
                from_reserve=data.from_reserve,
                customer_id=data.customer_id,
                note=data.note,
                actor=actor,
                stock_issue_id=issue.id,
            )
            await stock_issues.seal(db, issue, actor=actor)
    except (finished_stock.FinishedStockError, stock_issues.StockIssueError) as e:
        _raise(e)
    return StockMoveOut(
        **(await _fresh_out(db, item)).model_dump(),
        moved=moved,
        issue_id=issue.id if issue is not None else None,
        issue_code=code_for("dispatch_note", issue.id) if issue is not None else None,
    )


@router.post("/assemble", response_model=StockItemOut)
async def assemble_stock(
    data: StockAssembleIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.STOCK_MOVE),
):
    """Зібрати з деталей — the kit's parts leave the shelf, the position grows."""
    item = await _resolve_item(db, item_id=data.item_id, product_id=data.product_id, options=data.options, create=True)
    try:
        await finished_stock.assemble(
            db, item, data.qty, note=data.note, actor=await acting_user(request, db, current_user)
        )
    except (finished_stock.FinishedStockError, part_stock.PartStockError) as e:
        _raise(e)
    return await _fresh_out(db, item)
