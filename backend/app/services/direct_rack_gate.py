"""What a direct-connected nozzle rack (H3C, P3C) may not be sent yet — one gate.

BamDude reads an H3C / P3C (its AMS 3H / 3P, its rack, its per-PrintTool
external inputs) but does not yet write to one: the commands that address a
rack slot or a rack's external input, and the print dispatch itself, arrive
with their own stages (N01 B: printing; C: slot setting, load / unload, RFID,
drying). Until then the old writers would address those slots wrongly — an
``ams_filament_setting`` for ``(32, 5)`` goes out as an AMS-HT's, an external
``(255, 4)`` as tray 258 — and a print would carry an ``ams_mapping`` of
28 + k the firmware was never shown to accept.

So every such write is refused here, by name, and the assignment that wanted
it stays ``pending_config``; a print to such a printer is refused at the one
dispatch layer. A regular AMS bound to a rack printer is not a rack slot and
is not refused. Each stage removes its part of this gate.
"""

from __future__ import annotations

from fastapi import HTTPException

from backend.app.utils.tray_ids import EXTERNAL_AMS_ID, TrayLayout, as_layout, is_zmulti

UNSUPPORTED_CODE = "direct_rack_unsupported"
UNSUPPORTED_MESSAGE = "Printing and tray commands on H3C and P3C printers are not supported yet"


def slot_command_refused(layout: TrayLayout, ams_id: int, tray_id: int | None = None) -> bool:
    """Whether a command for slot ``(ams_id, tray_id)`` must not be sent: a rack AMS's slot, or a direct rack's external input."""
    layout = as_layout(layout)
    if is_zmulti(layout.ams_type(ams_id)):
        return True
    return ams_id == EXTERNAL_AMS_ID and layout.direct_rack


def tray_command_refused(layout: TrayLayout, tray: int | None) -> bool:
    """The same for a command that names its target by global tray id (load, ``extrusion_cali_set``).

    ``None`` — "whatever is loaded" — is refused on a direct rack: the loaded
    source of one is a rack slot or an external input.
    """
    layout = as_layout(layout)
    if not layout.direct_rack:
        return False
    if tray is None:
        return True
    return layout.rack_slot(tray) is not None or layout.is_external(tray)


def print_refused(layout: TrayLayout) -> bool:
    """Whether a print must not be dispatched to this printer (until stage B)."""
    return as_layout(layout).direct_rack


def refusal() -> HTTPException:
    """The 409 a route answers with for a refused command or print."""
    return HTTPException(409, {"error": UNSUPPORTED_CODE, "message": UNSUPPORTED_MESSAGE})
