from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator


class SpoolBase(BaseModel):
    material: str = Field(..., min_length=1, max_length=50)
    subtype: str | None = None
    color_name: str | None = None
    rgba: str | None = Field(None, pattern=r"^[0-9A-Fa-f]{8}$")
    brand: str | None = None
    label_weight: int = 1000
    core_weight: int = 250
    core_weight_catalog_id: int | None = None
    weight_used: float = 0
    # Anchor for the resettable "Total Consumed" display. The Inventory
    # page shows ``max(0, weight_used - weight_used_baseline)``; the
    # per-spool / bulk "Reset usage to 0" action sets ``baseline =
    # weight_used`` so the counter zeroes without touching remaining
    # (#1390, m075).
    weight_used_baseline: float = 0
    slicer_filament: str | None = None
    slicer_filament_name: str | None = None
    # Normalized GF-form filament_id for K-profile / colour matching
    # (base-resolved for custom presets). Set by the spool form at save time.
    resolved_filament_id: str | None = Field(default=None, max_length=50)
    nozzle_temp_min: int | None = None
    nozzle_temp_max: int | None = None
    note: str | None = None
    tag_uid: str | None = None
    tray_uuid: str | None = None
    data_origin: str | None = None
    tag_type: str | None = None
    cost_per_kg: float | None = Field(default=None, ge=0)
    purchase_date: datetime | None = None
    filament_diameter: str = Field(default="1.75", pattern=r"^(1\.75|2\.85)$")
    # ``gt=0`` — 0 is explicitly forbidden per the UI contract (treat "no
    # lot number" as NULL instead of zero).
    lot: int | None = Field(default=None, gt=0)
    weight_locked: bool = False
    last_scale_weight: int | None = None
    last_weighed_at: datetime | None = None
    # B.1 — multi-colour gradient stops (comma-separated 6/8-char hex
    # tokens, max 8) and visual effect overlay.
    extra_colors: str | None = Field(default=None, max_length=255)
    effect_type: str | None = Field(default=None, max_length=20)
    # B.8 — free-text category + per-spool low-stock threshold override (%).
    category: str | None = Field(default=None, max_length=50)
    low_stock_threshold_pct: int | None = Field(default=None, ge=1, le=99)
    # Free-text storage location ("Drybox #1", "Top shelf"), distinct from
    # ``location`` (AMS slot assignment). Column has lived on the ORM since
    # the inventory rework but was missing from this schema, so PATCH
    # writes were silently dropped by Pydantic and GET responses left the
    # field out — upstream Bambuddy #1291.
    storage_location: str | None = Field(default=None, max_length=255)


class SpoolCreate(SpoolBase):
    pass


class SpoolBulkCreate(BaseModel):
    spool: SpoolCreate
    quantity: int = Field(default=1, ge=1, le=100)
    # When set, overwrites ``spool.lot`` with 1..N for each generated row
    # so a purchase bundle gets sequential lot numbers in one submit.
    auto_increment_lot: bool = False


class SpoolUpdate(BaseModel):
    material: str | None = None
    subtype: str | None = None
    color_name: str | None = None
    rgba: str | None = Field(None, pattern=r"^[0-9A-Fa-f]{8}$")
    brand: str | None = None
    label_weight: int | None = None
    core_weight: int | None = None
    core_weight_catalog_id: int | None = None
    weight_used: float | None = None
    weight_used_baseline: float | None = None  # PATCH-able for "Reset usage to 0" parity (#1390)
    slicer_filament: str | None = None
    slicer_filament_name: str | None = None
    resolved_filament_id: str | None = Field(default=None, max_length=50)
    nozzle_temp_min: int | None = None
    nozzle_temp_max: int | None = None
    note: str | None = None
    tag_uid: str | None = None
    tray_uuid: str | None = None
    data_origin: str | None = None
    tag_type: str | None = None
    cost_per_kg: float | None = Field(default=None, ge=0)
    purchase_date: datetime | None = None
    filament_diameter: str | None = Field(default=None, pattern=r"^(1\.75|2\.85)$")
    lot: int | None = Field(default=None, gt=0)
    weight_locked: bool | None = None
    extra_colors: str | None = Field(default=None, max_length=255)
    effect_type: str | None = Field(default=None, max_length=20)
    category: str | None = Field(default=None, max_length=50)
    low_stock_threshold_pct: int | None = Field(default=None, ge=1, le=99)
    storage_location: str | None = Field(default=None, max_length=255)


