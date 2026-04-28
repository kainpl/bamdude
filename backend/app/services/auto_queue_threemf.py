"""3MF helpers for the auto-queue layer.

Thin wrappers over ``services/archive.py::ThreeMFParser`` that surface
just the bits the auto-queue scheduler needs: routing requirements
(target model, required filament types) and the cached print time
estimate used for SJF.

Putting this in a separate module keeps the AutoQueueScheduler free of
3MF parsing concerns and lets us mock the extraction in tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from backend.app.services.archive import ThreeMFParser


@dataclass(frozen=True)
class AutoQueueRequirements:
    """Routing inputs auto-extracted from a 3MF for auto-queue items.

    All fields are best-effort — a malformed 3MF returns a fully
    populated dataclass with empty/None values, never raises.

    Fields:
        target_model: Normalized printer model code (e.g. "X1C", "P1S",
            "K1C", "A1MINI"). None when slice_info is absent or printer
            model is not recognised.
        required_filament_types: De-duplicated list of filament types
            actually consumed by the print (used_g > 0). Empty list if
            no filament info found.
        print_time_seconds: Cached print-time estimate from slice_info
            ``prediction`` metadata. Used for SJF + been_jumped sorting.
        filament_slots: Raw per-slot filament info from
            ``ThreeMFParser.metadata['filament_slots']`` —
            ``[{slot_id, used_g, type, color}, ...]``. Surfaced for
            downstream AMS-mapping helpers.
    """

    target_model: str | None
    required_filament_types: list[str]
    print_time_seconds: int | None
    filament_slots: list[dict]


def extract_auto_queue_requirements(
    file_path: Path,
    plate_id: int | None = None,
) -> AutoQueueRequirements:
    """Extract routing requirements from a 3MF file for auto-queue.

    Args:
        file_path: Path to the 3MF on disk.
        plate_id: 1-indexed plate to extract metadata for. When None,
            ThreeMFParser uses the file's default plate (plate 1 for
            single-plate exports).

    Returns:
        Populated AutoQueueRequirements. Does not raise — corrupted /
        truncated 3MFs return a dataclass with None / empty fields.
    """
    parser = ThreeMFParser(file_path, plate_number=plate_id)
    md = parser.parse()

    types: list[str] = []
    slots = md.get("filament_slots") or []
    for slot in slots:
        t = slot.get("type")
        if t and t not in types:
            types.append(t)

    return AutoQueueRequirements(
        target_model=md.get("sliced_for_model"),
        required_filament_types=types,
        print_time_seconds=md.get("print_time_seconds"),
        filament_slots=list(slots),
    )
