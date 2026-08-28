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
    #: One line saying what the label is for. Shown beside the name wherever a
    #: design is offered — the print dialog used to hard-code six of these.
    description: str = Field(default="", max_length=300)
    width_mm: float = Field(gt=0, le=500)
    height_mm: float = Field(gt=0, le=500)
    shape: str = "rect"
    #: ``driver`` (through the OS print driver, colour allowed) or ``thermal``
    #: (a one-bit label printer, where a colour element is refused rather than
    #: silently dropped). Defaults to driver — see m146.
    target: str = "driver"
    elements: list[LabelElement] = Field(default_factory=list)


class LabelTemplateOut(BaseModel):
    id: int
    name: str
    description: str = ""
    width_mm: float
    height_mm: float
    shape: str
    target: str
    elements: list[LabelElement]
    #: Present for the designs that shipped with BamDude. It resolves the names
    #: ``POST /inventory/labels`` accepts, and marks where a row came from.
    #:
    #: ⚠️ It no longer freezes the row — a seeded design is a starting point a
    #: person may redraw, which is why the editor offers every one of them.
    builtin_key: str | None
    is_builtin: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None


class LabelSheetIn(BaseModel):
    """A page of stock: the paper, the grid, and nothing about the design.

    ⚠️ No reference to a template, for the same reason ``LabelSheetSpec`` has
    none: a sheet that held a design would make that design undeletable and weld
    one paper geometry to one layout forever.
    """

    name: str = Field(min_length=1, max_length=120)
    page_size: str = "A4"
    cell_width_mm: float = Field(gt=0)
    cell_height_mm: float = Field(gt=0)
    cols: int = Field(gt=0)
    rows: int = Field(gt=0)
    margin_top_mm: float = Field(default=0.0, ge=0)
    margin_left_mm: float = Field(default=0.0, ge=0)
    gap_x_mm: float = Field(default=0.0, ge=0)
    gap_y_mm: float = Field(default=0.0, ge=0)


class LabelSheetPreviewRequest(BaseModel):
    """Lay a saved design onto an unsaved page geometry.

    ⚠️ The sheet travels in the body and the design by id, and the asymmetry is
    deliberate: the geometry is what you are editing, the design is what you are
    checking it against.
    """

    sheet: LabelSheetIn
    template_id: int


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
    #: A seeded geometry. Read-only for the same reason a built-in design is:
    #: an automation printing onto Avery 5160 for a year must not find the grid
    #: moved under it. Duplicate to get an editable copy.
    is_builtin: bool = False
    #: What about this grid does not fit its paper, in words. Empty is fine.
    overflow: list[str] = Field(default_factory=list)


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
