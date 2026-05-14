"""Per-mode calibration parameter specs (W2 Phase 0).

Each spec describes the user-facing knobs for one calibration mode: the
ranges, bed/plate flavor, "print numbers" toggles, etc. Lives outside
``filament_calibration.py`` so the wizard-API request shapes stay small
and stable while the per-mode params evolve mode-by-mode through the W2
phases.

Naming follows the BS convention from ``src/libslic3r/Calib.hpp`` so the
mapping back to the source-of-truth is obvious: ``CalibTowerSpec`` covers
the tower-shape sweeps (PA / Temp / Retraction / VFA / Vol-Speed) since
they share the same start / end / step / "print numbers" knobs; specific
modes that diverge (Flow Rate's coarse/fine pass, PA Pattern's axis
labels) get their own subclass.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class CalibTowerSpec(BaseModel):
    """Shared shape for tower modes that sweep one numeric value per Z step.

    Covers PA Tower (M900 K), Temp Tower (M104 S), Retraction Tower (length),
    VFA Tower (speed mm/s), Vol-Speed Tower (mm³/s). Per-mode validators in
    ``calib_3mf_builder`` clamp these to the BS-allowed range for each
    mode — this schema is the loosest superset.

    ``layer_height`` is duplicated here from the user-picked process preset
    because the per-Z custom-gcode list must know layer boundaries to inject
    M900 / M104 / retraction commands at the right `top_z`. We can't infer
    it from the sidecar's chosen preset without an extra round-trip, and
    verification mode needs the operator to be explicit anyway.
    """

    start: float = Field(..., description="First sweep value (M900 K, temp °C, mm/s, mm³/s, ...)")
    end: float = Field(..., description="Last sweep value; must be > start")
    step: float = Field(..., gt=0, description="Sweep increment between Z bands")
    layer_height: float = Field(
        default=0.2,
        gt=0,
        description="Slice layer height in mm; must match the picked process preset",
    )
    nozzle_diameter: float = Field(
        default=0.4,
        gt=0,
        description="Nozzle diameter in mm; threaded through to per-print overrides",
    )
    print_numbers: bool = Field(
        default=False,
        description="Emit digit-overlay g-code labelling each band (PA Tower only)",
    )

    @model_validator(mode="after")
    def _end_after_start(self) -> CalibTowerSpec:
        if self.end <= self.start:
            raise ValueError("end must be greater than start")
        return self


class FlowRateSpec(BaseModel):
    """Flow Rate has two passes — coarse (9 blocks) and fine (7 blocks).

    BS picks the geometry by ``pass_num`` and the bed by the operator's
    plate selection. No numeric sweep — modifiers are baked into the
    per-object names on the 3MF plate.
    """

    pass_num: Literal[1, 2] = Field(..., description="1 = coarse 9-block, 2 = fine 7-block")
    bed_type: str = Field(
        ...,
        description="BS bed name: 'Cool Plate', 'Engineering Plate', 'High Temp Plate', "
        "'Textured PEI Plate', 'Bambu Cool Plate SuperTack'",
    )


class PAPatternSpec(BaseModel):
    """PA Pattern uses a custom-drawn comb with M900 changes per row.

    Bigger range than PA Tower (typically 0.0 → 0.5 K) because the pattern
    fits more samples in less Z height. ``print_numbers`` /
    ``print_axis_titles`` toggle the digit + label overlays — defaults
    match BS's GUI defaults.
    """

    start: float = Field(..., ge=0)
    end: float = Field(..., gt=0)
    step: float = Field(..., gt=0)
    print_numbers: bool = True
    print_axis_titles: bool = True

    @model_validator(mode="after")
    def _end_after_start(self) -> PAPatternSpec:
        if self.end <= self.start:
            raise ValueError("end must be greater than start")
        return self


class AutoPASpec(BaseModel):
    """Auto-PA / Auto-Flow run printer-side over MQTT — no slicing knobs.

    Kept here as a placeholder so the discriminated-union covers every
    ``CaliMode``. ``start_calibration`` ignores any extra fields.
    """


# Discriminated union — routes accept ``CalibrationSpec`` and dispatch on
# the per-mode shape in ``calib_3mf_builder``. Field on the wire is
# ``kind`` so the JSON contract stays explicit about which spec the body
# decodes to.
CalibrationSpec = CalibTowerSpec | FlowRateSpec | PAPatternSpec | AutoPASpec
