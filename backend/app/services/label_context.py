"""What a placeholder resolves to, for one spool.

⚠️ **Built here and nowhere else.** The preview endpoint, the PDF path and the
device path all print the same label, so they must all fill it in the same way
— a second builder is how a preview starts disagreeing with what comes out of
the printer, and that disagreement is invisible until somebody holds both.

Every value is a string, already formatted. Rounding a weight in the renderer
would make the number depend on which backend drew it.
"""

from __future__ import annotations

from typing import Any

from backend.app.models.spool import Spool
from backend.app.services.label_barcode import BarcodeError, spool_ean13
from backend.app.services.label_template import PLACEHOLDERS


def _num(value: float | int | None, decimals: int = 0) -> str:
    """A number as a label prints it: no trailing zeros, no ``None``."""
    if value is None:
        return ""
    if decimals == 0:
        return str(int(round(value)))
    text = f"{value:.{decimals}f}".rstrip("0").rstrip(".")
    return text or "0"


def _hex(rgba: str | None) -> str:
    """``RRGGBBAA`` → ``#RRGGBB``.

    The alpha is dropped rather than rendered: it describes a filament's
    translucency, and printing ``#FF3300FF`` on a shelf label reads as noise.
    """
    if not rgba:
        return ""
    token = rgba.strip().lstrip("#")
    return f"#{token[:6].upper()}" if token else ""


def _hex_all(rgba: str | None, extra_colors: str | None) -> str:
    """Every colour a spool has, comma-separated, for the swatch element."""
    tokens = [t for t in ((rgba or "").strip().lstrip("#"),) if t]
    for part in (extra_colors or "").split(","):
        token = part.strip().lstrip("#")
        if token:
            tokens.append(token)
    return ",".join(tokens)


def spool_context(
    spool: Spool,
    *,
    deeplink_base: str,
    display_name: str | None = None,
) -> dict[str, str]:
    """Fill in every placeholder for a local-inventory spool.

    ``display_name`` is what the Inventory table shows — composed client-side
    from the user's naming template — and wins when given, so the label matches
    the screen it was printed from. Without one, the same fallback chain the
    label API has always used applies.
    """
    label_weight = spool.label_weight or 0
    used = spool.weight_used or 0.0
    remaining = max(label_weight - used, 0.0)
    remaining_pct = (remaining / label_weight * 100) if label_weight else 0.0

    name = (display_name or "").strip() or (
        spool.color_name or spool.slicer_filament_name or f"{spool.brand or ''} {spool.material}".strip()
    )

    try:
        ean = spool_ean13(spool.id)
    except BarcodeError:
        # An id past twelve digits is not a reason to refuse the whole label —
        # the barcode element warns on its own when it cannot draw.
        ean = ""

    return {
        "id": str(spool.id),
        "brand": spool.brand or "",
        "material": spool.material or "",
        "subtype": spool.subtype or "",
        "color_name": spool.color_name or "",
        "slicer_filament_name": spool.slicer_filament_name or "",
        "note": spool.note or "",
        "label_weight_g": _num(label_weight),
        "label_weight_kg": _num(label_weight / 1000, 2),
        "remaining_g": _num(remaining),
        "remaining_kg": _num(remaining / 1000, 2),
        # ⚠️ With the sign, because that is how the same token reads in the
        # inventory table. A label and the row it was printed from disagreeing
        # about one field is discovered at a shelf.
        "remaining_pct": f"{_num(remaining_pct)}%",
        "color_hex": _hex(spool.rgba),
        "color_hex_all": _hex_all(spool.rgba, spool.extra_colors),
        "cost_per_kg": _num(spool.cost_per_kg, 2) if spool.cost_per_kg is not None else "",
        "purchase_date": spool.purchase_date.strftime("%Y-%m-%d") if spool.purchase_date else "",
        "filament_diameter": spool.filament_diameter or "",
        "lot": str(spool.lot) if spool.lot is not None else "",
        "display_name": name or (spool.material or ""),
        "deeplink": f"{deeplink_base}/inventory?spool={spool.id}",
        "ean": ean,
    }


def spoolman_context(
    raw: dict[str, Any],
    *,
    deeplink_base: str,
    display_name: str | None = None,
) -> dict[str, str]:
    """The same vocabulary, filled from a Spoolman ``/spool`` payload.

    Spoolman models a spool with no name of its own, so the display name comes
    off the embedded filament. Fields Spoolman does not carry resolve to empty
    rather than being omitted — an absent key would survive as ``{lot}`` on the
    printed label, which is worse than a gap.
    """
    filament = raw.get("filament") or {}
    vendor = filament.get("vendor") or {}
    spool_id = int(raw.get("id") or 0)

    initial = filament.get("weight")
    remaining = raw.get("remaining_weight")
    used = raw.get("used_weight")
    if remaining is None and initial is not None and used is not None:
        remaining = max(float(initial) - float(used), 0.0)

    color_hex = filament.get("color_hex")
    rgba = color_hex.lstrip("#") if isinstance(color_hex, str) else None

    multi = filament.get("multi_color_hexes")
    if isinstance(multi, list):
        multi = ",".join(str(t) for t in multi)

    try:
        ean = spool_ean13(spool_id)
    except BarcodeError:
        ean = ""

    name = (display_name or "").strip() or filament.get("name") or filament.get("material") or "Spool"

    return {
        "id": str(spool_id),
        "brand": vendor.get("name") or "",
        "material": filament.get("material") or "",
        "subtype": "",
        "color_name": filament.get("name") or "",
        "slicer_filament_name": filament.get("name") or "",
        "note": raw.get("comment") or "",
        "label_weight_g": _num(initial) if initial is not None else "",
        "label_weight_kg": _num(float(initial) / 1000, 2) if initial else "",
        "remaining_g": _num(remaining) if remaining is not None else "",
        "remaining_kg": _num(float(remaining) / 1000, 2) if remaining else "",
        "remaining_pct": f"{_num(float(remaining) / float(initial) * 100)}%" if remaining and initial else "",
        "color_hex": _hex(rgba),
        "color_hex_all": _hex_all(rgba, multi if isinstance(multi, str) else None),
        "cost_per_kg": _num(filament.get("price"), 2) if filament.get("price") else "",
        "purchase_date": "",
        "filament_diameter": _num(filament.get("diameter"), 2) if filament.get("diameter") else "",
        "lot": "",
        "display_name": name,
        "deeplink": f"{deeplink_base}/inventory?spool={spool_id}",
        "ean": ean,
    }


def example_context(*, deeplink_base: str = "https://bamdude.local") -> dict[str, str]:
    """What the editor shows before a spool is picked.

    The placeholders carry their own examples so the picker and the preview
    agree — an editor that previewed with invented values would teach a layout
    against text nothing ever produces.
    """
    context = {p.key: p.example for p in PLACEHOLDERS}
    context["deeplink"] = f"{deeplink_base}/inventory?spool=42"
    context.setdefault("color_hex_all", "FF3300")
    return context


__all__ = ["example_context", "spool_context", "spoolman_context"]
