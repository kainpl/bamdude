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

The same rule, and the same reasons, answer the two read routes of a product:
:func:`table` — every combination with its availability, a page at a time — and
:func:`describe` — the state of one choice for the order dialogs, each value
marked available with the other groups unchanged and available in any
combination at all.

Reads only — no lock is taken here; the callers that refuse already hold the
product's gate, and the read routes write nothing.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable, Iterator, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.models.product import Product
from backend.app.models.product_variant import ProductVariantGroup, ProductVariantOption
from backend.app.services.line_composition import Unavailable, availability
from backend.app.services.line_config import LineConfigError, resolve_on
from backend.app.services.product_composition import recipes_for_products, sliced_part_ids

COMBINATION_UNAVAILABLE = "That combination is not available"
#: :func:`describe` tells whether a value is open in ANY combination only for a product with
#: at most this many — above it every ``available_anywhere`` is ``None`` (unknown).
MAX_ENUMERATED = 2000


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


def _reasons_of(product: Product) -> Callable[[list[Unavailable]], list[dict]]:
    """The product's reasons as name snapshots; a field the reason does not name is
    ``None``. The names are read once, for any number of combinations."""
    groups = {g.id: g.name for g in product.variant_groups}
    options = {o.id: o.name for g in product.variant_groups for o in g.options}
    parts = {p.id: p.name for p in product.parts}

    def reasons(unavailable: list[Unavailable]) -> list[dict]:
        return [
            {
                "code": u.code,
                "group": groups.get(u.group_id),
                "option": options.get(u.option_id),
                "part": parts.get(u.part_id),
            }
            for u in unavailable
        ]

    return reasons


def _reasons(product: Product, unavailable: list[Unavailable]) -> list[dict]:
    return _reasons_of(product)(unavailable)


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


Values = Sequence[ProductVariantOption]


def _axes(product: Product) -> list[tuple[ProductVariantGroup, Values]]:
    """The product's groups with their values in table order — groups by ``(position, id)``,
    values by ``(position, id)``."""
    return [
        (group, sorted(group.options, key=lambda o: (o.position, o.id)))
        for group in sorted(product.variant_groups, key=lambda g: (g.position, g.id))
    ]


def _total(axes: Sequence[tuple[ProductVariantGroup, Values]]) -> int:
    """How many combinations the groups make — a product of counts, never an enumeration;
    0 without groups."""
    return math.prod(len(values) for _group, values in axes) if axes else 0


def _from_index(values: Sequence[Values], start: int) -> Iterator[tuple[ProductVariantOption, ...]]:
    """The combinations from number ``start`` on, the LAST group fastest — lazily, and
    without walking the ones before it: ``start`` is read as one digit per group, then each
    group, from the last up, runs on past its digit under the fixed digits before it
    (``itertools.product`` of what is left). ``start`` must be below the total."""
    digits = []
    for options in reversed(values):
        start, digit = divmod(start, len(options))
        digits.append(digit)
    digits.reverse()
    last = len(values) - 1
    for k in range(last, -1, -1):
        head = tuple(values[i][digits[i]] for i in range(k))
        # The last group starts AT its digit; every group before it, past it.
        rest = values[k][digits[k] + (k < last) :]
        for tail in itertools.product(rest, *values[k + 1 :]):
            yield head + tail


def _choices_of(
    axes: Sequence[tuple[ProductVariantGroup, Values]], combination: Sequence[ProductVariantOption]
) -> dict[int, int]:
    """A combination as choices, ``group_id → option_id``."""
    return {group.id: option.id for (group, _values), option in zip(axes, combination, strict=True)}


async def table(db: AsyncSession, product: Product, *, page: int, page_size: int) -> dict:
    """``GET /products/{id}/combinations`` — one page of every combination of the product's
    values, in table order (:func:`_axes`, the last group fastest), each with its
    availability and its reasons — the very reasons the 409 refusal names.

    ``product`` as ``line_config.load_products`` loads it. The sliced set is read once,
    and only when the page holds anything: a page past the end is empty, and so is the
    table of a product without groups (п. 12), with nothing read."""
    axes = _axes(product)
    total = _total(axes)
    start = (page - 1) * page_size
    if start >= total:
        return {"total": total, "page": page, "items": []}
    parts = list(product.parts)
    sliced = await sliced_for(db, product.id)
    reasons_of = _reasons_of(product)
    items = []
    for combination in itertools.islice(_from_index([values for _g, values in axes], start), page_size):
        reasons = reasons_of(availability(parts, _choices_of(axes, combination), sliced))
        items.append(
            {
                "choices": [
                    {"group_id": group.id, "group": group.name, "option_id": option.id, "option": option.name}
                    for (group, _values), option in zip(axes, combination, strict=True)
                ],
                "available": not reasons,
                "reasons": reasons,
            }
        )
    return {"total": total, "page": page, "items": items}


async def describe(db: AsyncSession, product: Product, choices: Mapping[int, int]) -> dict:
    """``POST /products/{id}/combination`` — the state of one choice for the order dialogs.

    ``choices`` as a request sends them: a group left out takes its standard value
    (``line_config.resolve_on``; a value not of this product is :class:`LineConfigError`,
    422). The answer: is the combination available, its reasons — the 409 refusal's — and
    every value of every group, in table order, with ``available_here`` (this value, the
    other groups as chosen) and ``available_anywhere`` (in at least one available
    combination; ``None`` for a product of more than :data:`MAX_ENUMERATED` combinations).

    ``product`` as ``line_config.load_products`` loads it. The sliced set is read once; a
    product without groups (п. 12) is available once its choices are valid, with nothing
    read and no combination judged."""
    _key, chosen, _counts = resolve_on(product, choices)
    axes = _axes(product)
    if not axes:
        return {"available": True, "reasons": [], "options": []}
    parts = list(product.parts)
    sliced = await sliced_for(db, product.id)

    def is_open(candidate: Mapping[int, int]) -> bool:
        return not availability(parts, candidate, sliced)

    anywhere: set[int] | None = None
    if _total(axes) <= MAX_ENUMERATED:
        anywhere = set()
        every = sum(len(values) for _group, values in axes)
        for combination in itertools.product(*(values for _group, values in axes)):
            if is_open(_choices_of(axes, combination)):
                anywhere.update(option.id for option in combination)
                if len(anywhere) == every:
                    break
    reasons = _reasons(product, availability(parts, chosen, sliced))
    return {
        "available": not reasons,
        "reasons": reasons,
        "options": [
            {
                "group_id": group.id,
                "option_id": option.id,
                "available_here": is_open({**chosen, group.id: option.id}),
                "available_anywhere": None if anywhere is None else option.id in anywhere,
            }
            for group, values in axes
            for option in values
        ],
    }
