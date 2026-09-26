"""Sanity-check a sliced file before BamDude is willing to print it.

A Bambu printer's start G-code is where the AMS load (``M620``) and the
preparation-stage announcements (``M1002 gcode_claim_action``) live. Slice
without it and the job still dispatches, still heats the bed and still moves
the toolhead — it simply extrudes nothing, reports no stage, and sits at layer
0 until somebody notices (upstream #2838).

Nothing downstream can tell that apart from a print that has not started yet,
so the only place to catch it is here, on the bytes the slicer just produced.
All 56 instantiable presets in the shipped Bambu bundle carry
``gcode_claim_action``, which makes its absence a reliable signal rather than a
heuristic.

⚠️ **The root cause is fixed in our sidecar already**, in
``profile-resolver.ts``: a bundled preset can keep a setting in a companion
file the preset itself does not reference, and a resolver that walks only
``inherits`` never reaches it — landing every BBL machine preset on the generic
577-byte block from ``fdm_machine_common``. This module is the backstop for the
one case our own source tree cannot rule out: an operator running an image
built before that fix. Hence the message says **rebuild**, not "update" — we
build the sidecar from source rather than pulling a published image.
"""

from __future__ import annotations

import io
import json
import logging
import zipfile

logger = logging.getLogger(__name__)

# Present in the start G-code of every instantiable machine preset in the
# bundle. ``M620`` is equally universal today, but this one is the marker whose
# absence is visible from the printer's side: no claim actions means ``stg_cur``
# stays -1 and the UI never names a preparation step.
_START_GCODE_MARKER = "gcode_claim_action"

_PROJECT_SETTINGS = "Metadata/project_settings.config"

# The start block sits after the file header and the embedded thumbnails, well
# inside this. Bounded so a pathological output cannot turn the check into a
# multi-hundred-megabyte read.
_GCODE_SCAN_BYTES = 4 * 1024 * 1024


def _as_text(value: object) -> str:
    """Slicer config values arrive as a bare string or a one-element list."""
    if isinstance(value, list):
        return "".join(str(v) for v in value)
    return "" if value is None else str(value)


def start_gcode_is_missing(content: bytes, *, export_3mf: bool) -> bool:
    """Whether ``content`` was sliced without the printer's start G-code.

    ⚠️ Answers False whenever the question cannot be settled — an unreadable
    archive, a missing config, a decode failure. A slice that is merely unusual
    must not be blocked by a check that only knows how to recognise one
    specific defect; the caller has no better information than we do.
    """
    if not content:
        return False

    if not export_3mf:
        head = content[:_GCODE_SCAN_BYTES].decode("utf-8", errors="ignore")
        return bool(head) and _START_GCODE_MARKER not in head

    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            raw = archive.read(_PROJECT_SETTINGS)
    except (KeyError, OSError, zipfile.BadZipFile) as exc:
        logger.debug("Slice output check skipped: cannot read %s (%s)", _PROJECT_SETTINGS, exc)
        return False

    try:
        settings = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.debug("Slice output check skipped: %s is not valid JSON (%s)", _PROJECT_SETTINGS, exc)
        return False

    if not isinstance(settings, dict) or "machine_start_gcode" not in settings:
        logger.debug("Slice output check skipped: no machine_start_gcode in %s", _PROJECT_SETTINGS)
        return False

    return _START_GCODE_MARKER not in _as_text(settings["machine_start_gcode"])


def missing_start_gcode_message(printer_preset_name: str) -> str:
    """The 502 body for a slice that came back without its start G-code.

    Names the sidecar because that is where the fix is: BamDude sends the
    bundled preset by name and the sidecar resolves it, so an image built
    before the companion-file fix resolves it to a generic stub and no amount
    of retrying here will change the result.

    ⚠️ **Rebuild, not update** — a divergence from upstream's wording, and the
    same one the process-settings panel's ``sidecar_outdated`` copy makes. They
    pull a published image; we build ours from the fork's GitHub URL, so
    "update the image" names an action our operator does not have.
    """
    return (
        f"The slicer returned a file with no printer start G-code for '{printer_preset_name}'. "
        "Printing it would heat the printer and extrude nothing, so it was not saved. "
        "The slicer sidecar image is out of date: older builds cannot read the companion "
        "profile that holds the real start G-code for most Bambu printers. Rebuild the "
        "sidecar image and slice again."
    )


# What the CLI writes into a filament slot it could not resolve (upstream
# 4f10d155). Bambu Studio uses this literal for a filament whose vendor is
# unknown, and it is the one field that separates "nothing inherited" from a
# legitimately vendor-less profile: a resolved preset always carries a real
# ``filament_ids`` entry, an unresolved one the empty string.
_UNDEFINED_VENDOR = "(Undefined)"


def unresolved_filament_slots(content: bytes, *, export_3mf: bool) -> list[int]:
    """1-indexed filament slots the slicer could not resolve a preset for.

    A filament preset whose name the sidecar's bundle cannot resolve is not
    rejected: the CLI inherits nothing, falls back to its defaults for every
    field and returns a well-formed success — measured, a missing name slices
    as ``filament_type: ["PLA"]`` at ``nozzle_temperature: ["200"]`` with
    ``filament_ids: [""]`` and ``filament_vendor: ["(Undefined)"]``. So a PETG
    preset a sidecar image predates prints at PLA temperatures.

    Empty whenever the question cannot be settled — a raw-G-code response, an
    unreadable archive, a missing or malformed config — on the same principle
    as ``start_gcode_is_missing``. Both signals are required together:
    ``filament_vendor`` alone would flag a hand-written profile that never
    named a vendor, ``filament_ids`` alone a user's own cloud preset, which
    legitimately carries no bundled id.
    """
    if not content or not export_3mf:
        return []

    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            raw = archive.read(_PROJECT_SETTINGS)
        settings = json.loads(raw)
    except (KeyError, OSError, zipfile.BadZipFile, UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.debug("Filament resolution check skipped: cannot read %s (%s)", _PROJECT_SETTINGS, exc)
        return []

    if not isinstance(settings, dict):
        return []
    vendors = settings.get("filament_vendor")
    ids = settings.get("filament_ids")
    if not isinstance(vendors, list) or not isinstance(ids, list):
        logger.debug("Filament resolution check skipped: no per-slot vendor/id arrays")
        return []

    unresolved: list[int] = []
    for slot in range(min(len(vendors), len(ids))):
        if _as_text(vendors[slot]).strip() == _UNDEFINED_VENDOR and not _as_text(ids[slot]).strip():
            unresolved.append(slot + 1)
    return unresolved


def unresolved_filament_message(slots: list[int], preset_names: list[str]) -> str:
    """The warning logged for slots whose filament preset did not resolve.

    Names the presets by the slot they were picked for — the only handle the
    user has on which dropdown to change. Plain ASCII so it survives every log
    sink.
    """
    parts: list[str] = []
    for slot in slots:
        name = preset_names[slot - 1] if slot - 1 < len(preset_names) else ""
        parts.append(f"slot {slot} ({name})" if name else f"slot {slot}")
    return (
        f"The slicer could not resolve the filament preset for {', '.join(parts)}, so those slots "
        "were sliced with its built-in defaults (PLA, 200 C) instead of the preset's own settings. "
        "The file was kept, but check the temperatures before printing. This usually means the "
        "slicer sidecar's bundled profiles do not contain the preset that was picked - updating "
        "the sidecar image, or picking a preset from its own bundled list, resolves it."
    )
