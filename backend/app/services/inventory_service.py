"""Spool list/update logic extracted from `api/routes/inventory.py` (behavior-preserving)
so it can be called directly — not only through HTTP — by the cloud portal's remote-op
registry (see docs/superpowers/sdd/2026-08-28-cloud-portal-phase2-remote-inventory-agent).
"""

from typing import Any

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.core.db_dialect import is_postgres
from backend.app.models.location import Location
from backend.app.models.printer import Printer
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.schemas.spool import SpoolUpdate
from backend.app.services.location_service import prepare_internal_spool_payload


class SpoolNotFoundError(Exception):
    """No spool with the given id."""


# ── Server-driven list: shared filter/sort core (task 1, 2026-08-29) ────────
#
# Mirrors ``ArchiveService.list_archives``'s shape: :func:`build_spool_filters`
# returns a plain list of SQLAlchemy conditions ANDed together, the SAME list
# driving both the page query and the count. Every param below ports one row
# of the predicate table in
# ``docs/superpowers/plans/2026-08-29-server-driven-lists-spools.md`` — that
# table quotes the deleted client code (``InventoryPage.tsx``) as the
# behavioral spec. Port exactly.
#
# Task 2/3 (ids/facets/groups endpoints) share this same builder — it is
# deliberately the ONLY place these predicates are expressed.

# The six raw columns the client's search fallback matched against
# (InventoryPage.tsx:1270-1276) — the template-name match itself doesn't
# survive server-side (no access to the user's display-name template), so
# ``q`` degrades to a tokenised ilike over these columns (spec §3.1, accepted).
_SEARCH_COLUMNS = (
    Spool.brand,
    Spool.material,
    Spool.color_name,
    Spool.subtype,
    Spool.note,
    Spool.slicer_filament_name,
)

# The legacy default ordering list_spools has always used — kept as its own
# constant so the legacy branch (filters=None) and the new-path fallback
# (unrecognized/omitted sort_by) can share it without risking drift.
_DEFAULT_ORDER = (Spool.material, Spool.brand, Spool.color_name)


def _net_weight_expr():
    """``max(0, label_weight - weight_used)`` as a portable CASE.

    Not ``func.max(0, ...)`` — SQLite's multi-arg ``max()`` is a scalar
    function, but PostgreSQL's ``MAX`` is aggregate-only and has no such
    overload; ``func.max(0, X)`` would 500 on Postgres alone. CASE is
    identical on both dialects.
    """
    diff = Spool.label_weight - Spool.weight_used
    return case((diff < 0, 0), else_=diff)


def _remaining_pct_expr():
    """0..100 remaining-percent, 0 when ``label_weight<=0`` — the ``lowstock``
    usage filter's scale (matches ``_warn_if_low_stock``'s arithmetic in
    ``usage_tracker.py`` exactly: a low-stock notification that disagreed with
    this filter would be worse than neither existing)."""
    net = _net_weight_expr()
    return case((Spool.label_weight <= 0, 0), else_=(net * 100.0 / Spool.label_weight))


def _remaining_ratio_expr():
    """0..1 remaining ratio, 0 when ``label_weight<=0`` — the ``remaining``
    sort key's scale (distinct from the lowstock filter's 0..100 percent)."""
    net = _net_weight_expr()
    return case((Spool.label_weight <= 0, 0), else_=(net * 1.0 / Spool.label_weight))


def _weight_check_expr():
    """``CASE WHEN last_scale_weight IS NULL THEN -1 ELSE abs(last_scale_weight
    - (net + core_weight)) END`` — port of columnSortValues.weight_check."""
    net = _net_weight_expr()
    expected_gross = net + Spool.core_weight
    return case((Spool.last_scale_weight.is_(None), -1), else_=func.abs(Spool.last_scale_weight - expected_gross))


# The 3 nullable DATETIME sort columns — can't be ``func.coalesce(col, "")``
# like the nullable TEXT columns below (a timestamp/text CASE type mismatch
# 500s on PostgreSQL); the equivalent "NULL sorts as the smallest value" is
# instead applied at ORDER BY time via ``.nulls_first()``/``.nulls_last()``
# (see :func:`_spool_order_by`) — same final row order the client's ``|| ''``
# fallback produces (an empty string is lexicographically smaller than any
# ISO datetime string, so it always sorts first on asc / last on desc; NULL
# pinned first/last is the identical outcome). ``added_time`` -> created_at is
# NOT NULL (server_default), so it's deliberately excluded here.
_DATETIME_NULLABLE_SORT_KEYS = frozenset({"purchase_date", "encode_time", "last_used_time"})


