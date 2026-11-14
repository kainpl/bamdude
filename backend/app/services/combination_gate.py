"""A new product line takes only an available combination (spec
product-part-variant-combinations, п. 10–14).

The rule is :func:`line_composition.availability` — pure, beside the kit's one
reader. This module reads what it needs (the product's sliced parts) and refuses:

* every line constructor goes through ``line_config.seed_line``, which asks
  :func:`ensure_available` for a ``product`` line — ``line_intake.add_lines``
  (order creation, one line, a batch) and ``order_from_files`` (a catalog
  product's order) at one point (п. 13);
* ``line_config.set_configuration`` asks it when the line's CHOICE changes,
  before its dry run — a change of counts alone is not checked.

Never asked (п. 14): saved lines (they keep a combination that closed later),
an order's copy, a ``parts`` line, and finished-stock positions — a receipt, a
stocktake or an assembly is a physical fact. A product without variant groups
is never asked either (п. 12): no query is made for it.

Reads only — no lock is taken here; the callers already hold the product's gate.
"""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.models.product import Product
from backend.app.services.line_composition import Unavailable, availability
from backend.app.services.line_config import LineConfigError
from backend.app.services.product_composition import recipes_for_products, sliced_part_ids

COMBINATION_UNAVAILABLE = "That combination is not available"


class CombinationUnavailable(LineConfigError):
    """A combination an order may not take — 409 with the reasons as name snapshots
    (``code``, ``group``, ``option``, ``part``), so the frontend composes the sentence
    in the reader's language."""

    def __init__(self, detail: str, reasons: list[dict]) -> None:
        super().__init__(detail, 409)
        self.reasons = reasons

    def payload(self) -> dict:
        """The structured refusal a route answers with (only ``message`` is translated)."""
        return {"error": "combination_unavailable", "message": str(self), "reasons": list(self.reasons)}


def refusal_detail(error: Exception) -> str | dict:
    """The ``detail`` of a route's answer to a line refusal: a closed combination's
    payload — the exception's own or the one a ``LineIntakeError`` carries — else
    the sentence."""
    # Imported here: line_intake imports this module.
    from backend.app.services.line_intake import LineIntakeError

    if isinstance(error, CombinationUnavailable):
        return error.payload()
    if isinstance(error, LineIntakeError) and error.payload is not None:
        return error.payload
    return str(error)


async def sliced_for(db: AsyncSession, product_id: int) -> set[int]:
    """The product's parts some sliced plate yields — of a file outside the trash
    (``recipes_for_products``, which wants the plates and parts loaded).

    No ``populate_existing``: it would reset the product's lazy relationships the
    caller still reads (its variant groups), and a reset one is a lazy load —
    ``MissingGreenlet`` under async. What this load adds is only what was not loaded."""
    product = (
        await db.execute(
            select(Product)
            .options(selectinload(Product.plates), selectinload(Product.parts))
            .where(Product.id == product_id)
        )
    ).scalar_one()
    return sliced_part_ids((await recipes_for_products(db, [product]))[product.id])


def _reasons(product: Product, unavailable: list[Unavailable]) -> list[dict]:
    """The reasons as name snapshots; a field the reason does not name is ``None``."""
    groups = {g.id: g.name for g in product.variant_groups}
    options = {o.id: o.name for g in product.variant_groups for o in g.options}
    parts = {p.id: p.name for p in product.parts}
    return [
        {
            "code": u.code,
            "group": groups.get(u.group_id),
            "option": options.get(u.option_id),
            "part": parts.get(u.part_id),
        }
        for u in unavailable
    ]


async def ensure_available(db: AsyncSession, product: Product, choices: Mapping[int, int]) -> None:
    """Refuse (:class:`CombinationUnavailable`) a combination the product cannot make.

    ``product`` as ``line_config`` loads it — parts, and variant groups with their
    options; ``choices`` the FULL resolved choices, a value for every group. A
    product without groups returns at once, without a query (п. 12)."""
    if not product.variant_groups:
        return
    reasons = availability(list(product.parts), choices, await sliced_for(db, product.id))
    if reasons:
        raise CombinationUnavailable(COMBINATION_UNAVAILABLE, _reasons(product, reasons))
