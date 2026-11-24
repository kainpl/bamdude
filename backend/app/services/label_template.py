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

import math
import re
from collections.abc import Iterable
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, ValidationError, model_validator


class Placeholder(BaseModel):
    """One field a template's text can name, and what to show in a picker."""

    key: str
    label: str
    description: str
    example: str


#: The vocabulary a template's text can use.
#:
#: The first nineteen mirror ``frontend/src/utils/spoolName.ts`` field for
#: field — that is where this idea started, and the spool display-name setting
#: still interpolates them client-side for the inventory table. The rest exist
#: only on a label (``LABEL_ONLY_PLACEHOLDERS``): three with no meaning in a
#: name, and three a name was never taught.
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
    Placeholder(
        key="material_number",
        label="Material number",
        description="Your own article code for the filament",
        example="15",
    ),
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
    Placeholder(
        key="storage_location",
        label="Storage location",
        description="Where the spool is kept — the shelf, box or dry cabinet entered for it",
        example="Shelf B",
    ),
    Placeholder(
        key="nozzle_temp",
        label="Nozzle temperature",
        description="The spool's nozzle temperature range, already written out with its unit",
        example="220–240 °C",
    ),
    Placeholder(
        key="added_date",
        label="Date added",
        description="The day the spool was added to the inventory (YYYY-MM-DD)",
        example="2026-04-15",
    ),
)

_TOKEN = re.compile(r"\{([a-z_0-9]+)\}")
_KNOWN = frozenset(p.key for p in PLACEHOLDERS)

#: The keys that only mean something on a LABEL. A spool's *name* has no use for
#: a URL, a barcode payload, or its own resolved self — the registry already
#: separates them with a comment, and this makes that line readable by code.
#: ``inventory_service`` composes the naming template in SQL so the inventory
#: search can match what the list SHOWS, and it needs to know where the naming
#: vocabulary stops.
#:
#: ⚠️ ``storage_location``, ``nozzle_temp`` and ``added_date`` are here by
#: decision, not by nature (owner, 2026-11-16): a name COULD say where a spool
#: lives, but the naming template is also composed in the browser
#: (``spoolName.ts``) and in SQL, and a key only this module knows would make
#: the list, the search and the label disagree.
LABEL_ONLY_PLACEHOLDERS = frozenset(
    {"display_name", "deeplink", "ean", "storage_location", "nozzle_temp", "added_date"}
)

#: The naming half of :data:`PLACEHOLDERS` — what a spool-name template may use.
NAMING_PLACEHOLDERS: tuple[Placeholder, ...] = tuple(p for p in PLACEHOLDERS if p.key not in LABEL_ONLY_PLACEHOLDERS)

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
    #: ``wrap`` uses additional lines inside the same box at the authored size.
    fit: Literal["shrink", "clip", "wrap"] = "shrink"


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
    description: str = Field(default="", max_length=300)
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

        ⚠️ **Refused here rather than dropped at print time.** A design being
        SAVED as thermal is one somebody is drawing now, and letting them place
        a block that will never appear is letting them design around something
        that is not there.

        ⚠️ ``RasterCanvas.swatch`` still SKIPS a swatch it is handed rather than
        refusing, and that is not a leftover: ``resolve_template`` does not
        filter on target, so a caller naming a driver design's ``template_id``
        for a device print reaches the raster with a colour block in hand. Losing
        the block is the right answer there; losing the whole job is not.
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
#:
#: ⚠️ **The one slack for every "does it fit"** — a grid on its page, a design
#: in a cell, a design on the stock loaded in a desk printer. There used to be
#: three for the cell alone (the dialog's half millimetre, the print's
#: millionth, the preview's none), so the dialog offered what the print refused.
FIT_TOLERANCE_MM = 0.5


def fits_within(width_mm: float, height_mm: float, space_width_mm: float, space_height_mm: float) -> bool:
    """Whether a design of this size goes into a space of that size.

    ⚠️ The ONLY place this is decided. The print dialog does not repeat the
    arithmetic — the sheet and device lists tell it which designs fit — so what
    it offers and what a print accepts cannot drift apart again.

    Only LARGER is refused: a design smaller than its space prints with a
    margin, which is fine.
    """
    return width_mm <= space_width_mm + FIT_TOLERANCE_MM and height_mm <= space_height_mm + FIT_TOLERANCE_MM


