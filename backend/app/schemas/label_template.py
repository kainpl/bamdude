"""Request and response shapes for label templates.

The design itself is validated by ``services.label_template.LabelTemplateSpec``
rather than restated here — one definition of what an element is, so the editor,
the renderer and the API cannot disagree about it.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from backend.app.services.label_template import LabelElement


class LabelTemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    width_mm: float = Field(gt=0, le=500)
    height_mm: float = Field(gt=0, le=500)
    shape: str = "rect"
    elements: list[LabelElement] = Field(default_factory=list)


class LabelTemplateOut(BaseModel):
    id: int
    name: str
    width_mm: float
    height_mm: float
    shape: str
    elements: list[LabelElement]
    #: Present for the four designs the label API names. A row that has one is
    #: read-only — see the duplicate endpoint.
    builtin_key: str | None
    is_builtin: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None


class LabelSheetOut(BaseModel):
    id: int
    name: str
    builtin_key: str | None
    page_size: str
    cell_width_mm: float
    cell_height_mm: float
    cols: int
    rows: int
    margin_top_mm: float
    margin_left_mm: float
    gap_x_mm: float
    gap_y_mm: float


class LabelPreviewRequest(BaseModel):
    """Render a design that has not been saved.

    ⚠️ The template travels in the body rather than by id on purpose: dragging
    an element in the editor must not have to save anything, and the picture has
    to come from the renderer that will do the printing rather than from a
    second one drawn in the browser.
    """

    template: LabelTemplateIn
    #: Which spool to fill it in with. Omitted renders the placeholders' own
    #: examples, which is what an empty inventory has to offer.
    spool_id: int | None = None
    #: The device's resolution. 203 dpi is 8 dots per millimetre.
    dots_per_mm: float = Field(default=8.0, gt=0, le=40)


class LabelTestPrintRequest(BaseModel):
    """Put the design currently on screen onto real stock.

    ⚠️ The template travels in the body, exactly as it does for the preview, and
    for the same reason: the point is to check a design *before* committing to
    it, which means before saving it.

    No spool is named. A test print is rendered from the placeholders' own
    examples — the same data the editor is previewing — so what comes out of the
    printer is what the screen was showing rather than a different label that
    happens to use the same design.
    """

    device_id: int
    template: LabelTemplateIn


__all__ = [
    "LabelPreviewRequest",
    "LabelTestPrintRequest",
    "LabelSheetOut",
    "LabelTemplateIn",
    "LabelTemplateOut",
]
