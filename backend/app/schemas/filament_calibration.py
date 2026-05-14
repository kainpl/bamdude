"""Pydantic schemas for the Filament Calibration wizard API (m062 / Plan 1)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from backend.app.schemas.slicer import PresetRef, SliceBundleSpec
from backend.app.services.calibration_constants import CaliMethod, CaliMode

# ---------- Capabilities ----------


class ExtruderInfo(BaseModel):
    id: int
    name: str


class NozzleInfo(BaseModel):
    id: int
    diameter: float | None = None
    type: str | None = None
    flow_type: str | None = None


class CalibCapabilities(BaseModel):
    pa_manual: bool
    flow_manual: bool
    temp_tower: bool
    vol_speed_tower: bool
    vfa_tower: bool
    retraction_tower: bool
    pa_auto: bool
    flow_auto: bool
    dual_extruder: bool
    extruders: list[ExtruderInfo]
    nozzles: list[NozzleInfo]
    # Per-mode lifecycle: key is CaliMode.value, value is ModeState.value
    # ("disabled" / "verification" / "production"). See
    # backend/app/services/calibration_mode_registry.py for the contract.
    mode_state: dict[str, str]


# ---------- Start ----------


class CalibPrintOptionsIn(BaseModel):
    """Per-job print toggles, mirrors PrintModal/types.ts ``PrintOptions``.

    Forwarded onto ``PrintQueueItem`` so the scheduler builds dispatch
    options identically to a regular print. Defaults match the
    BS-recommended values for a calibration test print: levelling on,
    AUTO flow-cali off (gcode M900 K changes drive the K sweep), AI
    layer inspection off, timelapse off, mesh-mode fast-check on,
    operator g-code injection off.
    """

    model_config = ConfigDict(extra="forbid")

    bed_levelling: bool = True
    flow_cali: bool = False
    layer_inspect: bool = False
    timelapse: bool = False
    mesh_mode_fast_check: bool = True
    gcode_injection: bool = False


class CalibSwapMacrosIn(BaseModel):
    """Swap-mode macro intent for a single calibration job, mirrors
    PrintModal/types.ts ``SwapMacrosOptions``.

    Maps to ``PrintQueueItem.execute_swap_macros`` +
    ``PrintQueueItem.swap_macro_events`` so the dispatcher's
    ``_run_swap_macro_if_needed`` fires the same macros for a
    calibration print as it does for a regular library / archive job.
    Only ``swap_mode_start`` / ``swap_mode_change_table`` are
    accepted — same allow-list as the PrintModal frontend.
    """

    model_config = ConfigDict(extra="forbid")

    execute: bool = False
    events: list[Literal["swap_mode_start", "swap_mode_change_table"]] = []


class CalibFilamentIn(BaseModel):
    ams_id: int
    slot_id: int
    tray_id: int
    filament_id: str
    filament_setting_id: str | None = None
    bed_temp: int
    nozzle_temp: int
    max_volumetric_speed: float
    flow_rate: float = 0.98
    extruder_id: int | None = None


class StartSessionIn(BaseModel):
    """Body for ``POST /printers/{id}/calibration/sessions`` (production
    dispatch).

    Preset-selection shape mirrors :class:`CalibSliceOnlyIn` so the
    same frontend picker drives both verification (download the bake)
    and production (dispatch to printer) flows — the only UX
    difference is the action button. Modes whose dispatcher routes
    through the slicer sidecar (PA Tower and all subsequent manual
    modes) consume these fields to build + slice the per-mode 3MF;
    Auto modes (AUTO_PA_LINE / FLOW_RATE) ignore them entirely (they
    fire MQTT directly).
    """

    cali_mode: CaliMode
    method: CaliMethod
    nozzle_diameter: float
    nozzle_volume_type: Literal["standard", "high_flow", "tpu_high_flow", "hybrid"]
    extruder_id: int = 0
    filaments: list[CalibFilamentIn]

    # --- Preset / slicer fields (mirror CalibSliceOnlyIn) ---
    # Optional spec passthrough for per-mode knobs (e.g. PA Tower's
    # start/end/step). Opaque to the route; per-mode builder validates.
    spec: dict[str, float | int | bool | str] | None = None

    # Bundle path (sidecar materialises printer/process/filament from
    # a stored .bbscfg).
    bundle: SliceBundleSpec | None = None

    # Manual path (resolver materialises each PresetRef into the JSON
    # the sidecar's --load-settings expects).
    printer_preset: PresetRef | None = None
    process_preset: PresetRef | None = None
    filament_presets: list[PresetRef] = []

    # Optional per-job slicer override — matches SliceRequest.slicer.
    slicer: Literal["orcaslicer", "bambu_studio"] | None = None
    # Bed plate override — SliceRequest.bed_type values.
    bed_type: (
        Literal[
            "Cool Plate",
            "Engineering Plate",
            "High Temp Plate",
            "Textured PEI Plate",
            "Supertack Plate",
        ]
        | None
    ) = None

    # Per-job dispatcher toggles — reuse the PrintModal panels so a
    # calibration print runs swap macros / bed-levelling / etc. the
    # same way a regular library job would. Defaults mirror the
    # PrintModal defaults (bed_levelling on, flow_cali off for this
    # job kind, swap macros opt-in by operator).
    print_options: CalibPrintOptionsIn = CalibPrintOptionsIn()
    swap_macros: CalibSwapMacrosIn = CalibSwapMacrosIn()

    @model_validator(mode="after")
    def _require_preset_shape_for_sidecar_modes(self) -> StartSessionIn:
        # Auto modes don't slice — skip preset validation entirely.
        if self.method == CaliMethod.AUTO:
            return self
        # Manual modes that route through the sidecar pipeline need
        # either a bundle or the full PresetRef triplet. Same contract
        # as CalibSliceOnlyIn.
        if self.bundle is not None:
            return self
        if not self.printer_preset or not self.process_preset or not self.filament_presets:
            raise ValueError(
                "Manual calibration needs either 'bundle' or all of "
                "'printer_preset' + 'process_preset' + 'filament_presets'"
            )
        return self


# ---------- Slice-only (verification mode) ----------


class CalibSliceOnlyIn(BaseModel):
    """Body for ``POST /printers/{id}/calibration/slice-only``.

    Verification-mode slice-and-download has no AMS slot or save-row
    bookkeeping (nothing dispatches, nothing persists) — the operator
    only needs to tell the sidecar which presets to slice against, plus
    the per-mode :class:`CalibrationSpec` knobs. ``spec`` is opaque to
    the route; the per-mode builder in ``calib_3mf_builder`` validates
    it against ``backend.app.schemas.calibration_spec``.

    Mirrors :class:`backend.app.schemas.slicer.SliceRequest`'s two
    preset shapes:

    - **Bundle path** — ``bundle`` is set, the sidecar materialises
      printer / process / filament JSONs from a stored ``.bbscfg``.
    - **Manual path** — ``printer_preset`` / ``process_preset`` /
      ``filament_presets`` carry source-aware :class:`PresetRef`
      pointers; the route resolves each through
      :mod:`backend.app.services.preset_resolver` and ships the JSON
      triplet to the sidecar's ``slice_with_profiles``.

    Exactly one shape must be present; the validator enforces it.
    """

    cali_mode: CaliMode
    spec: dict[str, float | int | bool | str] | None = None
    extruder_count: int = 1
    pass_n: int = 1

    # Bundle path
    bundle: SliceBundleSpec | None = None

    # Manual path
    printer_preset: PresetRef | None = None
    process_preset: PresetRef | None = None
    filament_presets: list[PresetRef] = []

    # Optional per-job slicer override — matches SliceRequest.slicer.
    slicer: Literal["orcaslicer", "bambu_studio"] | None = None
    # Bed plate override — SliceRequest.bed_type values.
    bed_type: (
        Literal[
            "Cool Plate",
            "Engineering Plate",
            "High Temp Plate",
            "Textured PEI Plate",
            "Supertack Plate",
        ]
        | None
    ) = None

    @model_validator(mode="after")
    def _require_one_preset_shape(self) -> CalibSliceOnlyIn:
        if self.bundle is not None:
            return self
        if not self.printer_preset or not self.process_preset or not self.filament_presets:
            raise ValueError(
                "slice-only body needs either 'bundle' or all of "
                "'printer_preset' + 'process_preset' + 'filament_presets'"
            )
        return self


# ---------- Bake-only (pre-slicer inspection) ----------


class CalibBakeOnlyIn(BaseModel):
    """Body for ``POST /printers/{id}/calibration/bake-only``.

    Returns the calibration 3MF *before* it's handed to the slicer
    sidecar — same artefact ``slice-only`` would have sent, but stops
    one step earlier. Useful for inspecting what BamDude actually puts
    on the wire: ``Metadata/custom_gcode_per_layer.xml`` lines, per-
    object overrides in ``model_settings.config``, project-settings
    patch, the trimmed scaffold geometry. The operator can unzip the
    returned file and diff against BS's own calibration plate.

    No bundle / preset triplet needed — presets are sidecar-side and
    not embedded in our composed 3MF.
    """

    cali_mode: CaliMode
    spec: dict[str, float | int | bool | str] | None = None
    extruder_count: int = 1
    pass_n: int = 1
    bed_type: (
        Literal[
            "Cool Plate",
            "Engineering Plate",
            "High Temp Plate",
            "Textured PEI Plate",
            "Supertack Plate",
        ]
        | None
    ) = None


# ---------- Submit ----------


class ManualResultIn(BaseModel):
    best_line_index: int | None = None
    coarse_modifier: int | None = None
    skip_fine: bool = False
    fine_modifier: int | None = None


class AutoResultEditIn(BaseModel):
    tray_id: int
    k_value: float | None = None
    n_coef: float | None = None
    name: str | None = None
    save: bool = True


class AutoResultIn(BaseModel):
    results: list[AutoResultEditIn]


# ---------- Outputs ----------


class FilamentCalibrationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    printer_id: int
    filament_id: str
    filament_setting_id: str | None
    nozzle_diameter: float
    nozzle_volume_type: str
    extruder_id: int
    pa_k_value: float | None
    pa_n_coef: float | None
    flow_ratio: float | None
    confidence: int | None
    cali_mode: str
    source: str
    is_active: bool
    cali_idx: int | None
    name: str
    notes: str | None
    nozzle_id: str | None
    calibrated_by_user_id: int | None
    created_at: datetime


class CalibrationSessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    printer_id: int
    user_id: int | None
    cali_mode: str
    method: str
    nozzle_diameter: float
    nozzle_volume_type: str
    extruder_id: int
    status: str
    stage: int
    coarse_ratio: float | None
    parent_session_id: int | None
    mqtt_sequence_id: str | None
    print_queue_item_id: int | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class ManualResultOutSchema(BaseModel):
    saved_rows: list[FilamentCalibrationOut]
    next_session_id: int | None = None


class PACalibHistoryEntryOut(BaseModel):
    cali_idx: int
    name: str
    filament_id: str
    setting_id: str
    nozzle_diameter: float
    nozzle_volume_type: str
    extruder_id: int
    k_value: float
    n_coef: float
