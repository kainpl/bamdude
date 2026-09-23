"""What the three list routes of the projects section share when paged.

Two kinds of sort key. A SQL key orders in the database and the page is a
LIMIT/OFFSET — that is where paging saves anything. A COMPUTED key is a figure
the row does not carry (progress, kits on the shelf, an order count): the
route loads the filtered set, computes the figures the way it always did, sorts
here and slices — the same cost as today's unpaged list, honestly. Both kinds
tiebreak on ``id`` so two pages never overlap or skip a row.

Spec: projects-lists-parity (vault). The contract itself — ``page`` as the
compat switch, the envelope — is the inventory list's (``routes/inventory.py``).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from backend.app.schemas.archive import PaginationMeta

T = TypeVar("T")


def page_meta(total: int, page: int, per_page: int, all_: bool) -> PaginationMeta:
    """The archive's meta; ``all`` is one page of everything (``per_page`` never 0)."""
    if all_:
        return PaginationMeta(total=total, current_page=1, per_page=total or 1, last_page=1)
    return PaginationMeta(
        total=total, current_page=page, per_page=per_page, last_page=max(1, math.ceil(total / per_page))
    )


def slice_page(rows: list[T], page: int, per_page: int, all_: bool) -> list[T]:
    if all_:
        return rows
    start = (page - 1) * per_page
    return rows[start : start + per_page]


@dataclass(frozen=True)
class SortSpec:
    """``sql``: key → (column, nulls_last); ``computed``: keys sorted in Python; ``default``: "key-dir"."""

    sql: dict[str, tuple[Any, bool]]
    computed: set[str] = field(default_factory=set)
    default: str = "name-asc"


def resolve_sort(spec: SortSpec, sort_by: str | None) -> tuple[str, str, bool]:
    """``(key, direction, is_computed)``; anything unknown is the default, never an error.

    The archive's convention: an old bookmark or a stale link must still open.
    """
    raw = sort_by or spec.default
    key, sep, direction = raw.rpartition("-")
    if not sep or direction not in ("asc", "desc") or (key not in spec.sql and key not in spec.computed):
        key, _, direction = spec.default.rpartition("-")
    return key, direction, key in spec.computed


def apply_sql_sort(query, spec: SortSpec, key: str, direction: str, id_column):
    """ORDER BY the key's column, NULLs last where the spec says so, then ``id`` ascending."""
    column, nulls_last = spec.sql[key]
    ordered = column.desc() if direction == "desc" else column.asc()
    if nulls_last:
        ordered = ordered.nulls_last()
    return query.order_by(ordered, id_column.asc())


def sort_computed(
    rows: Iterable[T], key_fn: Callable[[T], Any], direction: str, *, id_fn: Callable[[T], int]
) -> list[T]:
    """Stable sort by a computed figure; ``id`` decides ties whatever the direction.

    A missing figure (``None``) is never compared with a number — those rows
    go last in both directions, like the SQL keys' NULLS LAST.
    """
    rows = sorted(rows, key=id_fn)
    known = [r for r in rows if key_fn(r) is not None]
    unknown = [r for r in rows if key_fn(r) is None]
    known.sort(key=key_fn, reverse=(direction == "desc"))
    return known + unknown
