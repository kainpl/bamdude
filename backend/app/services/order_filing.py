"""Filing a print under the order that needs it (spec pass 7, Decisions 1/2/4).

A print started anywhere other than the order page's plan block used to reach
the queue with an order id at best and never a LINE, and the plan block counts
only what is filed under a line — so "still needed: 5" stayed at 5 after four of
them had been queued (order 6, file W02453, 2026-09-04; repaired by hand in the
DB). Completed prints were already attributed implicitly, archive → plate →
product → line, so the asymmetry was on the queued side alone.

Three answers live here, and they are the same rule asked three ways:

* :func:`line_for_plate` — pure. WHICH line of a set the plate lands on.
* :func:`resolve_line_id` — what the three queue writers stamp on a row when the
  caller named an order and no line. :class:`LineFiler` is the same answer with
  its three reads hoisted, for the writer that asks it once per plate of a
  request.
* :func:`order_candidates` — what the dialogs offer, ranked, each candidate
  carrying how many prints of this plate its line still needs.

⚠️ **Nothing here asks about a printer.** Choosing the order a print belongs to
is a question about parts, exactly as planning is; routing is not dispatching.

⚠️ **This never overrides a line the caller named.** An explicit
``project_line_id`` is the operator's own answer and outranks anything derived
here — the writers call :func:`resolve_line_id` only where the line is absent.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.models.library import LibraryFile
from backend.app.models.product import Product, ProductPlate
from backend.app.models.project import Project
from backend.app.models.project_line import ProjectLine
from backend.app.schemas.project import PROJECT_PRIORITIES
from backend.app.services.order_metrics import line_accepts_materials
from backend.app.services.product_composition import PlateRecipe, plate_materials, recipe_for

#: An order in one of these takes no more work; every other status is open. The
#: two words are spelled here rather than derived from
#: ``schemas/project.PROJECT_STATUSES`` — that tuple is the VOCABULARY, and
#: "closed" is a judgement about each word in it that a fourth status would have
#: to be asked afresh. Keep the two in step by hand when the vocabulary grows.
CLOSED_STATUSES = ("completed", "cancelled")


@dataclass
class OrderCandidate:
    """One (order, line) pair this plate could be filed under.

    ``outstanding_prints`` is how many prints of THIS plate the line still needs
    after everything already finished, running or queued — the plan engine's own
    ``outstanding_before``, read per plate. ``0`` means the line is satisfied; it
    is still offered, because printing ahead is legitimate, and it is simply
    ranked below the lines that need something.

    ⚠️ **The shared thing is the outstanding number, not a row.** The plan block
    shows the plates its greedy pick chose, so a plate the block never picked is
    offered here with a number the block displays nowhere — that is not a
    disagreement, it is a plate the block had no reason to name.

    ``priority`` is the RANK of ``Project.priority`` in ``PROJECT_PRIORITIES``
    (higher is more urgent), not the stored word — the wire wants something
    sortable and the word is already on the order elsewhere.
    """

    project_id: int
    project_name: str
    project_line_id: int
    product_id: int
    product_name: str
    outstanding_prints: int
    priority: int
    deadline: datetime | None
    created_at: datetime


def line_for_plate(lines: list[ProjectLine], product_id: int, materials: set[str]) -> ProjectLine | None:
    """The one line of ``lines`` this plate lands on, or ``None``.

    The lines of the order that carry ``product_id``, filtered by the material
    rule (:func:`order_metrics.line_accepts_materials` — the same rule
    attribution applies to a finished archive). Exactly one survivor is the
    answer.

    ⚠️ **Several survivors is not "take the first" in general.** It is the first
    by ``(sort_order, id)`` ONLY when the plate carried materials AND those
    materials ruled at least one line out — i.e. the plate demonstrably speaks
    to this half of the order. Otherwise the answer is ``None``: two lines the
    plate cannot tell apart are two lines, and guessing between them files
    somebody's print against work nobody ordered. That is the whole reason this
    returns an optional rather than a best effort.

    ⚠️ A plate with no materials at all (unsliced, or metadata without
    filaments) matches no CONSTRAINED line — "we do not know what this prints
    in" is not "it prints in anything". Same reading an archive with no
    ``filament_type`` gets.
    """
    of_product = [line for line in lines if line.product_id == product_id]
    accepting = sorted(
        (line for line in of_product if line_accepts_materials(line, materials)),
        key=lambda line: (line.sort_order, line.id),
    )
    if len(accepting) == 1:
        return accepting[0]
    if accepting and materials and len(accepting) < len(of_product):
        return accepting[0]
    return None


def _prints_for_plate(recipe: PlateRecipe, outstanding: dict[int, int]) -> int:
    """How many prints of this plate the line still needs.

    ``outstanding`` is a ``LinePlan.outstanding_before`` — per COUNTED part,
    already net of what is finished, running and queued, and carrying only
    non-zero entries. The plate covers a part ``ceil(outstanding / yield)``
    times over, and the line needs the worst of those: a plate yielding one
    shade and two arms against 4 shades / 8 arms is four prints, not twelve.

    A part the plate does not yield contributes nothing — this number is about
    THIS plate, and what it cannot make is another row's business (the plan
    calls it ``unsatisfiable``). A part the line does not count is absent from
    ``outstanding`` for the same reason ``line_yield`` drops it.

    ⚠️ What this shares with the plan block is the OUTSTANDING NUMBER, per
    plate — not a row of the block. The block lists the plates its greedy pick
    chose; asked about a plate it did not pick, this still answers, and the block
    shows nothing to compare that answer with.
    """
    per_part = [
        math.ceil(n / recipe.yield_by_part[pid]) for pid, n in outstanding.items() if recipe.yield_by_part.get(pid)
    ]
    return max(per_part, default=0)


def _priority_rank(priority: str | None) -> int:
    """``PROJECT_PRIORITIES`` index — higher is more urgent.

    A word outside the vocabulary cannot claim precedence over one inside it, so
    it ranks with the lowest known priority rather than being mapped onto
    "normal", which would promote a typo above ``low``.
    """
    try:
        return PROJECT_PRIORITIES.index(priority or "")
    except ValueError:
        return 0


def _plates_by_product(plates: list[ProductPlate], plate_index: int) -> dict[int, ProductPlate]:
    """Per product, the plate this print lands on: the exact index, else the
    product's whole-file (``plate_index = 0``) plate, which claims every plate
    of its file. The same two-step attribution uses on an archive — and the
    reason a single-plate 3MF (one 0-row from the sync) ever meets a print
    carrying the slicer's index, 1."""
    whole_file = {plate.product_id: plate for plate in plates if plate.plate_index == 0}
    exact = {plate.product_id: plate for plate in plates if plate.plate_index == plate_index}
    return {**whole_file, **exact}