def _spool_sort_columns() -> dict[str, Any]:
    """The FULL ``columnSortValues`` port (InventoryPage.tsx:514-565), minus
    ``display_name`` and ``location`` (both handled specially — see
    :func:`_spool_order_by`) and ``rgba``/``color_combined`` (operator ruling
    2026-08-29: the frontend remaps a swatch-header click to
    ``sort_by=color_name`` instead — the server never learns an rgba key).

    Nullable TEXT columns are coalesced to ``""`` (mirrors the client's
    ``s.field || ''`` fallback exactly — review finding 5) so NULL placement
    stops being dialect-dependent (SQLite defaults NULLS FIRST on asc,
    PostgreSQL defaults NULLS LAST — the same request would order differently
    per backend otherwise). Numeric nullable columns already coalesce to
    ``0`` (unchanged, matches the client's ``?? 0``). The 3 nullable
    DATETIME columns can't take the same treatment — see
    :data:`_DATETIME_NULLABLE_SORT_KEYS`.

    ⚠️ Deliberately NOT wrapped in ``func.lower()`` here: ``material``,
    ``subtype``, ``brand``, ``slicer_filament``, ``purchase_location``,
    ``note``, ``data_origin``, ``tag_type`` sort case-sensitively today
    (review finding 2) — ADJUDICATED DEFERRED 2026-08-29 as the same
    SQLite-collation class the operator has ruled gets solved once for every
    server-driven list at stage C, not per-list here. Do not add ``lower()``
    to these without re-opening that ruling.
    """
    net = _net_weight_expr()
    return {
        "id": Spool.id,
        "added_time": Spool.created_at,
        "purchase_date": Spool.purchase_date,
        "encode_time": Spool.encode_time,
        "last_used_time": Spool.last_used,
        "material": Spool.material,
        "subtype": func.coalesce(Spool.subtype, ""),
        "color_name": func.coalesce(func.lower(Spool.color_name), ""),
        "brand": func.coalesce(Spool.brand, ""),
        "slicer_filament": func.coalesce(Spool.slicer_filament_name, Spool.slicer_filament, ""),
        "storage_location": func.coalesce(func.lower(Spool.storage_location), ""),
        "purchase_location": func.coalesce(Spool.purchase_location, ""),
        "label_weight": Spool.label_weight,
        "net": net,
        "gross": net + Spool.core_weight,
        "used": Spool.weight_used,
        "remaining": _remaining_ratio_expr(),
        "note": func.coalesce(Spool.note, ""),
        "data_origin": func.coalesce(Spool.data_origin, ""),
        "tag_type": func.coalesce(Spool.tag_type, ""),
        # ``''`` is unconfigured, same as the ``stock`` FILTER just above (and
        # the client's ``s.slicer_filament ? 1 : 0`` extractor) — review
        # finding 4: this used to disagree with both.
        "stock": case((and_(Spool.slicer_filament.isnot(None), Spool.slicer_filament != ""), 1), else_=0),
        "spool_name": func.coalesce(Spool.core_weight_catalog_id, 0),
        "cost_per_kg": func.coalesce(Spool.cost_per_kg, 0),
        "filament_diameter": Spool.filament_diameter,
        "lot": func.coalesce(Spool.lot, 0),
        "weight_check": _weight_check_expr(),
    }


def _spool_order_by(sort_by: str | None) -> tuple[list, bool]:
    """Resolve ``sort_by`` (``<column>_asc``/``<column>_desc``) into ORDER BY
    clauses, plus whether the assignment/printer join is needed (``location``
    sort only).

    Falls back to the legacy default ordering (plus the tiebreak below) on a
    missing or unrecognized value — same permissive-fallback convention as
    ``ArchiveService.list_archives`` / the library file list (a stale
    bookmark should still open the page, not 400).

    ⚠️ ALWAYS appends ``Spool.id DESC`` as the tiebreak, regardless of the
    primary direction (same convention as ``ArchiveService``/the library file
    list) — this list PAGES, and rows sharing a sort key have no defined order
    between two queries otherwise.
    """
    tiebreak = Spool.id.desc()

    if not sort_by:
        return [*_DEFAULT_ORDER, tiebreak], False

    sort_key, _, sort_dir = sort_by.rpartition("_")
    if sort_dir not in ("asc", "desc"):
        return [*_DEFAULT_ORDER, tiebreak], False

    if sort_key == "location":
        # OPERATOR RULING 2026-08-29: implemented server-side (the operator
        # uses it constantly). ``outerjoin(SpoolAssignment).outerjoin(Printer)``
        # via :func:`_join_first_assignment` — same grouping the client's
        # label produced (shelf spools first, then printer -> unit -> slot),
        # tuple order replacing lexicographic order.
        if sort_dir == "asc":
            clauses = [
                func.coalesce(Printer.name, "").asc(),
                SpoolAssignment.ams_id.asc().nulls_first(),
                SpoolAssignment.tray_id.asc(),
            ]
        else:
            clauses = [
                func.coalesce(Printer.name, "").desc(),
                SpoolAssignment.ams_id.desc().nulls_last(),
                SpoolAssignment.tray_id.desc(),
            ]
        return [*clauses, tiebreak], True

    if sort_key == "display_name":
        # Composite (material, brand, color_name) — the template sort
        # approximates the client's formatted display name (spec-accepted).
        cols = (Spool.material, Spool.brand, Spool.color_name)
        clauses = [c.asc() for c in cols] if sort_dir == "asc" else [c.desc() for c in cols]
        return [*clauses, tiebreak], False

    column = _spool_sort_columns().get(sort_key)
    if column is None:
        return [*_DEFAULT_ORDER, tiebreak], False

    if sort_key in _DATETIME_NULLABLE_SORT_KEYS:
        # These 3 can't be coalesced to "" like the TEXT columns (a
        # timestamp/text CASE 500s on PostgreSQL) — nulls_first()/
        # nulls_last() produces the same final ordering the client's ``|| ''``
        # fallback does (NULL pinned as the smallest value) without a type
        # mismatch (review finding 5).
        clause = column.asc().nulls_first() if sort_dir == "asc" else column.desc().nulls_last()
    else:
        clause = column.asc() if sort_dir == "asc" else column.desc()
    return [clause, tiebreak], False


