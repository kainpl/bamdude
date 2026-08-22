"""What a label template is, and what its text can say.

The layout is data: a size in millimetres and a list of boxes. Nothing here
draws anything — two backends do that, one to a 1-bit raster for a printer on
somebody's desk and one to a PDF for the driver they already have — and they
share this module so the layout exists once rather than twice.

Millimetres, not percentages and not pixels. A template is bound to a label
size, and millimetres are the only unit shared by a 203 dpi head, a 300 dpi head
and a sheet of paper. Percentages would let one template claim to fit every size
while looking right on none: text and QR codes do not scale linearly, so a
design that reads well at 50 × 30 is mush at 25 × 10.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator


class Placeholder(BaseModel):
    """One field a template's text can name, and what to show in a picker."""

    key: str
    label: str
    description: str
    example: str


#: The vocabulary a template's text can use.
#:
#: The first seventeen mirror ``frontend/src/utils/spoolName.ts`` field for
#: field — that is where this idea started, and the spool display-name setting
#: still interpolates them client-side for the inventory table. The last three
#: exist only on a label and have no meaning in a name.
PLACEHOLDERS: tuple[Placeholder, ...] = (
    Placeholder(
        key="id",
        label="DB ID",
        description="Internal database row ID — stable across renames, useful as a search anchor",
        example="42",
    ),
    Placeholder(key="brand", label="Brand", description="Manufacturer name", example="Polymaker"),
    Placeholder(key="material", label="Material", description="PLA, PETG, ABS, …", example="PLA"),
    Placeholder(key="subtype", label="Subtype", description="Basic, Matte, Silk, …", example="Matte"),
    Placeholder(key="color_name", label="Color", description="Human-readable colour name", example="Jade White"),
    Placeholder(
        key="slicer_filament_name",
        label="Slicer preset",
        description="Filament preset name as shown in the slicer",
        example="Polymaker PolyTerra PLA @Bambu Lab X1C",
    ),
    Placeholder(key="note", label="Note", description="Free-form user note", example="Kitchen shelf"),
    Placeholder(
        key="label_weight_g",
        label="Label weight (g)",
        description="Nominal weight of a full spool in grams",
        example="1000",
    ),
    Placeholder(
        key="label_weight_kg",
        label="Label weight (kg)",
        description="Nominal weight of a full spool in kilograms",
        example="1",
    ),
    Placeholder(key="remaining_g", label="Remaining (g)", description="Label weight minus used, grams", example="750"),
    Placeholder(
        key="remaining_kg",
        label="Remaining (kg)",
        description="Label weight minus used, kilograms",
        example="0.75",
    ),
    Placeholder(
        key="remaining_pct",
        label="Remaining (%)",
        description="Remaining weight as a percentage of the label weight",
        example="75%",
    ),
    Placeholder(key="color_hex", label="Color hex", description="#RRGGBB (alpha dropped)", example="#FF3300"),
    Placeholder(
        key="color_hex_all",
        # ⚠️ The swatch element defaults to this one, so it has to be a known
        # key: an unknown one survives resolution verbatim, and the swatch would
        # then try to draw a block the colour of the literal text
        # "{color_hex_all}". Found by writing the context builder, not by a test.
        label="All colour hexes",
        description="Every colour of a multi-colour spool, comma-separated, no leading hash",
        example="FF3300,FFFFFF",
    ),
    Placeholder(
        key="cost_per_kg",
        label="Cost per kg",
        description="Cost per kilogram (bare number, no currency symbol)",
        example="25",
    ),
    Placeholder(
        key="purchase_date",
        label="Purchase date",
        description="User-entered acquisition date (YYYY-MM-DD)",
        example="2026-04-15",
    ),
    Placeholder(
        key="filament_diameter",
        label="Filament diameter",
        description="1.75 or 2.85 (bare number, no unit)",
        example="1.75",
    ),
    Placeholder(key="lot", label="Lot", description="Position inside a purchase bundle / batch", example="3"),
    # ── Label-only, with no meaning in a spool's name ──
    Placeholder(
        key="display_name",
        label="Display name",
        description="The spool's name under your naming template, so a label agrees with the list",
        example="Polymaker PLA Ivory",
    ),
    Placeholder(
        key="deeplink",
        label="Deep link",
        description="URL a phone scan should open — this spool's row in BamDude",
        example="https://bamdude.example/inventory?spool=42",
    ),
    Placeholder(
        key="ean",
        label="EAN-13 payload",
        description="Twelve digits for a barcode element, in the range reserved for internal use",
        example="200000000042",
    ),
)

_TOKEN = re.compile(r"\{([a-z_0-9]+)\}")
_KNOWN = frozenset(p.key for p in PLACEHOLDERS)

#: Characters that are punctuation between two values rather than a value.
#: ⚠️ Only a word made ENTIRELY of these counts — "PLA-CF" and "1,000" are
#: values that happen to contain one.
_SEPARATORS = frozenset("·-–—/|,;:")


