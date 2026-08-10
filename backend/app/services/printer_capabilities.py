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
from backend.app.utils.printer_configs import (
    air_print_detection_position,
    get_device_support_flags,
    supports_safety_options,
)
from backend.app.utils.printer_models import has_door_sensor, is_dual_nozzle_model


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
    smart_nozzle_blob: bool
    fod_check: bool
    displacement_detection: bool
    air_print_nonvisual: bool
    ai_monitoring_legacy: bool
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
    plate_mark: bool
    # Parts
    parts_editable: bool
    parts_dual: bool
    # Safety tab
    safety_tab: bool
    idle_heating: bool


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

    # Live per-option support parsed from the printer (BS DevPrintOptionsParser
    # parity — see bambu_mqtt._parse_print_option_support). Used when the printer
    # actually reported the option's source; otherwise fall back to the model-
    # family heuristic (covers the pre-pushall window + fields a printer omits).
    sup = getattr(state, "print_option_support", None) or {}

    def _s(key: str, fam: bool) -> bool:
        return bool(sup[key]) if key in sup else fam

    # Build-plate detection: BS shows the Type+Alignment group when either is
    # supported, otherwise the legacy single "detection of build plate position"
    # (plate_mark). Mutually exclusive — mark hidden whenever the group shows.
    _plate_type = _s("plate_type", is_h2 or m in {"X2D", "P2S"})
    _plate_align = _s("plate_align", is_h2 or m in {"X2D"})
    _plate_mark = bool(sup.get("plate_mark", False)) and not (_plate_type or _plate_align)

    return PrinterSupports(
        # AI detectors — modern printers advertise these via fun/fun2/xcam bits;
        # P1/A1 series send no xcam.cfg so they correctly resolve to off.
        spaghetti_detector=_s("spaghetti_detector", has_ai),
        pileup_detector=_s("pileup_detector", has_ai),
        nozzleclumping_detector=_s("nozzleclumping_detector", has_ai),
        airprinting_detector=_s("airprinting_detector", has_ai),
        # Fallback from the model's own config, not from "has a camera".
        # ⚠️ Measured across all fifteen: ``has_ai`` claims first-layer
        # inspection on H2C / H2D / H2D Pro / H2S, whose configs say
        # ``support_first_layer_inspect: false`` — four models offered a row for
        # a feature the machine does not have. The live bit still wins when the
        # printer reports it; this only decides the pre-push window and firmware
        # that omits the field, which is exactly where a guess was worst.
        first_layer_inspector=_s(
            "first_layer_inspector",
            bool(get_device_support_flags(printer_model).get("support_first_layer_inspect", has_ai)),
        ),
        ai_monitoring=_s("ai_monitoring", has_ai),
        # Sensors — legacy nozzle-blob is hidden when the smart 3-mode variant is
        # supported (BS mutual exclusion). Non-visual air-print + legacy AI
        # monitoring are new BS-parity rows (unverified — no local hardware).
        filament_tangle=_s("filament_tangle", has_ai),
        nozzle_blob=_s("nozzle_blob", m in _X1_FAMILY) and not bool(sup.get("smart_nozzle_blob", False)),
        smart_nozzle_blob=bool(sup.get("smart_nozzle_blob", False)),
        fod_check=_s("fod_check", has_ai),
        displacement_detection=_s("displacement_detection", has_ai),
        air_print_nonvisual=bool(sup.get("air_print_nonvisual", False))
        and air_print_detection_position(printer_model) == "print_option",
        ai_monitoring_legacy=bool(sup.get("ai_monitoring_devcfg", False)) and not bool(sup.get("ai_monitoring", False)),
        # Door / air — open-door detection moves to the Safety tab on
        # safety-capable models (X2D/P2S), mirroring BS's mutual exclusion.
        # BS: ``is_support_door_open_check = get_flag_bits(fun, 12)``, then the
        # row is hidden when the model has the Safety tab, because it lives
        # there instead (PrintOptionsDialog::UpdateOptionOpenDoorCheck).
        #
        # ⚠️ We parsed that bit in TWO places and read it in none, gating on a
        # hardcoded ``has_door_sensor`` list instead. That list answers "is
        # there a door sensor", which is a different question from "does this
        # firmware offer the door-open CHECK" — and it excluded the whole H2
        # family, so those machines could not turn the protection on at all.
        #
        # ``_s`` keeps the list as the fallback for the window before the first
        # push and for firmware that omits ``fun`` entirely.
        open_door_check=_s("open_door_check", has_door_sensor(printer_model))
        and not supports_safety_options(printer_model),
        purify_air=_s("purify_air", is_h2d_pro),
        # Behaviour — universal fallback; live flag wins when reported.
        auto_recovery=_s("auto_recovery", True),
        sound=_s("sound", True),
        # "Store sent files on external storage" is gated purely on the DevConfig
        # flag support_save_remote_print_file_to_storage (BS SupportSaveRemote,
        # default false) — NOT on having storage. P1S has an SD slot but BS still
        # hides it. Show only when the printer actually reports the flag.
        save_remote_to_storage=_s("save_remote_to_storage", False),
        snapshot=_s("snapshot", has_ai),
        # Build plate
        plate_type=_plate_type,
        plate_align=_plate_align,
        plate_mark=_plate_mark,
        # Parts — dual-nozzle (L/R) layout for every twin-nozzle model, not just
        # H2D/H2D Pro: X2D and H2C also report two hotend nozzles (ids 0/1). Use
        # the canonical dual-nozzle set so the Parts dialog labels L/R on all of
        # them (previously X2D showed two unlabelled, indistinguishable nozzles).
        parts_editable=False,  # read-only this iteration
        parts_dual=is_dual_nozzle_model(printer_model),
        # Safety tab — gated by the static JSON flag (X2D/P2S); rows self-gate on
        # live fun bits. idle_heating row needs fun bit 62 (support_idle_heating).
        safety_tab=supports_safety_options(printer_model),
        idle_heating=bool(getattr(state.print_options, "support_idle_heating", False)),
    )