def _join_first_assignment(query):
    """Outerjoin each spool to its FIRST assignment (lowest id) and that
    assignment's printer — for the ``location`` sort only.

    ⚠️ ``SpoolAssignment``'s only UniqueConstraint is ``(printer_id, ams_id,
    tray_id)`` — a SLOT, not a spool — and ``assign_spool`` (routes/inventory.py)
    only ever de-dupes the TARGET slot, never a spool's other assignments. A
    spool can therefore end up with more than one assignment row (stale data,
    or a race), and a naive ``outerjoin(SpoolAssignment)`` would duplicate
    that spool's row in a paged list. Joining through a GROUP BY
    MIN(id)-per-spool subquery first picks exactly one deterministic
    assignment per spool, so this can never duplicate a row regardless of how
    many assignments a spool has.
    """
    first_assignment = (
        select(
            SpoolAssignment.spool_id.label("spool_id"),
            func.min(SpoolAssignment.id).label("min_assignment_id"),
        )
        .group_by(SpoolAssignment.spool_id)
        .subquery()
    )
    query = query.outerjoin(first_assignment, first_assignment.c.spool_id == Spool.id)
    query = query.outerjoin(SpoolAssignment, SpoolAssignment.id == first_assignment.c.min_assignment_id)
    query = query.outerjoin(Printer, Printer.id == SpoolAssignment.printer_id)
    return query


