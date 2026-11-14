"""One road for every new order line (spec workshop-add-to-order, rules 6–7, 11–12).

The add-to-order dialog's batch (``POST /projects/{id}/lines/batch``), the old
single-line route and an order created with its lines all come through
:func:`add_lines`, so a line is born the same way whatever door it came in by:
its row, its configuration (``line_config.seed_line``), its stock and its
journal entry, in the caller's transaction.

Stock is taken only by an ACTIVE order, and only by a product line: ready units
of the line's configuration first (``finished_stock.reserve_for_line``), kits of
free parts for the rest (``part_stock.reserve_for_line``). ``stock: "auto"``
asks :func:`stock_pick.suggest`; explicit numbers are taken as far as the shelf
goes — the writers CLAMP, never refuse, so a shelf that moved while the dialog
was open gives less and the answer says how much (``asked`` / ``got``).

A refusal of any line raises :class:`LineIntakeError` with that line's own
sentence (a closed combination also with its structured ``payload``); the route
turns it into the HTTP answer and the request's transaction rolls back — a batch
is added whole or not at all.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.library import LibraryFile
from backend.app.models.product import Product
from backend.app.models.project import Project
from backend.app.models.project_line import ProjectLine
from backend.app.models.user import User
from backend.app.schemas.project import BatchPartsLineIn, BatchPlateLineIn, BatchProductLineIn, BatchStockIn
from backend.app.services import (
    combination_gate,
    finished_stock,
    line_config,
    order_from_files,
    order_journal,
    part_stock,
    stock_pick,
)
from backend.app.services.product_gate import product_gate


class LineIntakeError(Exception):
    """A line the batch refuses; ``status`` is the HTTP answer. ``payload`` — a
    structured refusal the route answers with instead of the sentence (a closed
    combination: ``{"error": "combination_unavailable", "message", "reasons"}``)."""

    def __init__(self, detail: str, status: int, payload: dict | None = None) -> None:
        super().__init__(detail)
        self.status = status
        self.payload = payload


@dataclass(slots=True)
class Intake:
    line: ProjectLine
    asked_finished: int = 0
    got_finished: int = 0
    asked_kits: int = 0
    got_kits: int = 0


_PLATE_ERRORS: dict[type, tuple[str, int]] = {
    order_from_files.FileNotFound: ("Library file not found", 404),
    order_from_files.NotPlannable: ("Only 3MF files can be planned", 400),
    order_from_files.PlateNotFound: ("Plate not found", 404),
}


async def _products_of(db: AsyncSession, specs: Sequence, visible: Callable[[LibraryFile], bool] | None) -> list[int]:
    """Phase 1 (WS-13 E1 BL3 / BL8 б): the product of every spec, in order. A named
    product must exist (read only); a plate's one-off product is made here — every
    file locked, ascending, before the first product is inserted, and all of it
    before the batch takes its gates."""
    product_ids: list[int | None] = []
    plates: list[tuple[int, tuple]] = []
    for i, spec in enumerate(specs):
        if isinstance(spec, BatchPlateLineIn):
            try:
                plates.append(
                    (i, await order_from_files.plate_of(db, spec.library_file_id, spec.plate_index, visible=visible))
                )
            except order_from_files.OrderFromFilesError as e:
                detail, status = _PLATE_ERRORS.get(type(e), ("Plate not found", 404))
                raise LineIntakeError(detail, status) from e
            product_ids.append(None)
        else:
            if await db.get(Product, spec.product_id) is None:
                raise LineIntakeError("Product not found", 404)
            product_ids.append(spec.product_id)
    if plates:
        made = await order_from_files.plate_products(db, [plate for _i, plate in plates])
        for (i, _plate), product in zip(plates, made, strict=True):
            product_ids[i] = product.id
    return [pid for pid in product_ids if pid is not None]


def _new_line(spec, product_id: int, sort_order: int) -> ProjectLine:
    common = {"material": spec.material, "color": spec.color, "note": spec.note, "sort_order": sort_order}
    if isinstance(spec, BatchPlateLineIn):
        return ProjectLine(product_id=product_id, quantity=spec.copies, mode="product", **common)
    if isinstance(spec, BatchPartsLineIn):
        return ProjectLine(product_id=product_id, quantity=1, mode="parts", **common)
    return ProjectLine(product_id=product_id, quantity=spec.quantity, mode="product", **common)


async def _asked(db: AsyncSession, line: ProjectLine, spec) -> tuple[int, int]:
    """What the line asks of the shelf: the server's proposal, or the operator's numbers."""
    stock = getattr(spec, "stock", "auto")
    if isinstance(stock, BatchStockIn):
        return stock.from_finished, stock.from_kits
    choices = dict(getattr(spec, "choices", None) or {})
    counts = dict(getattr(spec, "part_counts", None) or {})
    [answer] = await stock_pick.suggest(db, [stock_pick.PickRequest(line.product_id, choices, counts, line.quantity)])
    return answer.from_finished, answer.from_kits


async def add_lines(
    db: AsyncSession,
    project: Project,
    specs: Sequence[BatchProductLineIn | BatchPartsLineIn | BatchPlateLineIn],
    *,
    actor: User | None,
    visible: Callable[[LibraryFile], bool] | None = None,
) -> list[Intake]:
    """Add every line in ``specs`` to ``project`` with its configuration, stock and
    journal entry; never commits. ``visible`` is the caller's library ownership
    gate for plate lines — a file it rejects is the same 404 as a missing one."""
    sort_order = max((ln.sort_order for ln in project.lines), default=-1) + 1
    # Phase 1 — the products (one-off plate products made here); phase 2 — the gates
    # of all of them, ascending, before any line is inserted or configured (WS-13 E1
    # BL3); phase 3 — the lines, their configurations, stock and journal.
    product_ids = await _products_of(db, specs, visible)
    await product_gate(db, product_ids)
    from backend.app.services.order_recount import ensure_attribution_can_change

    await ensure_attribution_can_change(db, project.id, product_ids)
    # The order as it stands behind the gates (BL2): deleted meanwhile is the same 404 as
    # a missing one, and only an order still active takes stock.
    status = await db.scalar(select(Project.status).where(Project.id == project.id))
    if status is None:
        raise LineIntakeError("Project not found", 404)
    active = status == "active"
    out: list[Intake] = []
    for spec, product_id in zip(specs, product_ids, strict=True):
        line = _new_line(spec, product_id, sort_order)
        sort_order += 1
        project.lines.append(line)
        # Flushed first so the configuration, the movements and the journal have an id to name.
        await db.flush()
        choices = getattr(spec, "choices", None) or None
        counts = getattr(spec, "part_counts", None) or None
        try:
            await line_config.seed_line(db, line, choices=choices, counts=counts)
        except combination_gate.CombinationUnavailable as e:
            raise LineIntakeError(str(e), e.status, e.payload()) from e
        except line_config.LineConfigError as e:
            raise LineIntakeError(str(e), e.status) from e
        intake = Intake(line)
        if active and line.mode == "product":
            try:
                intake.asked_finished, intake.asked_kits = await _asked(db, line, spec)
            except stock_pick.StockPickError as e:
                raise LineIntakeError(str(e), e.status) from e
            intake.got_finished = await finished_stock.reserve_for_line(
                db, line, min(intake.asked_finished, line.quantity), actor=actor
            )
            kits = min(intake.asked_kits, line.quantity - intake.got_finished)
            if kits > 0:
                await part_stock.reserve_for_line(db, line, kits, created_by=actor.id if actor else None)
                intake.got_kits = await part_stock.reserved_units_for_line(db, line)
        product = await db.get(Product, line.product_id)
        await order_journal.record(
            db,
            project.id,
            "line_added",
            {
                "line_id": line.id,
                "product": product.name if product else None,
                "quantity": line.quantity,
                "from_finished": intake.got_finished,
                "from_stock": intake.got_kits,
            },
            actor=actor,
        )
        out.append(intake)
    return out