def _is_separator(word: str) -> bool:
    return bool(word) and all(character in _SEPARATORS for character in word)


def _drop_orphan_separators(words: list[str]) -> list[str]:
    """Remove separators with nothing on one side of them."""
    kept: list[str] = []
    for word in words:
        if _is_separator(word) and (not kept or _is_separator(kept[-1])):
            continue
        kept.append(word)
    while kept and _is_separator(kept[-1]):
        kept.pop()
    return kept


def resolve(text: str, context: dict[str, str]) -> str:
    """Substitute ``{key}`` tokens against ``context``.

    ⚠️ **An unknown key survives verbatim.** That is the same choice the spool
    display-name setting already makes, and for the same reason: a typo then
    shows up in the preview as ``{colour_name}`` rather than collapsing into a
    silent gap somebody only notices on printed stock.

    A known key with no value becomes empty, and the surrounding whitespace
    collapses — so ``{brand} {subtype}`` on a spool with no subtype does not
    print a trailing space.

    ⚠️ **A separator left holding nothing goes with it.** ``{brand} · {material}``
    on a spool with no brand used to print "· PLA", and a date-and-lot line on a
    spool with neither printed a lone "·". A separator is punctuation between
    two things; with one of them gone it is debris, and debris on a shelf label
    reads as a bug in the data.
    """

    def swap(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in _KNOWN:
            return match.group(0)
        return context.get(key, "")

    return " ".join(_drop_orphan_separators(_TOKEN.sub(swap, text).split()))


class _Box(BaseModel):
    """Where an element sits, in millimetres from the top-left of the label."""

    x_mm: float
    y_mm: float
    w_mm: float = Field(gt=0)
    h_mm: float = Field(gt=0)


class TextElement(_Box):
    type: Literal["text"]
    content: str
    size_mm: float = Field(default=3.0, gt=0)
    bold: bool = False
    italic: bool = False
    align: Literal["left", "center", "right"] = "left"
    valign: Literal["top", "middle", "bottom"] = "top"
    #: ``shrink`` reduces the size until the text fits, down to a floor of about
    #: a millimetre — below that it stops being type and becomes a grey smudge on
    #: a thermal head — and truncates with an ellipsis from there. ``clip`` keeps
    #: the authored size and cuts, for anyone who would rather have consistent
    #: type than complete text.
    fit: Literal["shrink", "clip"] = "shrink"


class QrElement(_Box):
    type: Literal["qr"]
    content: str


class BarcodeElement(_Box):
    type: Literal["barcode"]
    content: str
    #: ⚠️ Written out rather than taken from ``label_barcode.SUPPORTED`` —
    #: ``Literal`` needs literal values. A test asserts the two agree, because a
    #: symbology the schema accepts and the renderer refuses is a template that
    #: saves and then fails to print.
    symbology: Literal["code128", "code39", "ean13", "ean8", "upca", "itf"] = "code128"
    human_readable: bool = False


class SwatchElement(_Box):
    """A block of the spool's colour.

    ⚠️ **Drawn on paper and skipped on a thermal head.** A colour block on a
    black-and-white head prints as a muddy grey that says nothing, which is why
    the PDF renderer already dropped it in monochrome mode. Keeping the element
    but ignoring it on a 1-bit canvas means one template serves both without the
    author having to keep two.

    It exists at all because the PDF layouts carry one today, and losing it
    would be losing the thing people find a spool by.
    """

    type: Literal["swatch"]
    #: Resolves to one or more comma-separated hex colours, no leading hash.
    content: str = "{color_hex_all}"
    #: The outline the colour is poured into. ⚠️ Multi-colour banding survives
    #: every shape — a two-colour spool is two colours whatever the outline, and
    #: painting only the first is the small lie this element exists to avoid.
    shape: Literal["rect", "circle", "rounded"] = "rect"


LabelElement = Annotated[TextElement | QrElement | BarcodeElement | SwatchElement, Field(discriminator="type")]


class LabelTemplateSpec(BaseModel):
    """One label design: how big it is and what sits on it."""

    name: str = Field(min_length=1, max_length=120)
    width_mm: float = Field(gt=0, le=500)
    height_mm: float = Field(gt=0, le=500)
    #: ⚠️ Stored, and only ``rect`` is drawn for now. Niimbot sells round stock —
    #: 31 × 31 mm is a circular sticker — where a rectangular design loses its
    #: corners. One column now costs nothing; finding out later costs a
    #: migration and a re-seed.
    shape: Literal["rect", "round"] = "rect"
    #: Which printer this design is drawn for. ``driver`` may use colour — it
    #: goes out as PDF and could be landing on an inkjet or a laser; ``thermal``
    #: goes to a one-bit head where colour cannot survive.
    target: Literal["driver", "thermal"] = "driver"
    elements: list[LabelElement] = Field(default_factory=list)

    @model_validator(mode="after")
    def _colour_belongs_to_a_colour_printer(self) -> LabelTemplateSpec:
        """Refuse a colour element on a design declared thermal.

        ⚠️ **Refused here rather than dropped at print time**, which is the
        change m149 makes. The raster backend still skips a swatch it is given,
        so designs predating this keep printing as they always did — but a
        design being SAVED as thermal is one somebody is drawing now, and
        letting them place a block that will never appear is letting them design
        around something that is not there.
        """
        if self.target != "thermal":
            return self
        offenders = [index for index, element in enumerate(self.elements) if element.type == "swatch"]
        if offenders:
            raise ValueError(
                f"A thermal design cannot carry a colour swatch (element {offenders[0] + 1}): a one-bit "
                f"printhead has no colour to print it with. Use the hex-code placeholder in a text box, "
                f"or set this design's target to a driver printer."
            )
        return self


#: Paper is cut to a tolerance and so is the stock on it; a tenth of a
#: millimetre over is a rounding artefact, not an overflow.
_FIT_TOLERANCE_MM = 0.5


class LabelSheetSpec(BaseModel):
    """A page of labels: the paper, the grid, and nothing about the design.

    ⚠️ **No reference to a template.** The tempting shape — "this sheet holds
    that label" — makes a template undeletable while a sheet looks at it, and
    welds one paper geometry to one design forever. A sheet states its cell
    size; printing takes a sheet plus a template that fits the cell.
    """

    name: str = Field(min_length=1, max_length=120)
    page_size: Literal["A4", "A5", "letter"]
    cell_width_mm: float = Field(gt=0)
    cell_height_mm: float = Field(gt=0)
    cols: int = Field(gt=0)
    rows: int = Field(gt=0)
    margin_top_mm: float = Field(ge=0)
    margin_left_mm: float = Field(ge=0)
    gap_x_mm: float = Field(ge=0)
    gap_y_mm: float = Field(ge=0)

    @property
    def per_page(self) -> int:
        return self.cols * self.rows


def sheet_overflow(sheet: LabelSheetSpec) -> list[str]:
    """What about this grid does not fit its page, in words.

    ⚠️ A sheet whose grid is wider than its paper prints its last column half
    off the edge, and nothing on screen says so — the discovery costs a sheet of
    adhesive stock. Returned as a list rather than raised so the editor can show
    both axes at once and keep drawing while you fix them.

    ⚠️ N cells have N-1 gaps. Counting a trailing gap refuses sheets that fit,
    which is the same class of wrong as accepting ones that do not, only
    quieter.
    """
    from backend.app.services.label_renderer import PAGE_SIZES_MM

    page_w, page_h = PAGE_SIZES_MM[sheet.page_size]
    problems: list[str] = []

    used_w = sheet.margin_left_mm + sheet.cols * sheet.cell_width_mm + (sheet.cols - 1) * sheet.gap_x_mm
    if used_w > page_w + _FIT_TOLERANCE_MM:
        problems.append(
            f"The grid is {used_w:.1f}mm wide but {sheet.page_size} is {page_w:.1f}mm — "
            f"reduce the columns, the cell width, the gap or the left margin."
        )

    used_h = sheet.margin_top_mm + sheet.rows * sheet.cell_height_mm + (sheet.rows - 1) * sheet.gap_y_mm
    if used_h > page_h + _FIT_TOLERANCE_MM:
        problems.append(
            f"The grid is {used_h:.1f}mm in height but {sheet.page_size} is {page_h:.1f}mm — "
            f"reduce the rows, the cell height, the gap or the top margin."
        )

    return problems


def orientation(width_mm: float, height_mm: float, head_mm: float) -> Literal["as_drawn", "rotated"]:
    """Which way the label goes through the printhead.

    ⚠️ **Derived, never stored.** An earlier reading of the reference had this
    as a property of the printer — a B1 prints "top" — which the community's own
    label presets contradict: they carry it per label, with 40 × 12 as "left"
    and 50 × 30 as "top" on the same machine. The rule under both is simply that
    the side which fits the head is the side that crosses it.

    A derived value cannot be set wrong, and this one prints every label
    sideways when it is.
    """
    if width_mm <= head_mm:
        return "as_drawn"
    if height_mm <= head_mm:
        return "rotated"
    # Fits no way round. Answered rather than refused: the renderer clips and
    # the caller warns, which leaves somebody something to look at and a reason.
    return "as_drawn"


__all__ = [
    "PLACEHOLDERS",
    "BarcodeElement",
    "LabelElement",
    "LabelSheetSpec",
    "LabelTemplateSpec",
    "Placeholder",
    "QrElement",
    "SwatchElement",
    "TextElement",
    "orientation",
    "resolve",
]
