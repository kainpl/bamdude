"""Pydantic schemas for the unified slicer-presets endpoint.

The SliceModal pulls printer/process/filament options from three sources, in
priority order: cloud (the user's Bambu Cloud account), local (DB-backed
imported profiles), and standard (slicer-bundled stock profiles). The endpoint
returns all three lists with name-based dedup applied so each preset appears
exactly once across the response.
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
    source: Literal["cloud", "local", "standard"]
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
    """Each tier carries only the names that didn't appear in a higher tier.

    Cloud is the highest priority (user's personal customisations win), then
    the local imports the user explicitly curated, then the slicer's stock
    fallback. A name that appears in cloud is filtered out of local and
    standard; a name that appears in local is filtered out of standard.

    ``cloud_status`` lets the frontend show a banner explaining why the cloud
    tier is empty when the user expected to see it (signed out / token
    expired / network down).
    """

    cloud: UnifiedPresetsBySlot = UnifiedPresetsBySlot()
    local: UnifiedPresetsBySlot = UnifiedPresetsBySlot()
    standard: UnifiedPresetsBySlot = UnifiedPresetsBySlot()
    cloud_status: CloudStatus = "ok"
