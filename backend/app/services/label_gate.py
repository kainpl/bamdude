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
import contextvars
import functools
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar
from urllib.parse import quote

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

#: Threads that draw labels — every renderer call of the server shares these two.
#: ⚠️ Not the loop's default executor: a renderer handed the worst design the
#: bounds allow works for many seconds and a closed connection does not stop a
#: thread, so a few label requests held every thread the rest of the server
#: works through (print gates opening a 3MF, upload hashing, preview disk I/O).
DRAW_WORKERS = 2
#: How long one draw may wait for a worker and work, in seconds. The worst
#: draws the bounds allow measured 9–15 s of one worker on the dev machine
#: (2026-11-16); this leaves a host several times slower room to finish, and is
#: what stops requests piling up behind two busy workers without end.
DRAW_CEILING_SECONDS = 120.0
DRAW_BUSY_DETAIL = "Label drawing is busy — try again in a moment"

_executor: ThreadPoolExecutor | None = None
_executor_guard = threading.Lock()


def _refuse(problems: list[str]) -> None:
    if problems:
        raise HTTPException(422, problems[0])


def refuse(problems: list[str]) -> None:
    """Answer the first of ``problems`` as a 422 — for a gate that lives beside its renderer."""
    _refuse(problems)


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


def _workers() -> ThreadPoolExecutor:
    global _executor
    with _executor_guard:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=DRAW_WORKERS, thread_name_prefix="label-draw")
        return _executor


async def draw(render: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a renderer on one of the label workers.

    ⚠️ The renderers are synchronous and pure, and five hundred labels with a QR
    each is seconds of them. On the event loop that is seconds in which no
    request, socket push or printer callback is served; on the shared thread
    pool it is a thread nothing else gets.

    A draw that is not finished within :data:`DRAW_CEILING_SECONDS` answers 503.
    One still waiting for a worker is taken out of the queue; one already being
    drawn runs to its end (a thread cannot be stopped) and its result is dropped.
    """
    call = functools.partial(contextvars.copy_context().run, render, *args, **kwargs)
    work = asyncio.get_running_loop().run_in_executor(_workers(), call)
    try:
        return await asyncio.wait_for(work, DRAW_CEILING_SECONDS)
    except TimeoutError:
        raise HTTPException(503, DRAW_BUSY_DETAIL) from None


#: What a header value may carry as typed: printable ASCII, except the escape itself.
_HEADER_SAFE = "".join(chr(code) for code in range(0x20, 0x7F) if chr(code) != "%")


def warning_headers(warnings: list[str]) -> dict[str, str]:
    """The ``X-Label-Warnings`` header of a response that still carries its label — every door's one writer.

    ⚠️ A header value is Latin-1 on the wire, and a warning quotes what would
    not encode: a barcode fed a Cyrillic spool name. Written as typed, that
    sentence failed the whole response with a 500 — the opposite of "the label
    arrives, with a warning". So whatever is not printable ASCII goes as
    percent-encoded UTF-8, and so does ``%`` itself: an all-ASCII warning reads
    exactly as before, and ``decodeURIComponent`` gives back any other. A line
    break is encoded too, so a payload cannot start a header of its own.
    """
    if not warnings:
        return {}
    return {
        "X-Label-Warnings": quote(" | ".join(warnings), safe=_HEADER_SAFE),
        "Access-Control-Expose-Headers": "X-Label-Warnings",
    }


async def shutdown() -> None:
    """Let the label workers go: queued draws are dropped, the ones running end by themselves.

    The next draw makes new workers, so a second lifespan in one process works.
    """
    global _executor
    with _executor_guard:
        executor, _executor = _executor, None
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=True)


__all__ = [
    "DRAW_BUSY_DETAIL",
    "DRAW_CEILING_SECONDS",
    "DRAW_WORKERS",
    "checked_design",
    "checked_picture",
    "checked_sheet",
    "design_of",
    "draw",
    "refuse",
    "sheet_of",
    "shutdown",
    "warning_headers",
]