async def build_spool_filters(
    db: AsyncSession,
    *,
    archived: str | None = None,
    usage: str | None = None,
    material: str | None = None,
    brand: str | None = None,
    colors: list[str] | None = None,
    color_rgbas: list[str] | None = None,
    category: str | None = None,
    catalog_id: int | None = None,
    location_id: str | None = None,
    stock: str | None = None,
    assigned: str | None = None,
    q: str | None = None,
) -> list:
    """The shared WHERE-clause list for list/count/ids/facets/groups.

    Every param is optional and additive (AND'ed together); omitting a param
    applies no filter for that dimension — mirrors
    ``ArchiveService.list_archives``. Port table:
    ``docs/superpowers/plans/2026-08-29-server-driven-lists-spools.md``.

    ``location_id`` is a ``str`` (not ``int``) because it carries a sentinel:
    ``"__none__"`` means "no location at all" (matches ``category``'s same
    sentinel convention), and a numeric string otherwise resolves to the FK +
    legacy-text-fallback predicate below.
    """
    filters: list = []

    if archived == "active":
        filters.append(Spool.archived_at.is_(None))
    elif archived == "archived":
        filters.append(Spool.archived_at.isnot(None))

    if usage == "used":
        filters.append(Spool.weight_used > 0)
    elif usage == "new":
        filters.append(Spool.weight_used == 0)
    elif usage == "lowstock":
        # Reuse usage_tracker's tested resolver rather than re-deriving the
        # global-setting-with-fallback logic here — a low-stock filter that
        # disagrees with the low-stock NOTIFICATION would be worse than either
        # alone. Local import: usage_tracker is a large module with its own
        # import surface, and every OTHER filter branch here never needs it.
        from backend.app.services.usage_tracker import _global_low_stock_threshold

        global_threshold = await _global_low_stock_threshold(db)
        threshold = func.coalesce(Spool.low_stock_threshold_pct, global_threshold)
        filters.append(_remaining_pct_expr() < threshold)

    if material:
        filters.append(Spool.material == material)

    if brand:
        filters.append(Spool.brand == brand)

    if colors or color_rgbas:
        # Raw (color_name, rgba) pairs — the client resolves the catalog
        # display name and sends back whichever raw values resolve to it
        # (facets endpoint, task 2). Resolution stays client-side, where the
        # catalog lives (ColorCatalogProvider) — see the plan's colour design.
        color_clauses = []
        if colors:
            color_clauses.append(Spool.color_name.in_(colors))
        if color_rgbas:
            color_clauses.append(and_(Spool.color_name.is_(None), Spool.rgba.in_(color_rgbas)))
        filters.append(or_(*color_clauses))

    if category == "__none__":
        filters.append(or_(Spool.category.is_(None), Spool.category == ""))
    elif category:
        # Trim BOTH sides: the facets endpoint advertises TRIMMED options
        # (' Production' and 'Production' are one dropdown entry — mirroring
        # the client's ``.map(c?.trim())``), so the filter must match what the
        # facet advertised, or a padded-category spool (the CSV-import shape)
        # contributes an option that matches nothing and list/count/ids
        # disagree with facets. This deliberately DIVERGES from the deleted
        # client's own wart (dropdown trimmed, filter exact-matched) — the API
        # contract must be self-consistent (T2 review, minor 1).
        filters.append(func.trim(Spool.category) == category.strip())

    if catalog_id is not None:
        filters.append(Spool.core_weight_catalog_id == catalog_id)

    if location_id == "__none__":
        filters.append(
            and_(
                Spool.location_id.is_(None),
                or_(Spool.storage_location.is_(None), func.trim(Spool.storage_location) == ""),
            )
        )
    elif location_id is not None:
        loc_id_int = int(location_id)
        loc_name = (await db.execute(select(Location.name).where(Location.id == loc_id_int))).scalar_one_or_none()
        if loc_name:
            # ⚠️ Fold BOTH sides with the SAME function (SQL ``lower()``), never
            # SQL lower() on the column vs Python ``.lower()`` on the literal —
            # SQLite's built-in ``lower()`` folds ASCII only (Cyrillic passes
            # through unchanged), so a Python-folded literal compared against a
            # SQL-folded column silently never matches a byte-identical
            # non-ASCII location name (review finding 1, measured on SQLite
            # 3.49.1: ``lower('Полиця A')`` stays `'Полиця a'`). Folding both
            # sides through the same ``func.lower()`` keeps identical-case
            # matches working regardless of script; only a pure case
            # difference in non-ASCII text ("ПОЛИЦЯ" vs stored "Полиця") is
            # inherently unreachable via SQL ``lower()`` on SQLite — the
            # historical client behaviour for ASCII-only folding, not a new
            # regression.
            filters.append(
                or_(
                    Spool.location_id == loc_id_int,
                    and_(
                        Spool.location_id.is_(None),
                        func.lower(func.trim(Spool.storage_location)) == func.lower(loc_name.strip()),
                    ),
                )
            )
        else:
            # No location with this id — only the FK branch can ever match
            # (there is no resolved name for the legacy-text fallback to
            # compare against), which correctly yields zero rows for a bogus id.
            filters.append(Spool.location_id == loc_id_int)

    if stock == "stock":
        filters.append(or_(Spool.slicer_filament.is_(None), Spool.slicer_filament == ""))
    elif stock == "configured":
        filters.append(and_(Spool.slicer_filament.isnot(None), Spool.slicer_filament != ""))

    if assigned == "assigned":
        filters.append(select(SpoolAssignment.spool_id).where(SpoolAssignment.spool_id == Spool.id).exists())
    elif assigned == "unassigned":
        filters.append(~select(SpoolAssignment.spool_id).where(SpoolAssignment.spool_id == Spool.id).exists())

    if q and q.strip():
        # Tokenised ilike: each token must match AT LEAST ONE of the six
        # columns (OR), and every token must match SOMETHING (AND across
        # tokens) — so "SUN Bl" finds a SUNLU-brand Black spool the same way
        # the deleted client-side template search did (spec §3.1).
        for token in q.strip().split():
            term = f"%{token}%"
            filters.append(or_(*(col.ilike(term) for col in _SEARCH_COLUMNS)))

    return filters


