"""Checks shared by the manual drying button and scheduled drying.

The manual route (``POST /printers/{id}/drying/start``) and the scheduled-drying
writer both go through here, so a run the button would refuse is never quietly
published by the scheduler (upstream d37ce94f).
"""

from backend.app.services.printer_manager import (
    drying_screen_only,
    first_drying_blocking_reason,
    supports_drying,
)

SCREEN_ONLY_DETAIL = "This printer only supports AMS drying from its own screen"
UNSUPPORTED_DETAIL = "Drying not supported for this printer model or firmware version"

# Maximum drying temperature per AMS unit type — BS ``AMSDryControl.cpp``:
#     { N3F, 45, 65, "AMS2" }, { N3S, 45, 85, "AMS-S" }
# and BS warns when the entered value exceeds the unit's own maximum. A flat
# 45-85 let an AMS 2 Pro be asked for 85 °C — twenty degrees above what that
# unit is rated to do. The unit's ``module_type`` comes from the module name the
# printer reports (``_AMS_MODULE_PREFIXES``). The fallback for an unrecognised
# unit is the LOWER ceiling: erring low costs a retry once the module name
# arrives; erring high drives a spool past what its unit is rated for.
AMS_DRY_MAX_TEMP = {"n3f": 65, "n3s": 85}
AMS_DRY_MAX_TEMP_FALLBACK = 65
AMS_DRY_MIN_TEMP = 45

_POWER_CODES = frozenset({1, 8})
_RETRACT_CODE = 3


def refusal_code(model: str | None, firmware: str | None, *, require_firmware: bool = True) -> str | None:
    """``"screen_only"`` / ``"unsupported"`` when this printer cannot be told to dry, else None.

    ``require_firmware=False`` when there is no live status to read a version
    from — only the model is judged then.
    """
    if drying_screen_only(model):
        return "screen_only"
    if require_firmware and not supports_drying(model, firmware):
        return "unsupported"
    return None


def max_temp_for_unit(unit: dict | None) -> int:
    module_type = str((unit or {}).get("module_type") or "").lower()
    return AMS_DRY_MAX_TEMP.get(module_type, AMS_DRY_MAX_TEMP_FALLBACK)


def blocker_code(unit: dict | None) -> str | None:
    """The waiting-reason code for this unit's blocker, by the same priority as the route."""
    blocker = first_drying_blocking_reason(unit)
    if blocker is None:
        return None
    code = blocker[0]
    if code in _POWER_CODES:
        return "blocked_power"
    if code == _RETRACT_CODE:
        return "blocked_filament_at_outlet"
    return "blocked_other"


def resolve_filament(unit: dict | None, filament: str) -> str:
    """The printer rejects a drying payload without a filament type: take the first loaded tray, else PLA."""
    if filament:
        return filament
    for tray in (unit or {}).get("tray") or []:
        tray_type = tray.get("tray_type")
        if tray_type:
            return str(tray_type)
    return "PLA"
