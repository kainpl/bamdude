"""Compact per-plate slices for the library list.

Vault 60-specs/library-multiplate-card-spec §4. Pure projections over the
cached ``file_metadata`` (m023) - never a ZIP open. The list is paginated and
the card shows seven things per plate, so the slice rides in the list response
instead of a ``/plates`` request per card; everything deeper (grams per slot,
object names, bed, layers) stays behind ``/plates``.
"""

from __future__ import annotations


def _int_or_none(value) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _float_or_none(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def filament_types_of(filaments) -> list[str]:
    """Unique filament ``type`` tokens of one plate, in slot order; blanks skipped."""
    out: list[str] = []
    for f in filaments or []:
        if not isinstance(f, dict):
            continue
        token = str(f.get("type") or "").strip()
        if token and token not in out:
            out.append(token)
    return out


def split_types(value) -> list[str]:
    """The top-level ``filament_type`` snapshot - the parser joins unique types
    with ``", "`` - back into a deduplicated list. The fallback for a file
    uploaded before m023, which has no plates cache."""
    out: list[str] = []
    for part in str(value or "").split(","):
        token = part.strip()
        if token and token not in out:
            out.append(token)
    return out


def plate_summary(plate: dict) -> dict:
    """The seven fields the card pages through. Layers are deliberately not here."""
    return {
        "index": _int_or_none(plate.get("index")) or 0,
        "name": (str(plate["name"]).strip() or None) if plate.get("name") else None,
        "print_time_seconds": _int_or_none(plate.get("print_time_seconds")),
        "filament_used_grams": _float_or_none(plate.get("filament_used_grams")),
        "object_count": _int_or_none(plate.get("object_count")),
        "filament_types": filament_types_of(plate.get("filaments")),
        "has_thumbnail": bool(plate.get("has_thumbnail")),
    }


def cached_plates(meta) -> list[dict]:
    """The m023 plate cache of a file, or ``[]`` for anything that is not a list of dicts."""
    plates = meta.get("plates") if isinstance(meta, dict) else None
    return [p for p in plates if isinstance(p, dict)] if isinstance(plates, list) else []