async def _plate_recipes_for_file(
    db: AsyncSession, file: LibraryFile, plate_index: int
) -> tuple[dict[int, PlateRecipe], dict[int, Product]]:
    """``product_id → recipe`` and ``product_id → product`` for every product
    holding this plate. One SELECT for the plates, one for the products; the
    file itself is already in hand, so the recipes are built without re-reading
    it (which is what ``recipes_for_products`` would do, for every OTHER plate
    of those products as well)."""
    plates = (await db.execute(select(ProductPlate).where(ProductPlate.library_file_id == file.id))).scalars().all()
    by_product = _plates_by_product(list(plates), plate_index)
    if not by_product:
        return {}, {}
    products = (
        (await db.execute(select(Product).options(selectinload(Product.parts)).where(Product.id.in_(by_product))))
        .scalars()
        .all()
    )
    recipes = {
        product.id: recipe_for(by_product[product.id], file.file_metadata, file.file_type, list(product.parts or []))
        for product in products
    }
    return recipes, {product.id: product for product in products}


async def order_candidates(db: AsyncSession, library_file_id: int, plate_index: int) -> list[OrderCandidate]:
    """The active orders this plate could be filed under, best first.

    An order qualifies when it has a line whose PRODUCT holds this plate and
    whose material accepts it — :func:`line_for_plate`, once per (order,
    product), so one order with two lines on two different products yields two
    candidates while one order with two indistinguishable lines yields none
    (there is nothing to propose that the writers would then refuse to stamp).

    Ranking, in order: lines that still need the plate first, then order
    priority (higher first), then deadline (earlier first, none last), then the
    older order. **The amount still needed does not rank** — sorting by it would
    starve either the big order or the nearly-finished one, depending on which
    way round it went.

    ⚠️ ``outstanding_prints`` comes from ``plan_for_order`` — the plan block's
    own machinery, run once per candidate ORDER (not per candidate line, and not
    per plate). Deriving the number here from the figures instead would be a
    second implementation of "outstanding", and what this endpoint owes the
    operator is the SAME outstanding number the block works from, asked about
    this plate. Asked about a plate the block's greedy pick never chose it still
    answers, and the block names no row to compare it with. The cost is one plan
    per active order that holds this product, which is the same computation
    opening any one of those order pages does.

    ⚠️ Archived customers are not filtered because there is no such thing:
    ``customers`` has no archived flag, and ``projects.archived`` was retired by
    m158. Status is the whole of "is this order still open".
    """
    # Imported here, not at module scope: ``plan_engine`` imports
    # :func:`line_for_plate` from this module for its own implicit branch, and a
    # module-level import in both directions is a cycle. The engine is the lower
    # layer of the two, so this is the edge that gives.
    from backend.app.services.plan_engine import plan_for_order

    file = (await db.execute(select(LibraryFile).where(LibraryFile.id == library_file_id))).scalar_one_or_none()
    if file is None:
        return []
    index = plate_index or 0
    recipes, products = await _plate_recipes_for_file(db, file, index)
    if not recipes:
        return []
    materials = plate_materials(file.file_metadata, index)

    projects = (
        (
            await db.execute(
                select(Project)
                .options(selectinload(Project.lines))
                .where(
                    Project.status.notin_(CLOSED_STATUSES),
                    Project.id.in_(select(ProjectLine.project_id).where(ProjectLine.product_id.in_(recipes))),
                )
            )
        )
        .scalars()
        .all()
    )

    out: list[OrderCandidate] = []
    for project in projects:
        lines = sorted(project.lines, key=lambda line: (line.sort_order, line.id))
        resolved: dict[int, int] = {}  # line id → product id
        for product_id in recipes:
            line = line_for_plate(lines, product_id, materials)
            if line is not None:
                resolved.setdefault(line.id, product_id)
        if not resolved:
            continue
        plan = await plan_for_order(db, project.id)
        outstanding_by_line = {lp.line_id: lp.outstanding_before for lp in (plan.lines if plan else [])}
        for line_id, product_id in resolved.items():
            out.append(
                OrderCandidate(
                    project_id=project.id,
                    project_name=project.name,
                    project_line_id=line_id,
                    product_id=product_id,
                    product_name=products[product_id].name,
                    outstanding_prints=_prints_for_plate(recipes[product_id], outstanding_by_line.get(line_id) or {}),
                    priority=_priority_rank(project.priority),
                    deadline=project.due_date,
                    created_at=project.created_at,
                )
            )

    out.sort(
        key=lambda c: (
            c.outstanding_prints == 0,  # needy first; satisfied lines follow in the same secondary order
            -c.priority,
            (c.deadline is None, c.deadline or datetime.min),
            c.created_at,
            c.project_id,
            c.project_line_id,
        )
    )
    return out


