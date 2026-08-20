"""The templates and sheets a fresh install starts with.

Four of them exist to answer the names the API has always taken —
``box_40x30`` and friends — so a caller that knows nothing about templates
notices nothing. Two more exist because a label printer cannot use any of those
four: three are wider than a B1's printhead.

⚠️ **The built-in layouts are derived, not transcribed.** The geometry below is
computed from the same formulas ``label_renderer`` uses today rather than
copied out as numbers, so the seed cannot drift from the layout it replaces
through a typo, and a new size costs one line.

⚠️ **They will still not be pixel-identical.** The old layout adjusts itself in
ways a movable design cannot reproduce: it drops rows that would collide, sizes
the QR against a floor, and omits the swatch in monochrome. That difference is
the one thing about this change existing users will see, and it belongs in the
CHANGELOG rather than in a comment.
"""

from __future__ import annotations

from typing import Any

#: Margin the fixed layouts leave, in millimetres (1.2 mm, their ``pad``).
_PAD = 1.2

#: Point sizes the roomy layout uses, converted once.
_PT = 25.4 / 72
_BRAND_MM = round(8 * _PT, 2)
_SUB_MM = round(7 * _PT, 2)
_HEX_MM = round(6.5 * _PT, 2)
_NAME_MM = round(9 * _PT, 2)
_ID_MM = round(16 * _PT, 2)

#: Leading between rows, in millimetres — the fixed layout uses 1.2–1.5 points.
_GAP = 0.45


def _roomy_elements(width_mm: float, height_mm: float) -> list[dict[str, Any]]:
    """The layout every built-in uses, as boxes.

    ⚠️ All four built-ins land here. The renderer's other layout, "tight",
    triggers below 20 mm and the shortest template is 25.4 — it has been
    unreachable since the 30 × 15 preset was withdrawn.
    """
    inner_w = width_mm - 2 * _PAD
    inner_h = height_mm - 2 * _PAD

    swatch_w = min(inner_w * 0.18, inner_h, 16.0)
    # The 12 mm floor is #1870: below it a QR module falls under two dots at
    # 203 dpi and the code bleeds into itself.
    qr = min(max(inner_w * 0.20, 12.0), inner_h, 18.0, inner_w * 0.45)

    text_x = _PAD + swatch_w + 1.5
    qr_x = width_mm - _PAD - qr
    text_w = qr_x - text_x - 1.5

    rows: list[dict[str, Any]] = []
    y = _PAD
    for content, size, bold, italic in (
        ("{brand}", _BRAND_MM, True, False),
        ("{material} · {subtype}", _SUB_MM, False, False),
        ("{color_hex}", _HEX_MM, False, False),
        ("{display_name}", _NAME_MM, True, False),
        ("{note}", _HEX_MM, False, True),
    ):
        rows.append(
            {
                "type": "text",
                "x_mm": round(text_x, 2),
                "y_mm": round(y, 2),
                "w_mm": round(text_w, 2),
                "h_mm": size,
                "content": content,
                "size_mm": size,
                "bold": bold,
                "italic": italic,
                # ⚠️ `clip`, not the `shrink` default. The layout these replace
                # truncates at a fixed size, and shrinking instead inverts the
                # type hierarchy on real data: a long brand shrinks below the
                # short material line under it, so the label reads as though
                # the material were the more important field. Caught by looking
                # at a render rather than by any test.
                "fit": "clip",
            }
        )
        y += size + _GAP

    return [
        {
            "type": "swatch",
            "x_mm": _PAD,
            "y_mm": _PAD,
            "w_mm": round(swatch_w, 2),
            "h_mm": round(inner_h, 2),
            "content": "{color_hex_all}",
        },
        *rows,
        {
            # Anchored to the bottom, big and bold — the field somebody reads
            # across a room.
            "type": "text",
            "x_mm": round(text_x, 2),
            "y_mm": round(height_mm - _PAD - _ID_MM, 2),
            "w_mm": round(text_w, 2),
            "h_mm": _ID_MM,
            "content": "#{id}",
            "size_mm": _ID_MM,
            "bold": True,
            "fit": "clip",
        },
        {
            "type": "qr",
            "x_mm": round(qr_x, 2),
            "y_mm": round(_PAD + (inner_h - qr) / 2, 2),
            "w_mm": round(qr, 2),
            "h_mm": round(qr, 2),
            "content": "{deeplink}",
        },
    ]


def _builtin(key: str, name: str, width_mm: float, height_mm: float) -> dict[str, Any]:
    return {
        "builtin_key": key,
        "name": name,
        "width_mm": width_mm,
        "height_mm": height_mm,
        "shape": "rect",
        "elements": _roomy_elements(width_mm, height_mm),
    }


#: The four names ``POST /inventory/labels`` has always accepted for a single
#: label. Their sizes are the ones ``label_renderer`` declares.
BUILTIN_TEMPLATES: list[dict[str, Any]] = [
    _builtin("ams_holder_74x33", "AMS holder 74 × 33", 74.0, 33.0),
    _builtin("ams_holder_75x55", "AMS holder 75 × 55", 75.0, 55.0),
    _builtin("box_40x30", "Box label 40 × 30", 40.0, 30.0),
    _builtin("box_62x29", "Box label 62 × 29", 62.0, 29.0),
]