# ---------- Filament Calibration capabilities (m062 / Plan 1) ----------

_LIDAR_MODELS = frozenset({"X1", "X1C", "X1E", "H2D", "H2DPRO"})
_DUAL_EXTRUDER_MODELS = frozenset({"H2D", "H2DPRO"})


def _list_extruders(model_norm: str) -> list[dict]:
    if model_norm in _DUAL_EXTRUDER_MODELS:
        return [{"id": 0, "name": "Right"}, {"id": 1, "name": "Left"}]
    return [{"id": 0, "name": "Main"}]


def _as_float(value: object) -> float | None:
    """Firmware reports the nozzle diameter as a string ("0.4"); the API contract
    (``client.ts``) and the wizard both want a number. Empty and unparseable
    both answer ``None`` so the caller can tell "not reported" from a value."""
    if value is None or value == "":
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


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
        # ⚠️ These read ``nozzle_diameter`` / ``nozzle_type`` / ``nozzle_flow``,
        # which is what ``NozzleInfo`` actually carries. They used to ask for
        # ``diameter`` / ``type`` / ``flow_type`` — attributes that do not exist
        # on it — so all three ``getattr`` defaults fired and **every nozzle came
        # out as all-None**. The wizard then fell through to its hardcoded
        # 0.4 mm (``capabilities?.nozzles?.[0]?.diameter ?? 0.4``), so a farm on
        # 0.6 or 0.8 filed every calibration under the wrong nozzle key.
        #
        # The response keys stay ``diameter`` / ``type`` / ``flow_type``: that is
        # the published contract (``client.ts``), and it is only the *source*
        # attribute that was wrong.
        "nozzles": [
            {
                "id": i,
                # NozzleInfo keeps the firmware's string ("0.4"); the schema and
                # the wizard both want a number, and the wizard compares it.
                "diameter": _as_float(getattr(n, "nozzle_diameter", None)),
                "type": getattr(n, "nozzle_type", None) or None,
                "flow_type": getattr(n, "nozzle_flow", None) or None,
            }
            for i, n in enumerate(getattr(state, "nozzles", []) or [])
        ],
        "mode_state": mode_state_map(),
    }