async def list_spools(
    db: AsyncSession,
    *,
    include_archived: bool = False,
    filters: list | None = None,
    sort_by: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[Spool]:
    """List spools.

    Two paths:

    - **Legacy** (``filters`` omitted — ``None``): historical behaviour,
      byte-for-byte unchanged. ``include_archived`` gates archived rows, fixed
      material/brand/colour ordering, no tiebreak. Every existing caller (the
      bare ``GET /spools`` route, the Cloud Link remote op via
      ``remote_ops._list_spools``) uses exactly this path and must keep
      working unchanged — neither passes ``filters`` or ``sort_by``.
    - **Server-driven** (``filters`` a list — even ``[]``): the paged
      ``GET /spools?page=`` branch. ``filters`` (built by
      :func:`build_spool_filters`) replaces the ``include_archived`` gate
      entirely; ``sort_by`` selects the sort map, falling back to the legacy
      default plus a ``Spool.id`` tiebreak on anything missing/unrecognized —
      see :func:`_spool_order_by`.

    ``limit``/``offset`` page either ordering. ``None`` (the default) keeps
    the historical unbounded behaviour the local HTTP route relies on; the
    Cloud Link remote op always passes a real limit (its answer must fit one
    ws frame — see ``remote_ops._slim_spool``).
    """
    query = select(Spool).options(selectinload(Spool.k_profiles))

    if filters is None:
        if not include_archived:
            query = query.where(Spool.archived_at.is_(None))
        query = query.order_by(*_DEFAULT_ORDER)
    else:
        order_clauses, needs_location_join = _spool_order_by(sort_by)
        if needs_location_join:
            query = _join_first_assignment(query)
        query = query.where(*filters).order_by(*order_clauses)

    if offset:
        query = query.offset(offset)
    if limit is not None:
        query = query.limit(limit)
    result = await db.execute(query)
    return list(result.scalars().all())


async def count_spools(db: AsyncSession, *, include_archived: bool = False, filters: list | None = None) -> int:
    """How many spools :func:`list_spools` would page over — its ``total``.

    Same two-path split as :func:`list_spools`: ``filters=None`` reproduces
    the historical ``include_archived`` gate; a ``filters`` list (even
    ``[]``) counts under those conditions instead.
    """
    query = select(func.count()).select_from(Spool)
    if filters is None:
        if not include_archived:
            query = query.where(Spool.archived_at.is_(None))
    else:
        query = query.where(*filters)
    return int((await db.execute(query)).scalar_one())


async def list_spool_ids(db: AsyncSession, *, filters: list, limit: int | None = None) -> list[int]:
    """Just the ids of the spools matching ``filters`` — the "Select all N
    matching the filter" feed (task 2, spec §3.4).

    ``filters`` comes from :func:`build_spool_filters` — the SAME list the
    paged query and count consume, so the materialized selection can never
    include a row the list wouldn't show (the whole point of the endpoint;
    the bulk-action invariant stays selection-scoped, explicit ids only).

    Ordered by ``Spool.id`` ascending — deterministic, and no sort map means
    no ``location`` join, so a spool with several assignment rows can never
    duplicate here. ``limit`` exists for the route's sanity cap (it asks for
    cap+1 and refuses when it gets them); ``None`` returns everything.
    """
    query = select(Spool.id).where(*filters).order_by(Spool.id)
    if limit is not None:
        query = query.limit(limit)
    result = await db.execute(query)
    return [int(spool_id) for spool_id in result.scalars().all()]


async def spool_facets(db: AsyncSession, *, filters: list) -> dict[str, list]:
    """Distinct filter-dropdown values over the spools matching ``filters``
    (task 2, spec §3.6 — in practice the route passes only the ``archived``
    tab condition, but taking the shared filters list keeps this on the same
    contract as list/count/ids).

    Five cheap DISTINCT queries, one per dimension — a UNION contortion would
    save round-trips and cost readability for a call that fires once per
    dropdown open. Each mirrors the client derivation it replaces
    (``InventoryPage.tsx:1301-1321``):

    - ``materials`` — plain distinct (the column is NOT NULL; the client
      applied no filtering).
    - ``brands`` — NULL/'' excluded (client ``.filter(Boolean)``).
    - ``categories`` — trimmed first, then NULL/blank excluded (client
      ``.map(c?.trim()).filter(Boolean)`` — ' Prod' and 'Prod' are ONE option).
    - ``catalog_ids`` — distinct non-NULL ids only; the display NAMES stay a
      client concern (it already holds the catalog via ``GET /catalog``).
    - ``colors`` — RAW distinct ``(color_name, rgba)`` pairs; resolution and
      grouping by display name stay client-side where the colour catalog
      lives (``ColorCatalogProvider``). A pair with BOTH sides NULL is
      dropped: it can never be sent back as a filter (the rgba branch
      requires a NULL name AND a concrete rgba) and resolves to nothing.

    Lists come back sorted by the underlying SQL collation for determinism;
    the client re-sorts for display anyway (same stage-C collation deferral
    as the sort map — don't add ``lower()`` here either).
    """
    materials_q = select(Spool.material).where(*filters).distinct().order_by(Spool.material)
    materials = list((await db.execute(materials_q)).scalars().all())

    brands_q = (
        select(Spool.brand).where(*filters, Spool.brand.isnot(None), Spool.brand != "").distinct().order_by(Spool.brand)
    )
    brands = list((await db.execute(brands_q)).scalars().all())

    trimmed_category = func.trim(Spool.category)
    categories_q = (
        select(trimmed_category)
        .where(*filters, Spool.category.isnot(None), trimmed_category != "")
        .distinct()
        .order_by(trimmed_category)
    )
    categories = list((await db.execute(categories_q)).scalars().all())

    catalog_ids_q = (
        select(Spool.core_weight_catalog_id)
        .where(*filters, Spool.core_weight_catalog_id.isnot(None))
        .distinct()
        .order_by(Spool.core_weight_catalog_id)
    )
    catalog_ids = [int(cid) for cid in (await db.execute(catalog_ids_q)).scalars().all()]

    colors_q = (
        select(Spool.color_name, Spool.rgba)
        .where(*filters, or_(Spool.color_name.isnot(None), Spool.rgba.isnot(None)))
        .distinct()
        .order_by(Spool.color_name, Spool.rgba)
    )
    colors = [{"color_name": color_name, "rgba": rgba} for color_name, rgba in (await db.execute(colors_q)).all()]

    return {
        "materials": materials,
        "brands": brands,
        "categories": categories,
        "catalog_ids": catalog_ids,
        "colors": colors,
    }


# ── Stats bar — the five cards, aggregated (task 5, 2026-08-29) ─────────────


async def inventory_stats(db: AsyncSession) -> dict[str, Any]:
    """Everything the Inventory stats bar shows, as two aggregate scans.

    The behavioral spec is the client memo this replaces (``const stats`` in
    ``InventoryPage.tsx``, quoted in full in the module docstring of
    ``backend/tests/test_inventory_stats.py``) — it ran over a full-table
    ``all=true`` fetch, the page's last one.

    Every arithmetic piece reuses the expressions the ``lowstock`` list filter
    and the ``filament_low`` notification already share
    (:func:`_net_weight_expr`, :func:`_remaining_pct_expr`,
    ``usage_tracker._global_low_stock_threshold``): a stats card that
    disagreed with the filter beside it would be worse than no card.

    Scope per field is deliberately mixed — see ``InventoryStatsResponse``.
    """
    # Local import for the same reason ``build_spool_filters`` takes it
    # locally: usage_tracker is a large module and only this one line needs it.
    from backend.app.services.usage_tracker import _global_low_stock_threshold

    global_threshold = await _global_low_stock_threshold(db)

    is_live = Spool.archived_at.is_(None)
    net = _net_weight_expr()  # max(0, label_weight - weight_used)
    # ``Math.max(0, weight_used - (weight_used_baseline ?? 0))``, clamped PER
    # SPOOL: an over-reset row (baseline above the counter — a reset, then an
    # AMS correction downward) must not eat a healthy spool's grams.
    consumed = Spool.weight_used - func.coalesce(Spool.weight_used_baseline, 0.0)
    consumed_clamped = case((consumed < 0, 0.0), else_=consumed)
    # ``s.low_stock_threshold_pct ?? lowStockThreshold`` — NULL means "use the
    # global", not "zero"; the comparison is strict, so a spool sitting exactly
    # on its threshold is not low.
    threshold = func.coalesce(Spool.low_stock_threshold_pct, global_threshold)

    totals = (
        await db.execute(
            select(
                func.count(Spool.id).label("total_spools"),
                func.sum(case((is_live, 1), else_=0)).label("active_spools"),
                func.sum(case((is_live, net), else_=0.0)).label("total_weight_g"),
                func.sum(consumed_clamped).label("total_consumed_g"),
                func.sum(case((is_live & (_remaining_pct_expr() < threshold), 1), else_=0)).label("low_stock_count"),
            )
        )
    ).one()

    material_rows = (
        await db.execute(
            select(
                Spool.material,
                func.count(Spool.id).label("count"),
                func.sum(net).label("remaining_g"),
            )
            .where(is_live)
            .group_by(Spool.material)
        )
    ).all()

    # ``s.material || 'Unknown'`` collapses a blank and a NULL into ONE bucket,
    # so the merge happens here rather than in the GROUP BY (the DB keeps them
    # apart) — the same NULL↔'' collapse the forecast engine does in Python.
    by_material: dict[str, dict[str, Any]] = {}
    for row in material_rows:
        name = row.material or "Unknown"
        bucket = by_material.setdefault(name, {"material": name, "count": 0, "remaining_g": 0.0})
        bucket["count"] += int(row.count or 0)
        bucket["remaining_g"] += float(row.remaining_g or 0.0)

    return {
        "total_spools": int(totals.total_spools or 0),
        "active_spools": int(totals.active_spools or 0),
        "total_weight_g": float(totals.total_weight_g or 0.0),
        "total_consumed_g": float(totals.total_consumed_g or 0.0),
        # Heaviest first — the client sorted at render (``topMaterials``);
        # served pre-sorted so the rule keeps one owner. The material name
        # breaks ties, which the client's stable sort left to insertion order.
        "by_material": sorted(by_material.values(), key=lambda b: (-b["remaining_g"], b["material"])),
        "low_stock_count": int(totals.low_stock_count or 0),
    }


# ── Grouped mode — "group similar spools" server-side (task 3, 2026-08-29) ──
#
# ``group_similar=true`` turns the paged list's rows into GROUPS. The group
# key is the EXACT port of the deleted client's ``spoolGroupKey``
# (InventoryPage.tsx:83-87):
#
#     `${material}|${subtype || ''}|${brand || ''}|${color_name || ''}|
#      ${rgba || ''}|${label_weight}|${lot ?? ''}`
#
# and the client's CONSUMERS (:1402-1439) are part of the behavioral spec
# too: only unused (``weight_used === 0``) AND unassigned spools are eligible
# to merge — a used or assigned spool always stays its own row, however many
# twins its key has. No case folding and no trimming anywhere in the key —
# the client key had none (and the stage-C collation ruling forbids adding
# any).

# The sort keys grouped mode accepts — the subset of the flat sort map that
# maps onto group-key columns (plan Task 3 ruling). Everything else is
# REFUSED (a 400 at the route, ValueError here) rather than silently
# falling back like the flat list does: a caller asking to page groups under
# an order the grouped query cannot express should hear "no", not receive
# stable-looking pages in a different order.
GROUP_SORT_KEYS = frozenset({"display_name", "material", "brand", "color_name"})


def _spool_group_key_exprs() -> dict[str, Any]:
    """The 7 group-key columns, keyed by their response-field names.

    - ``material`` / ``label_weight`` — raw (both NOT NULL; the client key
      interpolated them uncoalesced).
    - ``subtype`` / ``brand`` / ``color_name`` / ``rgba`` —
      ``coalesce(col, '')``: the client's ``|| ''`` folds NULL and ``''``
      into ONE key value (pinned by the client's own grouping test), and a
      bare GROUP BY would keep them apart.
    - ``lot`` — RAW, deliberately NOT coalesced: the client used ``?? ''``
      (nullish), not ``|| ''`` — so ``lot=0`` keys as ``'0'`` while NULL keys
      as ``''``. GROUP BY on the bare integer column reproduces exactly that:
      NULLs group together (SQL GROUP BY treats NULLs as equal on both
      dialects), and 0 stays its own group. ``coalesce(lot, 0)`` (the SORT
      map's spelling) would wrongly merge them.
    """
    return {
        "material": Spool.material,
        "subtype": func.coalesce(Spool.subtype, ""),
        "brand": func.coalesce(Spool.brand, ""),
        "color_name": func.coalesce(Spool.color_name, ""),
        "rgba": func.coalesce(Spool.rgba, ""),
        "label_weight": Spool.label_weight,
        "lot": Spool.lot,
    }


def _spool_group_single_id_expr():
    """The eligibility discriminator, joined into GROUP BY: NULL for a spool
    the client would merge (unused AND unassigned), the spool's own id
    otherwise. Eligible rows share the NULL and group by the key alone;
    every ineligible row gets a unique value and therefore stays a singleton
    group — including two ineligible twins, which the client also never
    merged with each other (InventoryPage.tsx:1407-1424). The assignment
    check is the same correlated EXISTS the ``assigned`` filter uses."""
    assigned = select(SpoolAssignment.spool_id).where(SpoolAssignment.spool_id == Spool.id).exists()
    return case((or_(Spool.weight_used > 0, assigned), Spool.id))


def assert_group_sort_supported(sort_by: str | None) -> None:
    """Raise ``ValueError`` unless ``sort_by`` is valid for grouped mode:
    omitted/empty (→ the default ordering), or one of
    :data:`GROUP_SORT_KEYS` with an ``_asc``/``_desc`` suffix. The route
    calls this up front to turn the refusal into a 400 before any DB work;
    :func:`list_spool_groups` enforces it again for direct callers."""
    if not sort_by:
        return
    key, _, direction = sort_by.rpartition("_")
    if direction not in ("asc", "desc") or key not in GROUP_SORT_KEYS:
        raise ValueError(
            f"sort_by {sort_by!r} is not available in grouped mode — "
            f"use one of {sorted(GROUP_SORT_KEYS)} with _asc/_desc, or omit it"
        )


def _spool_groups_subquery(filters: list):
    """One grouped SELECT shared by list and count: the labeled key columns +
    ``group_count`` + ``rep_id`` (min member id — the representative, per the
    plan's ruling) + the aggregated member ids.

    The member-id aggregate is the one per-dialect branch (the plan's ⚠️):
    PostgreSQL ``array_agg`` returns a real int array, SQLite
    ``group_concat`` a comma-joined string — :func:`_parse_member_ids`
    normalizes both (and sorts, since neither aggregate guarantees an order
    without dialect-specific ordered-aggregate syntax). Branching via
    ``db_dialect.is_postgres`` is the house pattern (archives FTS,
    migrations helpers)."""
    keys = _spool_group_key_exprs()
    member_ids = func.array_agg(Spool.id) if is_postgres() else func.group_concat(Spool.id)
    return (
        select(
            *(expr.label(name) for name, expr in keys.items()),
            func.count().label("group_count"),
            func.min(Spool.id).label("rep_id"),
            member_ids.label("member_ids"),
        )
        .where(*filters)
        .group_by(*keys.values(), _spool_group_single_id_expr())
        .subquery()
    )


def _parse_member_ids(raw) -> list[int]:
    """Normalize the per-dialect member-id aggregate to a sorted int list —
    ascending, same determinism convention as ``list_spool_ids``."""
    if isinstance(raw, str):  # SQLite group_concat: "3,1,2"
        return sorted(int(part) for part in raw.split(","))
    return sorted(int(member_id) for member_id in raw)  # PostgreSQL array_agg


def _spool_group_order_by(sub, sort_by: str | None) -> list:
    """ORDER BY over the grouped subquery's output columns (so PostgreSQL
    never has to match grouping expressions), ALWAYS ending in the
    ``rep_id DESC`` tiebreak — min member ids are unique across groups
    (every spool belongs to exactly one), so grouped pages are stable the
    same way the flat list's ``Spool.id`` tiebreak makes its pages stable.

    Omitted ``sort_by`` mirrors the flat default (material, brand,
    color_name) over the key columns — which are coalesced, so a NULL
    brand/color orders as ``''`` deterministically (the review-finding-5
    convention). ``color_name`` keeps the flat map's ``lower()`` — a
    pre-existing client extractor fold, not a stage-C addition — applied
    OUTSIDE the subquery where no GROUP BY constraint applies."""
    assert_group_sort_supported(sort_by)
    tiebreak = sub.c.rep_id.desc()

    if not sort_by:
        return [sub.c.material.asc(), sub.c.brand.asc(), sub.c.color_name.asc(), tiebreak]

    key, _, direction = sort_by.rpartition("_")
    if key == "display_name":
        # Same composite the flat mode's display_name resolves to.
        cols = (sub.c.material, sub.c.brand, sub.c.color_name)
        clauses = [c.asc() for c in cols] if direction == "asc" else [c.desc() for c in cols]
        return [*clauses, tiebreak]

    expr = {
        "material": sub.c.material,
        "brand": sub.c.brand,
        "color_name": func.lower(sub.c.color_name),
    }[key]
    return [expr.asc() if direction == "asc" else expr.desc(), tiebreak]


async def list_spool_groups(
    db: AsyncSession,
    *,
    filters: list,
    sort_by: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict]:
    """One page of GROUPS for the ``group_similar=true`` list mode.

    ``filters`` (from :func:`build_spool_filters` — the same list every other
    surface consumes) applies BEFORE grouping: a filtered-out spool is
    neither counted nor listed among a group's members. Pagination applies
    over GROUPS, after ordering (``limit``/``offset`` land on the grouped
    query itself).

    Each returned dict carries the key fields (text keys COALESCED — ``''``
    where the column is NULL, because that IS the key value; ``lot`` raw,
    None for the all-NULL-lots group), ``group_count``, the COMPLETE sorted
    member ``ids``, and ``representative`` — the min(id) member as an ORM
    ``Spool`` row with ``k_profiles`` eager-loaded, ready for
    ``_spool_to_list_item``. The representative is fetched by joining
    ``Spool`` back onto the grouped subquery in the SAME statement, so a
    concurrently-deleted spool can never leave a group row without its
    representative."""
    sub = _spool_groups_subquery(filters)
    order_clauses = _spool_group_order_by(sub, sort_by)
    query = (
        select(
            Spool,
            sub.c.material,
            sub.c.subtype,
            sub.c.brand,
            sub.c.color_name,
            sub.c.rgba,
            sub.c.label_weight,
            sub.c.lot,
            sub.c.group_count,
            sub.c.member_ids,
        )
        .join(sub, Spool.id == sub.c.rep_id)
        .options(selectinload(Spool.k_profiles))
        .order_by(*order_clauses)
    )
    if offset:
        query = query.offset(offset)
    if limit is not None:
        query = query.limit(limit)

    rows = (await db.execute(query)).all()
    return [
        {
            "material": row.material,
            "subtype": row.subtype,
            "brand": row.brand,
            "color_name": row.color_name,
            "rgba": row.rgba,
            "label_weight": int(row.label_weight),
            "lot": row.lot,
            "group_count": int(row.group_count),
            "ids": _parse_member_ids(row.member_ids),
            "representative": row.Spool,
        }
        for row in rows
    ]


async def count_spool_groups(db: AsyncSession, *, filters: list) -> int:
    """How many groups :func:`list_spool_groups` would page over — grouped
    mode's ``meta.total`` (a count of GROUPS, never of spools), from the same
    grouped subquery the list rides."""
    sub = _spool_groups_subquery(filters)
    return int((await db.execute(select(func.count()).select_from(sub))).scalar_one())


async def update_spool(db: AsyncSession, spool_id: int, spool_data: SpoolUpdate) -> Spool:
    """Update a spool.

    Raises SpoolNotFoundError when spool_id doesn't exist. Lets
    prepare_internal_spool_payload's ValueError propagate — the caller maps both
    to the appropriate response (route -> HTTP 404 / 400).
    """
    # Lazy import: _validate_family_id/_safe_autolink have other callers still in
    # inventory.py, and that module imports this one at module level to call
    # list_spools/update_spool — a top-level import here would be circular.
    from backend.app.api.routes.inventory import _safe_autolink, _validate_family_id

    result = await db.execute(select(Spool).where(Spool.id == spool_id))
    spool = result.scalar_one_or_none()
    if not spool:
        raise SpoolNotFoundError(spool_id)

    update_data = spool_data.model_dump(exclude_unset=True)
    update_data = await prepare_internal_spool_payload(db, update_data, set(spool_data.model_fields_set))
    # Auto-lock weight when user explicitly sets weight_used
    if "weight_used" in update_data and "weight_locked" not in update_data:
        update_data["weight_locked"] = True

    await _validate_family_id(db, update_data.get("filament_family_id"))
    for field, value in update_data.items():
        setattr(spool, field, value)

    await db.commit()
    # Re-link when the family / resolved filament_id changed (or on any save —
    # cheap and keeps links current with the spool's current preset).
    if "filament_family_id" in update_data or "slicer_filament" in update_data:
        await _safe_autolink(db, spool)
    result = await db.execute(select(Spool).options(selectinload(Spool.k_profiles)).where(Spool.id == spool_id))
    return result.scalar_one()