def fitting_ids(
    designs: Iterable[tuple[int, float, float]], space_width_mm: float, space_height_mm: float
) -> list[int]:
    """The ids of the ``(id, width, height)`` designs that fit this space."""
    return [
        design_id
        for design_id, width_mm, height_mm in designs
        if fits_within(width_mm, height_mm, space_width_mm, space_height_mm)
    ]


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


#: ── What a door refuses before anything is drawn ──
#:
#: ⚠️ **Checked by functions, not by ``Field`` limits on the models above.** A
#: row saved before these existed (or written past the API) still has to load:
#: the catalogue lists it and the editor opens it so somebody can fix it. A
#: field limit would turn such a row into a 500 on every read; the functions
#: let every door that DRAWS — save, preview, print, queue — refuse it in words.
MAX_SHEET_COLS = 50
MAX_SHEET_ROWS = 100
#: A cell smaller than this holds no label anybody can read, and is how a page
#: comes to hold a hundred million of them.
MIN_CELL_SIDE_MM = 5.0
#: No paper is longer; every length of a sheet is refused past it.
MAX_SHEET_LENGTH_MM = 2000.0
#: An element may hang off the label (it is clipped, with a warning) but not be
#: larger than the largest label, nor sit further away than one.
MAX_ELEMENT_SIDE_MM = 500.0
MAX_ELEMENT_OFFSET_MM = 500.0
#: 50 mm capitals are ~140 pt — a poster, not a label.
MAX_FONT_SIZE_MM = 50.0
MAX_ELEMENTS = 100
#: All the type on one design, added up. ⚠️ The shrink-to-fit loop walks down
#: from the authored size a dot at a time, loading a face at each step, so this
#: sum is the work a design can ask of a renderer: a hundred boxes of 50 mm type
#: measured 36 s of a worker thread at 8 dots/mm. Ten lines of 5 mm type are 50.
MAX_TYPE_TOTAL_MM = 500.0
#: The longest side of a rendered picture, in dots: a 500 mm label at the
#: 8 dots/mm every supported head prints at.
MAX_PICTURE_SIDE_DOTS = 4000
#: Labels in one export of pictures. Measured 2026-11-16: a 100 × 50 mm label at
#: 600 dpi is ~70 ms of a worker thread and ~65 kB, so this many is ~15 s and 13 MB.
MAX_LABEL_PICTURES = 200
#: Every picture of one export added up, in megapixels. ⚠️ The count alone let
#: 200 labels of 169 × 169 mm at 600 dpi through — 3 187 MP, a minute or two of
#: one worker thread. Drawing costs by the dot, so the dots are what is bounded:
#: the worst 500 MP measured ~9 s on the dev machine (2026-11-16; 31 such labels,
#: or 200 of 63 × 63 mm). Fifty 100 × 150 mm shipping labels at 600 dpi are
#: 418 MP, two hundred of them at 300 dpi 105 MP.
MAX_EXPORT_MEGAPIXELS = 500

#: The sentences those refusals are made of. A named map so the refusal catalog
#: (``scripts/api_error_catalog.py``, ``FORWARDED_MESSAGE_MAPS``) can see them:
#: they reach the wire one at a time, and the sheet editor shows them as typed.
LABEL_GEOMETRY_MESSAGES = {
    "not_finite": "Every measurement must be an ordinary number.",
    "sheet_cols": "A sheet takes at most {} columns.",
    "sheet_rows": "A sheet takes at most {} rows.",
    "sheet_cell": "A cell must be at least {} mm on each side.",
    "sheet_length": "No measurement of a sheet may be more than {} mm.",
    "sheet_shape": (
        "Columns and rows must be whole numbers of at least 1, cell sizes more than zero, "
        "and margins and gaps not negative."
    ),
    "sheet_paper": "'{}' is not a paper size BamDude knows.",
    "grid_wide": (
        "The grid is {}mm wide but {} is {}mm — reduce the columns, the cell width, the gap or the left margin."
    ),
    "grid_high": (
        "The grid is {}mm in height but {} is {}mm — reduce the rows, the cell height, the gap or the top margin."
    ),
    "elements": "A design takes at most {} elements.",
    "element_not_finite": "Element {} has a measurement that is not an ordinary number.",
    "element_side": "Element {} is more than {} mm on a side.",
    "element_offset": "Element {} sits more than {} mm away from the label.",
    "element_font": "Element {} has type larger than {} mm.",
    "type_total": "The type sizes of one design may add up to at most {} mm.",
    "picture": "At {} dots per millimetre this design is more than {} dots on a side — lower the resolution.",
    "picture_dpi": "At {} dpi this design is more than {} dots on a side — choose a lower resolution.",
    "picture_count": "One export takes at most {} labels as pictures — select fewer.",
    "picture_budget": (
        "At {} dpi these {} labels come to more than {} megapixels — select fewer or choose a lower resolution."
    ),
}