class SpoolBulkUpdate(BaseModel):
    """Apply a partial field set to many spools at once.

    ``fields`` carries only the columns the user chose to change (the route
    reads them with ``exclude_unset``). Usage / identity columns are stripped
    server-side regardless of what's sent — bulk edit must never touch consumed
    weight or copy an RFID UID across physical spools.
    """

    spool_ids: list[int] = Field(min_length=1)
    fields: SpoolUpdate


class SpoolKProfileBase(BaseModel):
    """Wire shape for the PA tab — frontend posts (printer_id, extruder,
    nozzle_diameter, k_value, name, cali_idx, setting_id) and the backend
    resolves cali_idx → find-or-create a ``filament_calibration`` row, then
    creates the link row. This shape is unchanged for backwards compat with
    the existing UI; the data is sourced from the printer's live K-profile
    list (``extrusion_cali_get``) at fill time."""

    printer_id: int
    extruder: int = 0
    nozzle_diameter: str = "0.4"
    nozzle_type: str | None = None
    k_value: float
    name: str | None = None
    cali_idx: int | None = None
    setting_id: str | None = None


class SpoolKProfileResponse(BaseModel):
    """Response shape for the PA tab — same fields as the input, plus link
    bookkeeping. The link row stores only ``filament_calibration_id`` after
    m064; this validator pulls k_value/name/etc. off the joined cache row
    so every existing caller building this from ORM (`from_attributes`) keeps
    working unchanged."""

    id: int
    spool_id: int
    printer_id: int
    extruder: int = 0
    nozzle_diameter: str = "0.4"
    nozzle_type: str | None = None
    k_value: float | None = None
    name: str | None = None
    cali_idx: int | None = None
    setting_id: str | None = None
    auto_linked: bool = False
    created_at: datetime

    class Config:
        from_attributes = True

    @model_validator(mode="before")
    @classmethod
    def _enrich_from_link(cls, data: Any) -> Any:
        # Only enrich ORM-row inputs (from_attributes path). Dict / Pydantic
        # objects coming over the wire are already fully shaped.
        if isinstance(data, dict):
            return data
        fc = getattr(data, "filament_calibration", None)
        if fc is None:
            return data
        # Build a dict that pydantic will validate. Pull link-side fields
        # first, then layer filament_calibration values on top.
        return {
            "id": getattr(data, "id", None),
            "spool_id": getattr(data, "spool_id", None) or getattr(data, "spoolman_spool_id", None),
            "printer_id": getattr(data, "printer_id", None),
            "extruder": getattr(data, "extruder", 0),
            "auto_linked": getattr(data, "auto_linked", False),
            "created_at": getattr(data, "created_at", None),
            "nozzle_diameter": str(fc.nozzle_diameter) if fc.nozzle_diameter is not None else "0.4",
            "nozzle_type": fc.nozzle_volume_type,
            "k_value": fc.pa_k_value if fc.pa_k_value is not None else fc.flow_ratio,
            "name": fc.name,
            "cali_idx": fc.cali_idx,
            "setting_id": fc.filament_setting_id,
        }


class SpoolResponse(SpoolBase):
    id: int
    # rgba is intentionally unconstrained on the response side: the write paths
    # (SpoolCreate, SpoolUpdate) enforce the 8-char hex pattern, but legacy
    # rows or data sourced from AMS firmware / backups may carry malformed
    # values. A single bad row must not 500 the entire inventory list endpoint
    # (upstream #1055).
    rgba: str | None = None
    added_full: bool | None = None
    last_used: datetime | None = None
    encode_time: datetime | None = None
    tag_uid: str | None = None
    tray_uuid: str | None = None
    data_origin: str | None = None
    tag_type: str | None = None
    archived_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    k_profiles: list[SpoolKProfileResponse] = []

    class Config:
        from_attributes = True


class SpoolAssignmentCreate(BaseModel):
    spool_id: int
    printer_id: int
    ams_id: int
    tray_id: int


class SpoolAssignmentResponse(BaseModel):
    id: int
    spool_id: int
    printer_id: int
    printer_name: str | None = None
    ams_id: int
    tray_id: int
    fingerprint_color: str | None = None
    fingerprint_type: str | None = None
    created_at: datetime
    spool: SpoolResponse | None = None
    configured: bool = False
    # True when the target slot was empty at assign time so MQTT was deferred.
    # `on_ams_change` replays the configuration when filament is later inserted.
    # Bambu firmware silently drops ams_filament_setting / extrusion_cali_sel
    # for unloaded slots (no filament context for cali_idx to attach to), so
    # pre-load assignment must skip the publish and arm a replay.
    pending_config: bool = False
    ams_label: str | None = None  # User-defined friendly name for the AMS unit

    class Config:
        from_attributes = True