@dataclass(frozen=True)
class LineFiler:
    """One order's filing question, with everything it reads already loaded.

    The three rows :func:`resolve_line_id` needs — the order's lines, the library
    file, that file's product plates — depend on the ORDER and the FILE, never on
    the plate index. A writer that files one plate can happily load them per
    call; ``auto_queue_add`` fans out over the plates of a single request and
    would then repeat all three SELECTs per plate, so it builds this once and
    asks :meth:`for_plate` per plate instead.

    ⚠️ Frozen and read-only on purpose: it is a snapshot taken before a writer
    starts committing, and a commit may expire the instances it holds. Nothing
    here re-reads the session.
    """

    lines: list[ProjectLine]
    file: LibraryFile | None
    plates: list[ProductPlate]

    def for_plate(self, plate_index: int | None) -> int | None:
        """The unambiguous line for this plate index, or ``None``.

        ⚠️ ``plate_index`` is the SLICER's plate index as the queue tables store
        it (``plate_id``), where ``0``/``None`` means the whole file — NOT a
        ``ProductPlate.id``.

        ⚠️ Ambiguity is not only "two lines of one product": two PRODUCTS of this
        order each resolving a line of their own is equally unanswerable, and
        both end here as ``None``.
        """
        if self.file is None or not self.lines:
            return None
        index = plate_index or 0
        materials = plate_materials(self.file.file_metadata, index)
        ordered = sorted(self.lines, key=lambda line: (line.sort_order, line.id))
        resolved: set[int] = set()
        for product_id in _plates_by_product(self.plates, index):
            line = line_for_plate(ordered, product_id, materials)
            if line is not None:
                resolved.add(line.id)
        return next(iter(resolved)) if len(resolved) == 1 else None


async def line_filer(db: AsyncSession, *, project_id: int, library_file_id: int | None) -> LineFiler:
    """Load what filing this file under this order needs, once.

    Every "nothing to file against" case — no file named, an order with no lines,
    a file that is gone — comes back as an EMPTY filer rather than an exception,
    so a caller in a per-plate loop has one shape to handle and
    :meth:`LineFiler.for_plate` answers ``None`` for all of them.
    """
    if library_file_id is None:
        return LineFiler(lines=[], file=None, plates=[])
    lines = list((await db.execute(select(ProjectLine).where(ProjectLine.project_id == project_id))).scalars().all())
    if not lines:
        return LineFiler(lines=[], file=None, plates=[])
    file = (await db.execute(select(LibraryFile).where(LibraryFile.id == library_file_id))).scalar_one_or_none()
    if file is None:
        return LineFiler(lines=lines, file=None, plates=[])
    plates = list(
        (
            await db.execute(
                select(ProductPlate).where(
                    ProductPlate.library_file_id == library_file_id,
                    ProductPlate.product_id.in_({line.product_id for line in lines}),
                )
            )
        )
        .scalars()
        .all()
    )
    return LineFiler(lines=lines, file=file, plates=plates)


async def resolve_line_id(
    db: AsyncSession, *, project_id: int, library_file_id: int | None, plate_index: int | None
) -> int | None:
    """The unambiguous line of ``project_id`` for this plate, or ``None``.

    What the queue writers call when the caller named an order and no line, for
    ONE plate. ``None`` is an ordinary answer, not a failure: the row is written
    with ``project_line_id = NULL`` and the plan's implicit branch will make the
    same decision about it on every read.

    A caller filing several plates of the same file under the same order wants
    :func:`line_filer` instead — this is that, with the load per call.
    """
    filer = await line_filer(db, project_id=project_id, library_file_id=library_file_id)
    return filer.for_plate(plate_index)


__all__ = [
    "CLOSED_STATUSES",
    "LineFiler",
    "OrderCandidate",
    "line_filer",
    "line_for_plate",
    "order_candidates",
    "resolve_line_id",
]