def _finite(*values: float) -> bool:
    return all(math.isfinite(value) for value in values)


def design_problems(spec: LabelTemplateSpec) -> list[str]:
    """What about this design no renderer should be handed, in words."""
    say = LABEL_GEOMETRY_MESSAGES
    if not _finite(spec.width_mm, spec.height_mm):
        return [say["not_finite"]]
    if len(spec.elements) > MAX_ELEMENTS:
        return [say["elements"].format(MAX_ELEMENTS)]

    problems: list[str] = []
    for index, element in enumerate(spec.elements, start=1):
        numbers = [element.x_mm, element.y_mm, element.w_mm, element.h_mm]
        if element.type == "text":
            numbers.append(element.size_mm)
        if not _finite(*numbers):
            problems.append(say["element_not_finite"].format(index))
            continue
        if element.w_mm > MAX_ELEMENT_SIDE_MM or element.h_mm > MAX_ELEMENT_SIDE_MM:
            problems.append(say["element_side"].format(index, f"{MAX_ELEMENT_SIDE_MM:g}"))
        if abs(element.x_mm) > MAX_ELEMENT_OFFSET_MM or abs(element.y_mm) > MAX_ELEMENT_OFFSET_MM:
            problems.append(say["element_offset"].format(index, f"{MAX_ELEMENT_OFFSET_MM:g}"))
        if element.type == "text" and element.size_mm > MAX_FONT_SIZE_MM:
            problems.append(say["element_font"].format(index, f"{MAX_FONT_SIZE_MM:g}"))
    if not problems and _type_total(spec) > MAX_TYPE_TOTAL_MM:
        problems.append(say["type_total"].format(f"{MAX_TYPE_TOTAL_MM:g}"))
    return problems


def _type_total(spec: LabelTemplateSpec) -> float:
    return sum(element.size_mm for element in spec.elements if element.type == "text")


def picture_problems(spec: LabelTemplateSpec, dots_per_mm: float) -> list[str]:
    """Whether a raster of this design at this resolution is a picture or a denial of service.

    Asked after :func:`design_problems`, so every number here is finite.
    """
    longest = max(
        [spec.width_mm, spec.height_mm, _type_total(spec)]
        + [side for element in spec.elements for side in (element.w_mm, element.h_mm)]
    )
    if longest * dots_per_mm > MAX_PICTURE_SIDE_DOTS + 1e-6:
        return [LABEL_GEOMETRY_MESSAGES["picture"].format(f"{dots_per_mm:g}", MAX_PICTURE_SIDE_DOTS)]
    return []


def colour_picture_problems(spec: LabelTemplateSpec, dpi: float, *, labels: int = 1) -> list[str]:
    """Whether this design as colour pictures at this resolution is an export or a denial of service.

    ⚠️ Not :func:`picture_problems`: that one bounds the 1-bit raster, whose
    shrink-to-fit loop works dot by dot and so counts the type as well. A colour
    picture is the PDF page rasterised, and only the page's own sides cost.
    """
    say = LABEL_GEOMETRY_MESSAGES
    if labels > MAX_LABEL_PICTURES:
        return [say["picture_count"].format(MAX_LABEL_PICTURES)]
    if max(spec.width_mm, spec.height_mm) * dpi / 25.4 > MAX_PICTURE_SIDE_DOTS + 0.5:
        return [say["picture_dpi"].format(f"{dpi:g}", MAX_PICTURE_SIDE_DOTS)]
    dots = round(spec.width_mm * dpi / 25.4) * round(spec.height_mm * dpi / 25.4)
    if labels > 1 and dots * labels > MAX_EXPORT_MEGAPIXELS * 1_000_000:
        return [say["picture_budget"].format(f"{dpi:g}", labels, MAX_EXPORT_MEGAPIXELS)]
    return []


