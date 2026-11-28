"""What a direct-connected nozzle rack (H3C, P3C) may not be sent yet — one gate.

BamDude prints on an H3C / P3C (N01 stage B) and configures its slots — a rack
AMS slot and each PrintTool's external input take ``ams_filament_setting`` and
its reset in Bambu Studio's form (``bambu_mqtt.ams_set_filament_setting``).
What still waits for stage C is everything else that addresses such a slot:
loading and unloading, an RFID re-read, drying, the K-profile of a slot
(``extrusion_cali_sel`` / ``extrusion_cali_set``, keyed by ``nozzle_pos`` there)
and the automatic PA / flow calibration. The old writers would address those
wrongly, and the firmware's answer to them is not known.

So each such write is refused here, by name: the client does not send it, a
route answers 409 ``direct_rack_unsupported``. A regular AMS bound to a rack
printer is not a rack slot and is not refused. Each stage removes its part.
"""

from __future__ import annotations

from fastapi import HTTPException

from backend.app.utils.tray_ids import EXTERNAL_AMS_ID, TrayLayout, as_layout, is_zmulti

UNSUPPORTED_CODE = "direct_rack_unsupported"
UNSUPPORTED_MESSAGE = (
    "Loading, unloading, RFID, drying, K-profile and automatic calibration commands "
    "on H3C and P3C printers are not supported yet"
)


class DirectRackUnsupported(Exception):
    """A service refusing a command on a direct rack (automatic calibration); the route answers ``refusal()``."""

    def __init__(self) -> None:
        super().__init__(UNSUPPORTED_MESSAGE)


class DirectRackRefused(Exception):
    """A direct rack's own reason (a routing code: ``rack_hotend_mismatch`` ...) refusing a calibration start.

    The route answers ``refusal_for(code)``: 409 with the code and its sentence.
    """

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def refusal_for(code: str) -> HTTPException:
    """The 409 for a direct rack's routing reason, worded from the routing catalogue."""
    from backend.app.services.filament_intake import routing_detail

    return HTTPException(409, {"error": code, "message": routing_detail(code)["message"]})


def is_rack_slot(layout: TrayLayout, ams_id: int, tray_id: int | None = None) -> bool:
    """Whether ``(ams_id, tray_id)`` is a rack AMS's slot or a direct rack's external input."""
    layout = as_layout(layout)
    if is_zmulti(layout.ams_type(ams_id)):
        return True
    return ams_id == EXTERNAL_AMS_ID and layout.direct_rack


def slot_command_refused(layout: TrayLayout, ams_id: int, tray_id: int | None = None) -> bool:
    """Whether a stage-C command (drying, RFID, K-profile) for slot ``(ams_id, tray_id)`` must not be sent.

    Not asked by the slot setting and its reset: those go out in Studio's form.
    """
    return is_rack_slot(layout, ams_id, tray_id)


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


def auto_calibration_refused(layout: TrayLayout) -> bool:
    """Whether an automatic PA / flow calibration must not be started on this printer (until stage C).

    The manual calibration is a print through the dispatcher and is not refused.
    """
    return as_layout(layout).direct_rack


def refusal() -> HTTPException:
    """The 409 a route answers with for a refused command."""
    return HTTPException(409, {"error": UNSUPPORTED_CODE, "message": UNSUPPORTED_MESSAGE})
