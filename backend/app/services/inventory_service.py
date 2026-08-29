"""Spool list/update logic extracted from `api/routes/inventory.py` (behavior-preserving)
so it can be called directly — not only through HTTP — by the cloud portal's remote-op
registry (see docs/superpowers/sdd/2026-08-28-cloud-portal-phase2-remote-inventory-agent).
"""

from typing import Any

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

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
        filters.append(Spool.category == category)

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