def sheet_problems(sheet: LabelSheetSpec) -> list[str]:
    """Everything about this page that stops it being printed, in words.

    The bounds first — an absurd grid is refused before its size is even
    multiplied out — then :func:`sheet_overflow`, which is only arithmetic on
    numbers known to be sane.
    """
    say = LABEL_GEOMETRY_MESSAGES
    lengths = (
        sheet.cell_width_mm,
        sheet.cell_height_mm,
        sheet.margin_top_mm,
        sheet.margin_left_mm,
        sheet.gap_x_mm,
        sheet.gap_y_mm,
    )
    if not _finite(*lengths):
        return [say["not_finite"]]

    problems: list[str] = []
    if sheet.cols > MAX_SHEET_COLS:
        problems.append(say["sheet_cols"].format(MAX_SHEET_COLS))
    if sheet.rows > MAX_SHEET_ROWS:
        problems.append(say["sheet_rows"].format(MAX_SHEET_ROWS))
    if sheet.cell_width_mm < MIN_CELL_SIDE_MM or sheet.cell_height_mm < MIN_CELL_SIDE_MM:
        problems.append(say["sheet_cell"].format(f"{MIN_CELL_SIDE_MM:g}"))
    if max(lengths) > MAX_SHEET_LENGTH_MM:
        problems.append(say["sheet_length"].format(f"{MAX_SHEET_LENGTH_MM:g}"))
    return problems or sheet_overflow(sheet)


def draft_sheet_problems(raw: dict[str, Any]) -> list[str]:
    """The same answer for a sheet that is still being typed.

    ⚠️ A draft may not be a sheet yet: clearing a field to type another passes
    through zero, and a column count can be "3.5" for a keystroke. Those are
    moments of editing, so they get a sentence here instead of the validation
    error a save would answer with. Everything that IS a sheet goes on to
    :func:`sheet_problems` — one validator, asked earlier.
    """
    from backend.app.services.label_renderer import PAGE_SIZES_MM

    say = LABEL_GEOMETRY_MESSAGES
    numbers = [raw.get(field) for field in _SHEET_NUMBERS]
    if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in numbers):
        return [say["not_finite"]]
    if raw.get("page_size") not in PAGE_SIZES_MM:
        return [say["sheet_paper"].format(raw.get("page_size"))]
    try:
        sheet = LabelSheetSpec(**{**raw, "name": raw.get("name") or "-"})
    except ValidationError:
        return [say["sheet_shape"]]
    return sheet_problems(sheet)


_SHEET_NUMBERS = (
    "cell_width_mm",
    "cell_height_mm",
    "cols",
    "rows",
    "margin_top_mm",
    "margin_left_mm",
    "gap_x_mm",
    "gap_y_mm",
)


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
    if used_w > page_w + FIT_TOLERANCE_MM:
        problems.append(LABEL_GEOMETRY_MESSAGES["grid_wide"].format(f"{used_w:.1f}", sheet.page_size, f"{page_w:.1f}"))

    used_h = sheet.margin_top_mm + sheet.rows * sheet.cell_height_mm + (sheet.rows - 1) * sheet.gap_y_mm
    if used_h > page_h + FIT_TOLERANCE_MM:
        problems.append(LABEL_GEOMETRY_MESSAGES["grid_high"].format(f"{used_h:.1f}", sheet.page_size, f"{page_h:.1f}"))

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
    "LABEL_GEOMETRY_MESSAGES",
    "PLACEHOLDERS",
    "BarcodeElement",
    "LabelElement",
    "LabelSheetSpec",
    "LabelTemplateSpec",
    "Placeholder",
    "QrElement",
    "SwatchElement",
    "TextElement",
    "design_problems",
    "draft_sheet_problems",
    "fits_within",
    "fitting_ids",
    "orientation",
    "colour_picture_problems",
    "picture_problems",
    "resolve",
    "sheet_problems",
]