#: The other two names describe a page, not a label.
#:
#: ⚠️ No template is named here. A sheet that pointed at one would make that
#: design undeletable and weld one paper geometry to one layout; it states its
#: cell size, and printing takes a sheet plus a template that fits it.
BUILTIN_SHEETS: list[dict[str, Any]] = [
    {
        "builtin_key": "avery_5160",
        "name": "Avery 5160 (US Letter, 30 per sheet)",
        "page_size": "letter",
        "cell_width_mm": 66.675,
        "cell_height_mm": 25.4,
        "cols": 3,
        "rows": 10,
        "margin_top_mm": 12.7,
        "margin_left_mm": 4.76,
        "gap_x_mm": 3.175,
        "gap_y_mm": 0.0,
    },
    {
        "builtin_key": "avery_l7160",
        "name": "Avery L7160 (A4, 21 per sheet)",
        "page_size": "A4",
        "cell_width_mm": 63.5,
        "cell_height_mm": 38.1,
        "cols": 3,
        "rows": 7,
        "margin_top_mm": 15.15,
        "margin_left_mm": 7.0,
        "gap_x_mm": 2.5,
        "gap_y_mm": 0.0,
    },
]


#: What a B1 can actually put down, against 50 mm stock.
#:
#: ⚠️ The template is the *label*, not the printable area — 50 × 30 stock is a
#: 50 × 30 template, because that is what the cassette says and what the
#: catalogue records. But the head is 48 mm and aligns to one edge, so anything
#: past that simply is not printed. A starter has to keep its content inside it.
_B1_PRINTABLE_MM = 48.0


def _device_elements(
    width_mm: float, height_mm: float, *, barcode: bool, printable_mm: float | None = None
) -> list[dict[str, Any]]:
    """A layout for stock a thermal label printer can actually take.

    No swatch: on a one-bit head a colour block is a smear, so the hex line
    carries the colour instead. The QR sits right, the text runs left, and the
    barcode — where there is room for one — takes the bottom.

    ``printable_mm`` narrows where content may go without changing how big the
    label is.
    """
    pad = 1.5
    usable_mm = min(width_mm, printable_mm or width_mm)
    qr = min(height_mm - 2 * pad, usable_mm * 0.3, 12.0)
    if barcode:
        qr = min(qr, (height_mm - 2 * pad) * 0.55)

    qr_x = usable_mm - pad - qr
    text_w = qr_x - pad - 1.0

    elements: list[dict[str, Any]] = [
        {
            "type": "text",
            "x_mm": pad,
            "y_mm": pad,
            "w_mm": round(text_w, 2),
            "h_mm": 4.0,
            "content": "{display_name}",
            "size_mm": 4.0,
            "bold": True,
        },
        {
            "type": "text",
            "x_mm": pad,
            "y_mm": round(pad + 4.6, 2),
            "w_mm": round(text_w, 2),
            "h_mm": 2.6,
            "content": "{brand} · {material}",
            "size_mm": 2.6,
        },
        {
            "type": "text",
            "x_mm": pad,
            "y_mm": round(pad + 7.7, 2),
            "w_mm": round(text_w, 2),
            "h_mm": 2.6,
            "content": "{remaining_g} g · #{id}",
            "size_mm": 2.6,
            "italic": True,
        },
        {
            "type": "qr",
            "x_mm": round(qr_x, 2),
            "y_mm": pad,
            "w_mm": round(qr, 2),
            "h_mm": round(qr, 2),
            "content": "{deeplink}",
        },
    ]

    if barcode:
        bar_h = 7.0
        elements.append(
            {
                "type": "barcode",
                "x_mm": pad,
                "y_mm": round(height_mm - pad - bar_h, 2),
                "w_mm": round(usable_mm - 2 * pad, 2),
                "h_mm": bar_h,
                "content": "{ean}",
                "symbology": "ean13",
            }
        )

    return elements


#: Sizes a label printer can take, so somebody who plugs one in is not looking
#: at a list with nothing in it that fits.
#:
#: ⚠️ No ``builtin_key``: no API contract names them, and a key would make them
#: undeletable for no reason.
STARTER_TEMPLATES: list[dict[str, Any]] = [
    {
        "builtin_key": None,
        "name": "Label printer 40 × 20",
        "width_mm": 40.0,
        "height_mm": 20.0,
        "shape": "rect",
        "elements": _device_elements(40.0, 20.0, barcode=False, printable_mm=_B1_PRINTABLE_MM),
    },
    {
        "builtin_key": None,
        "name": "Label printer 50 × 30",
        "width_mm": 50.0,
        "height_mm": 30.0,
        "shape": "rect",
        "elements": _device_elements(50.0, 30.0, barcode=True, printable_mm=_B1_PRINTABLE_MM),
    },
]


__all__ = ["BUILTIN_SHEETS", "BUILTIN_TEMPLATES", "STARTER_TEMPLATES"]
