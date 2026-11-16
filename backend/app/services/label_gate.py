"""The question every door that takes label geometry asks before it draws.

Saving, previewing, printing and queueing all end in a renderer, and a renderer
handed an absurd design does not fail — it works, for minutes, or allocates a
picture the size of a wall. So the bounds in ``label_template`` are asked HERE,
by one pair of functions, and a door that builds its spec through them cannot
forget to.

⚠️ A refusal carries ONE sentence — the first problem — so the refusal catalog
can translate it at the boundary. The sheet editor's check door answers the
whole list.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, TypeVar

from fastapi import HTTPException
from pydantic import ValidationError

from backend.app.services.label_template import (
    LabelSheetSpec,
    LabelTemplateSpec,
    design_problems,
    picture_problems,
    sheet_problems,
)

_SHEET_FIELDS = (
    "name",
    "page_size",
    "cell_width_mm",
    "cell_height_mm",
    "cols",
    "rows",
    "margin_top_mm",
    "margin_left_mm",
    "gap_x_mm",
    "gap_y_mm",
)

T = TypeVar("T")


def _refuse(problems: list[str]) -> None:
    if problems:
        raise HTTPException(422, problems[0])


def checked_design(raw: dict[str, Any]) -> LabelTemplateSpec:
    """A design a renderer may be handed, or a 422 saying why not."""
    try:
        spec = LabelTemplateSpec(**raw)
    except ValidationError as error:
        raise HTTPException(422, f"Invalid template: {error}") from error
    _refuse(design_problems(spec))
    return spec


def checked_picture(spec: LabelTemplateSpec, dots_per_mm: float) -> None:
    """Refuse a raster whose longest side no printhead prints."""
    _refuse(picture_problems(spec, dots_per_mm))


def checked_sheet(raw: dict[str, Any]) -> LabelSheetSpec:
    """A page a renderer may be handed, or a 422 saying why not."""
    try:
        spec = LabelSheetSpec(**raw)
    except ValidationError as error:
        raise HTTPException(422, f"Invalid sheet: {error}") from error
    _refuse(sheet_problems(spec))
    return spec


def design_of(row: Any) -> LabelTemplateSpec:
    """A stored design, checked like a posted one — a row can predate the bounds."""
    return checked_design(
        {
            "name": row.name,
            "width_mm": row.width_mm,
            "height_mm": row.height_mm,
            "shape": row.shape,
            "elements": row.elements or [],
        }
    )


def sheet_of(row: Any) -> LabelSheetSpec:
    """A stored page, checked like a posted one."""
    return checked_sheet({field: getattr(row, field) for field in _SHEET_FIELDS})


async def draw(render: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a renderer in a thread.

    ⚠️ The renderers are synchronous and pure, and five hundred labels with a QR
    each is seconds of them. On the event loop that is seconds in which no
    request, socket push or printer callback is served.
    """
    return await asyncio.to_thread(render, *args, **kwargs)


__all__ = ["checked_design", "checked_picture", "checked_sheet", "design_of", "draw", "sheet_of"]
