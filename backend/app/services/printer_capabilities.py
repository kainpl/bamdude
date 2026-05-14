"""Per-model capability map for the Printer Settings dialog.

Mirrors the BS PrintOptionsDialog visibility rules. AI/visual detectors
sit behind camera-capable families (X1 + H2D); sensors like filament-
tangle live on X1/H2D; behaviour toggles (auto-recovery, sound) are
universal. Dual-nozzle parts editing only on H2D family.

This is intentionally a flat dict (no nested supports) so the API
response keeps it cheap to serialize and the frontend hides rows with
``!supports[key]`` checks.
"""

from typing import TypedDict

from backend.app.services.bambu_mqtt import PrinterState
from backend.app.services.calibration_mode_registry import mode_state_map
from backend.app.utils.printer_models import has_door_sensor


class PrinterSupports(TypedDict):
    # AI / visual detectors
    spaghetti_detector: bool
    pileup_detector: bool
    nozzleclumping_detector: bool
    airprinting_detector: bool
    first_layer_inspector: bool
    ai_monitoring: bool
    # Other sensors
    filament_tangle: bool
    nozzle_blob: bool
    fod_check: bool
    displacement_detection: bool
    # Door / air
    open_door_check: bool
    purify_air: bool
    # Behaviour
    auto_recovery: bool
    sound: bool
    save_remote_to_storage: bool
    snapshot: bool
    # Build plate
    plate_type: bool
    plate_align: bool
    # Parts
    parts_editable: bool
    parts_dual: bool


def _norm(model: str | None) -> str:
    if not model:
        return ""
    return model.strip().upper().replace(" ", "").replace("-", "")


_X1_FAMILY = frozenset({"X1", "X1C", "X1E"})
_H2_FAMILY = frozenset({"H2D", "H2DPRO", "H2C", "H2S"})
_AI_CAPABLE = _X1_FAMILY | _H2_FAMILY


def compute_printer_supports(state: PrinterState, printer_model: str | None, module_vers: dict) -> PrinterSupports:
    m = _norm(printer_model)
    has_ai = m in _AI_CAPABLE
    is_h2 = m in _H2_FAMILY
    is_h2d_pro = m == "H2DPRO"

    return PrinterSupports(
        # AI detectors
        spaghetti_detector=has_ai,
        pileup_detector=has_ai,
        nozzleclumping_detector=has_ai,
        airprinting_detector=has_ai,
        first_layer_inspector=has_ai,
        ai_monitoring=has_ai,
        # Sensors
        filament_tangle=has_ai,
        nozzle_blob=m in _X1_FAMILY,  # BS gates this to X1 only
        fod_check=has_ai,
        displacement_detection=has_ai,
        # Door / air
        open_door_check=has_door_sensor(printer_model),
        purify_air=is_h2d_pro,
        # Behaviour — universal where MQTT supports it
        auto_recovery=True,
        sound=True,
        save_remote_to_storage=True,
        snapshot=has_ai,
        # Build plate
        plate_type=is_h2 or m in {"X2D", "P2S"},
        plate_align=is_h2 or m in {"X2D"},
        # Parts
        parts_editable=False,  # read-only this iteration
        parts_dual=is_h2 and m in {"H2D", "H2DPRO"},
    )


# ---------- Filament Calibration capabilities (m062 / Plan 1) ----------

_LIDAR_MODELS = frozenset({"X1", "X1C", "X1E", "H2D", "H2DPRO"})
_DUAL_EXTRUDER_MODELS = frozenset({"H2D", "H2DPRO"})


def _list_extruders(model_norm: str) -> list[dict]:
    if model_norm in _DUAL_EXTRUDER_MODELS:
        return [{"id": 0, "name": "Right"}, {"id": 1, "name": "Left"}]
    return [{"id": 0, "name": "Main"}]


def compute_calibration_supports(
    state: PrinterState,
    printer_model: str | None,
    module_vers: dict,
) -> dict:
    """Per-model capability matrix for Filament Calibration wizard.

    auto_* gates: model must have lidar AND the printer state must report
    support flag. Manual paths universally available. Tower modes universal
    (just a print). Dual-extruder for H2D family.

    Slicer-sidecar availability is checked at the *entry-point* layer
    (Filament Calibration kebab entries are hidden when ``use_slicer_api``
    is off in Settings) and again server-side in
    ``CalibrationService.start_calibration`` for the manual / tower paths
    — all of which need slicing once the W2 pipeline lands.

    Per-mode lifecycle state (``mode_state``) is projected from
    ``calibration_mode_registry.MODE_STATE`` — the wizard reads it to
    render disabled / verification / production rows. Capability booleans
    (``pa_manual`` etc.) stay as-is for backwards compatibility; the
    frontend ANDs them with ``mode_state`` to decide whether the row is
    interactive.
    """
    m = _norm(printer_model)
    has_lidar = m in _LIDAR_MODELS

    return {
        # Manual paths
        "pa_manual": True,
        "flow_manual": True,
        "temp_tower": True,
        "vol_speed_tower": True,
        "vfa_tower": True,
        "retraction_tower": True,
        # Auto paths (lidar + push flag) — printer-side, no asset / slice needed
        "pa_auto": has_lidar and bool(getattr(state, "is_support_pa_calibration", False)),
        "flow_auto": has_lidar and bool(getattr(state, "is_support_auto_flow_calibration", False)),
        # Layout
        "dual_extruder": m in _DUAL_EXTRUDER_MODELS,
        "extruders": _list_extruders(m),
        "nozzles": [
            {
                "id": i,
                "diameter": getattr(n, "diameter", None),
                "type": getattr(n, "type", None),
                "flow_type": getattr(n, "flow_type", None),
            }
            for i, n in enumerate(getattr(state, "nozzles", []) or [])
        ],
        "mode_state": mode_state_map(),
    }
