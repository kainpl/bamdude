"""Farm-wide filament-usage history — the filter/sort/facet core behind
``GET /inventory/usage`` (the Inventory page's History view, 2026-09-01).

Same contract as ``inventory_service``'s server-driven spool list, deliberately:
every param feeds :func:`build_usage_filters`, and that SAME list of conditions
drives the page query, the count, the totals AND the facets. Nothing about this
list is decided in the browser — a farm with a year of prints has six figures of
usage rows, and the last farm-wide feed of this table was a 5000-row download
the forecast rewrite deleted for exactly that reason.

⚠️ **Both joins are OUTER.** ``spool_id`` is a NOT NULL FK with ``ON DELETE
CASCADE``, but this codebase never sets ``PRAGMA foreign_keys = ON``, so on
SQLite a deleted spool leaves its usage rows behind. An inner join would make
those orphans vanish from the one screen that could show somebody they exist —
and the grams they carry are still the grams the archives think were printed.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import String, and_, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.printer import Printer
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.services.inventory_service import display_name_expr

# What ``q`` matches. The print's own name first — that is what an operator
# actually remembers — then the spool's identity and the machine, so "sunlu
# black" and a printer name both find their rows without a separate control.
# The spool's id and lot are here for the same reason they are on the spool
# list: they are the numbers written on the reel, and the default naming
# template mentions neither, so the composed name below cannot cover them.
#
# ⚠️ A FLOOR, not the whole search — the spool's composed display name is added
# per request in :func:`build_usage_filters`, because it depends on a setting.
_SEARCH_COLUMNS = (
    SpoolUsageHistory.print_name,
    Spool.brand,
    Spool.material,
    Spool.color_name,
    Spool.subtype,
    Printer.name,
    cast(Spool.id, String),
    cast(Spool.lot, String),
)

# Newest first, because a history is read from its end. ``id`` breaks the tie:
# a runout close-out lands in the same second as the print's own rows (the same
# tiebreak the per-spool endpoint has always used), and this list PAGES — rows
# sharing a timestamp have no defined order between two queries otherwise.
_DEFAULT_ORDER = (SpoolUsageHistory.created_at.desc(), SpoolUsageHistory.id.desc())

# The composite sort — one header, three columns — mirroring the spool list's
# ``display_name`` key. The client shows one "Spool" column built from these.
_SPOOL_SORT_COLUMNS = (
    func.coalesce(Spool.material, ""),
    func.coalesce(Spool.brand, ""),
    func.coalesce(Spool.color_name, ""),
)


def _with_joins(query):
    """The two OUTER joins every query here shares — see the module docstring
    for why they are outer and not inner."""
    return query.outerjoin(Spool, Spool.id == SpoolUsageHistory.spool_id).outerjoin(
        Printer, Printer.id == SpoolUsageHistory.printer_id
    )


def _usage_sort_columns() -> dict[str, Any]:
    """The sortable columns, by the key the client sends.

    Nullable TEXT is coalesced to ``""`` so NULL placement stops being
    dialect-dependent (SQLite defaults NULLS FIRST on asc, PostgreSQL NULLS
    LAST — the same request would order differently per backend otherwise).
    Same reasoning, and the same deliberate absence of ``lower()``, as
    ``inventory_service._spool_sort_columns``.
    """
    return {
        "created_at": SpoolUsageHistory.created_at,
        "weight_used": SpoolUsageHistory.weight_used,
        "percent_used": SpoolUsageHistory.percent_used,
        "cost": func.coalesce(SpoolUsageHistory.cost, 0),
        "status": SpoolUsageHistory.status,
        "print_name": func.coalesce(SpoolUsageHistory.print_name, ""),
        "printer": func.coalesce(Printer.name, ""),
    }


def _usage_order_by(sort_by: str | None) -> list:
    """Resolve ``<column>_asc``/``<column>_desc`` into ORDER BY clauses.

    Permissive fallback on a missing or unrecognized value — same convention as
    every other server-driven list here (a stale bookmark should still open the
    page, not 400). ALWAYS ends with the id tiebreak.
    """
    tiebreak = SpoolUsageHistory.id.desc()

    if not sort_by:
        return list(_DEFAULT_ORDER)

    sort_key, _, sort_dir = sort_by.rpartition("_")
    if sort_dir not in ("asc", "desc"):
        return list(_DEFAULT_ORDER)

    if sort_key == "spool":
        cols = _SPOOL_SORT_COLUMNS
        clauses = [c.asc() for c in cols] if sort_dir == "asc" else [c.desc() for c in cols]
        return [*clauses, tiebreak]

    column = _usage_sort_columns().get(sort_key)
    if column is None:
        return list(_DEFAULT_ORDER)

    return [column.asc() if sort_dir == "asc" else column.desc(), tiebreak]


def build_usage_filters(
    *,
    statuses: list[str] | None = None,
    printer_id: str | None = None,
    spool_id: int | None = None,
    material: str | None = None,
    brand: str | None = None,
    archived: str | None = None,
    assigned: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    q: str | None = None,
    display_name_template: str | None = None,
) -> list:
    """The shared WHERE-clause list for list/count/totals/facets.

    Every param is optional and additive (AND'ed together); omitting one applies
    no filter for that dimension.

    ``printer_id`` is a ``str`` because it carries a sentinel: ``"__none__"`` is
    "recorded against no printer at all" (the same convention ``location_id``
    uses on the spool list). ``date_from``/``date_to`` are absolute instants,
    NOT calendar days — the client turns the days a person picked into UTC
    boundaries before sending them, because only the client knows that person's
    timezone. ``date_to`` is EXCLUSIVE, so the client sends the start of the day
    after and nothing is lost to an off-by-one at midnight.

    ``archived`` and ``assigned`` ask about the SPOOL behind the row, and are the
    same two questions the spool list asks — but with a third answer the spool
    list does not have: **omitting them means "all", and that is the default**.
    A history is the record of what was burned, and retiring or unloading the
    reel afterwards does not un-burn it; a view that hid those rows unasked
    would make its own totals disagree with the archives. So the filters exist
    because an operator wants to ask, and they start switched off.

    ⚠️ ``archived="active"`` requires the spool to still EXIST — an orphaned row
    (see the module docstring) has no spool to be active, and a bare
    ``archived_at IS NULL`` would quietly count NULL-because-deleted as
    NULL-because-not-archived.
    """
    filters: list = []

    if statuses:
        filters.append(SpoolUsageHistory.status.in_(statuses))

    if printer_id == "__none__":
        filters.append(SpoolUsageHistory.printer_id.is_(None))
    elif printer_id is not None:
        filters.append(SpoolUsageHistory.printer_id == int(printer_id))

    if spool_id is not None:
        filters.append(SpoolUsageHistory.spool_id == spool_id)

    if material:
        filters.append(Spool.material == material)

    if brand:
        filters.append(Spool.brand == brand)

    if archived == "active":
        filters.append(and_(Spool.id.isnot(None), Spool.archived_at.is_(None)))
    elif archived == "archived":
        filters.append(Spool.archived_at.isnot(None))

    # ⚠️ Correlated on the usage row's own ``spool_id``, NOT on the outer-joined
    # ``Spool.id`` — that one is NULL for an orphan, which would make every
    # orphan silently "assigned to nothing" by accident rather than by fact.
    _assignment_exists = (
        select(SpoolAssignment.spool_id).where(SpoolAssignment.spool_id == SpoolUsageHistory.spool_id).exists()
    )
    if assigned == "assigned":
        filters.append(_assignment_exists)
    elif assigned == "unassigned":
        filters.append(~_assignment_exists)

    if date_from is not None:
        filters.append(SpoolUsageHistory.created_at >= date_from)

    if date_to is not None:
        filters.append(SpoolUsageHistory.created_at < date_to)

    if q and q.strip():
        # Tokenised ilike: each token must match AT LEAST ONE column (OR), and
        # every token must match something (AND across tokens) — so "sunlu bl"
        # finds a SUNLU Black row, exactly like the spool list's ``q``.
        #
        # The spool's composed DISPLAY NAME is one of those columns, for the
        # same reason it is on the spool list: this view shows the name the
        # operator's template builds, and a search that could not match what it
        # shows is the bug that was just fixed over there. The template arrives
        # as a param so this builder stays synchronous and free of a settings
        # read — the route resolves it once per request.
        composed = display_name_expr(display_name_template)
        columns = (*_SEARCH_COLUMNS, composed) if composed is not None else _SEARCH_COLUMNS
        for token in q.strip().split():
            term = f"%{token}%"
            filters.append(or_(*(col.ilike(term) for col in columns)))

    return filters


async def list_usage(
    db: AsyncSession,
    *,
    filters: list,
    sort_by: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[tuple[SpoolUsageHistory, Spool | None, Printer | None]]:
    """One page of usage rows, each already carrying the spool it charged and
    the machine that burned it.

    The spool and printer travel WITH the row rather than being looked up
    client-side in a separate list: this page shows rows for spools the
    inventory list is not showing (archived, filtered out, deleted), and a
    lookup that missed would render a row with no identity at all.

    The whole ``Printer`` comes back, not just its name, because the client
    labels a RETIRED printer generically ("Printer 5 (Archived)") rather than by
    a name that may since have been reused — see ``utils/printerLabel.ts``. That
    rule needs the archived flag beside the name, from the same query.
    """
    query = _with_joins(select(SpoolUsageHistory, Spool, Printer).select_from(SpoolUsageHistory))
    query = query.where(*filters).order_by(*_usage_order_by(sort_by))

    if offset:
        query = query.offset(offset)
    if limit is not None:
        query = query.limit(limit)

    return [(row, spool, printer) for row, spool, printer in (await db.execute(query)).all()]


async def count_usage(db: AsyncSession, *, filters: list) -> int:
    """How many rows :func:`list_usage` would page over — its ``total``."""
    query = _with_joins(select(func.count()).select_from(SpoolUsageHistory)).where(*filters)
    return int((await db.execute(query)).scalar_one())


async def usage_totals(db: AsyncSession, *, filters: list) -> dict[str, float | None]:
    """Grams and money across the WHOLE filter, not the page on screen.

    This is the question a history view exists to answer ("what did this month
    cost me"), and answering it from the visible rows would quietly mean "what
    did these fifty cost". ``cost`` stays ``None`` when no matching row carries
    one — a 0.00 would read as "free" rather than "unpriced".
    """
    query = _with_joins(
        select(
            func.coalesce(func.sum(SpoolUsageHistory.weight_used), 0.0),
            func.sum(SpoolUsageHistory.cost),
        ).select_from(SpoolUsageHistory)
    ).where(*filters)
    weight, cost = (await db.execute(query)).one()
    return {"weight_used": float(weight or 0.0), "cost": None if cost is None else float(cost)}


async def usage_facets(db: AsyncSession, *, filters: list) -> dict[str, list]:
    """Distinct filter-dropdown values, one cheap DISTINCT per dimension.

    The route passes an EMPTY filter list on purpose: the options are computed
    over every usage row, not over the current selection, so narrowing by
    printer does not make the material you were about to pick disappear from its
    own dropdown. The signature still takes ``filters`` to keep this on the same
    contract as list/count/totals.

    ``printers`` carries the id, the name AND the archived flag: a printer can
    be renamed or retired after it burned the filament, and the client labels a
    retired one generically instead of by a name that may since have been reused
    (``utils/printerLabel.ts``). All three have to come from the same query that
    found the id, not from a live printer list the row may have outlived.
    """
    statuses_q = (
        _with_joins(select(SpoolUsageHistory.status).select_from(SpoolUsageHistory))
        .where(*filters)
        .distinct()
        .order_by(SpoolUsageHistory.status)
    )
    statuses = list((await db.execute(statuses_q)).scalars().all())

    printers_q = (
        _with_joins(select(SpoolUsageHistory.printer_id, Printer.name, Printer.archived).select_from(SpoolUsageHistory))
        .where(*filters, SpoolUsageHistory.printer_id.isnot(None))
        .distinct()
        .order_by(Printer.name)
    )
    printers = [
        {"id": int(pid), "name": name, "archived": bool(archived)}
        for pid, name, archived in (await db.execute(printers_q)).all()
        if pid is not None
    ]

    materials_q = (
        _with_joins(select(Spool.material).select_from(SpoolUsageHistory))
        .where(*filters, Spool.material.isnot(None), Spool.material != "")
        .distinct()
        .order_by(Spool.material)
    )
    materials = list((await db.execute(materials_q)).scalars().all())

    brands_q = (
        _with_joins(select(Spool.brand).select_from(SpoolUsageHistory))
        .where(*filters, Spool.brand.isnot(None), Spool.brand != "")
        .distinct()
        .order_by(Spool.brand)
    )
    brands = list((await db.execute(brands_q)).scalars().all())

    return {"statuses": statuses, "printers": printers, "materials": materials, "brands": brands}
