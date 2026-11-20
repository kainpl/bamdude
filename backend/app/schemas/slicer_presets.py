"""Pydantic schemas for the unified slicer-presets endpoint.

The SliceModal pulls printer/process/filament options from four sources, in
priority order: local (DB-backed imported profiles), orca_cloud, cloud (the
user's Bambu Cloud account), and standard (slicer-bundled stock profiles).
The endpoint returns every tier's full list — no cross-tier dedup — so a
preset name that exists in several tiers appears in each group and the user
can pick any source. The order only drives auto-pick and group rendering.
"""

from typing import Literal

from pydantic import BaseModel

CloudStatus = Literal["ok", "not_authenticated", "expired", "unreachable"]


class UnifiedPreset(BaseModel):
    """A single printer/process/filament preset with its source.

    The ``id`` shape varies by source:
      - cloud  → Bambu Cloud setting_id (e.g. ``"PFUS9ac902733670a9"``)
      - local  → stringified DB row id from ``local_presets``
      - standard → preset name as written in the bundled JSON (the slicer
                   resolves bundled profiles by name during inheritance walk)

    The frontend treats ``id`` as opaque; the slice dispatch path uses
    ``(source, id)`` to fetch / pass the preset content to the sidecar.

    ``filament_type`` and ``filament_colour`` are populated for the filament
    slot only — they let the SliceModal pre-pick a preset per plate slot in
    the multi-color flow by matching against the source 3MF's per-slot type
    and color. Populated when the underlying preset JSON exposes them; left
    as ``None`` on bundled profiles where colour is a runtime spool attribute.
    """

    id: str
    name: str
    source: Literal["orca_cloud", "cloud", "local", "standard"]
    filament_type: str | None = None
    filament_colour: str | None = None
    # The filament preset's stored ``filament_flow_ratio`` (a per-extruder
    # vector in BS — we surface the first value). Populated for the
    # filament slot only; used by the Flow Rate verify-download page to
    # auto-prefill the baseline input with the operator's current value
    # instead of forcing them to type it. ``None`` when the resolver
    # didn't expose it (typical for thin cloud-delta stubs).
    filament_flow_ratio: float | None = None
    # The slicer's own ``compatible_printers`` list (process / filament slots).
    # Drives the SliceModal + calibration printer-aware filtering (#1325):
    # a preset is compatible with a printer when this list names it. Populated
    # for the local tier (the imported preset JSON carries it); ``None`` on
    # cloud / standard stubs, where the matcher falls back to bundle membership
    # then the ``@BBL <model> <nozzle>`` name convention.
    compatible_printers: list[str] | None = None
    # Printer slot only: the NAME of the preset this one was saved from, for a
    # preset that is not a system preset (upstream 3acc52d93, #3250). A printer
    # preset saved under a name of its own ("Bambu Lab H2D 0.4 nozzle - Skew")
    # is in no ``compatible_printers`` list and its name may not state a model;
    # the slicer checks a process against this parent, and so does the dialog.
    # ⚠️ ``None`` on a system preset even though its JSON has an ``inherits``:
    # "Bambu Lab H2D 0.6 nozzle" inherits the 0.4 preset as file layout, and
    # reading that as "saved from" would offer 0.4 processes to a 0.6 nozzle.
    inherits: str | None = None


class UnifiedPresetsBySlot(BaseModel):
    """Three slots in the order Bambu Studio / OrcaSlicer use."""

    printer: list[UnifiedPreset] = []
    process: list[UnifiedPreset] = []
    filament: list[UnifiedPreset] = []


class FilamentPresetInfo(BaseModel):
    """Flow-rate-relevant metadata for one filament preset.

    Returned by ``GET /slicer/filament-preset/info`` — Flow Rate's
    verify-download page calls this on filament pick to auto-prefill
    the pass-1 baseline input with the operator's stored
    ``filament_flow_ratio``. Heavier than the listing (full preset
    resolution via :func:`resolve_preset_ref`), so fetched on-demand
    rather than baked into every entry of ``/slicer/presets``.
    """

    flow_ratio: float | None = None
    filament_type: str | None = None


class UnifiedPresetsResponse(BaseModel):
    """Every tier carries its full preset list — no cross-tier dedup.

    Priority order: ``local > orca_cloud > cloud > standard``. The order
    drives auto-pick (first non-empty tier wins, name-lookup walks tiers in
    this order, filament scoring tiebreaks by per-tier bonus) and determines
    the visual rendering order of the SliceModal's optgroups, but a name that
    exists in multiple tiers appears in EACH of their groups so the user can
    pick any source.

    ``cloud_status`` / ``orca_cloud_status`` let the frontend show a banner
    explaining why a cloud tier is empty when the user expected to see it
    (signed out / token expired / network down). Each tier has its own status
    because they can fail independently.
    """

    orca_cloud: UnifiedPresetsBySlot = UnifiedPresetsBySlot()
    cloud: UnifiedPresetsBySlot = UnifiedPresetsBySlot()
    local: UnifiedPresetsBySlot = UnifiedPresetsBySlot()
    standard: UnifiedPresetsBySlot = UnifiedPresetsBySlot()
    orca_cloud_status: CloudStatus = "ok"
    cloud_status: CloudStatus = "ok"


class LoadedFilament(BaseModel):
    """One filament loaded on an online printer — the SPOOL, never what the slot
    was advertised as (see ``services/slice_loaded_filaments``)."""

    kind: Literal["ams", "external"]
    material: str
    # The filament id of the spool's family, when BamDude knows a family by it.
    family_id: str | None = None
    # The source states only a material (no family, a Generic one, or a borrowed
    # profile): presets of the same material type match it.
    by_material: bool = False


class OnlinePrinter(BaseModel):
    printer_id: int
    # Normalised short model ("X1C"); ``accepts`` are the file models this
    # printer takes besides its own, from the mirrored printer configs.
    model: str | None = None
    accepts: list[str] = []
    nozzle_diameters: list[float] = []
    filaments: list[LoadedFilament] = []


class FarmPrinter(BaseModel):
    """A printer of the farm, whatever its state — online, paused, offline or in
    maintenance; only an archived printer is no longer of the farm (spec
    slicing-setup D4: "Only models of my farm"). Nozzles are read from a live
    connection only; an unknown fit is ``[]`` and excludes no size."""

    printer_id: int
    model: str | None = None
    accepts: list[str] = []
    nozzle_diameters: list[float] = []


class FamilyPresetRefs(BaseModel):
    """The presets that ARE one family, by the ``id`` each tier of
    ``/slicer/presets`` lists them under."""

    name: str
    cloud: list[str] = []
    orca_cloud: list[str] = []
    local: list[str] = []
    standard: list[str] = []


class LoadedFilamentsResponse(BaseModel):
    printers: list[OnlinePrinter] = []
    farm_printers: list[FarmPrinter] = []
    families: dict[str, FamilyPresetRefs] = {}
