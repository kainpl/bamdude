"""Pydantic schemas for slice requests (Phase 1 of 0.5.x slicer cycle)."""

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class PresetRef(BaseModel):
    """A source-aware reference to a printer / process / filament preset.

    The SliceModal pulls dropdown options from four tiers (orca_cloud /
    cloud / local / standard). At submit time the client sends one of these
    per slot so the backend knows where to fetch the preset content from at
    slice time. ``cloud`` is Bambu Cloud (kept as the bare name for backward
    compatibility with existing requests); ``orca_cloud`` is Orca Cloud.
    """

    source: Literal["orca_cloud", "cloud", "local", "standard"]
    id: str = Field(
        ...,
        description=(
            "Orca Cloud profile id, Bambu Cloud setting_id, local DB row id (stringified), or standard preset name."
        ),
    )


class SliceRequest(BaseModel):
    """Body for ``POST /library/files/{file_id}/slice``.

    Two preset shapes are accepted per slot for backwards-compatibility:

    - **Legacy** — bare integer ``*_preset_id`` fields point into the
      ``local_presets`` table. Existing clients (and stale browser tabs after
      a BamDude upgrade) keep working unchanged.
    - **Source-aware** — ``*_preset`` carries an explicit ``{source, id}``.
      Required for cloud / standard tiers; also accepted (and equivalent)
      for local presets when the client is on the new modal.

    Exactly one of each pair must be set; the validator normalises legacy
    integer ids into a ``PresetRef(source='local', id=str(id))`` so the
    downstream resolver only deals with one shape.
    """

    # Legacy fields — kept optional so older clients continue to work.
    printer_preset_id: int | None = Field(
        default=None,
        description="DEPRECATED: prefer printer_preset. LocalPreset id with preset_type='printer'.",
    )
    process_preset_id: int | None = Field(
        default=None,
        description="DEPRECATED: prefer process_preset. LocalPreset id with preset_type='process'.",
    )
    filament_preset_id: int | None = Field(
        default=None,
        description="DEPRECATED: prefer filament_preset. LocalPreset id with preset_type='filament'.",
    )

    # Source-aware fields — set by the new SliceModal.
    printer_preset: PresetRef | None = None
    process_preset: PresetRef | None = None
    filament_preset: PresetRef | None = None

    # Multi-color: one PresetRef per AMS slot the source plate uses. Order is
    # significant — the slicer matches index-by-index against the plate's
    # filament slots. Always preferred over the legacy singular field; the
    # validator promotes a singular field into ``[singular]`` when the list
    # is empty so older clients keep working.
    filament_presets: list[PresetRef] = Field(default_factory=list)

    plate: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Plate number to slice. ``None`` defaults to plate 1 on the sidecar "
            "(pre-multi-plate behaviour). ``0`` is the 'all plates' sentinel — "
            "produces a single multi-plate 3MF covering every plate. ``>= 1`` "
            "slices that one plate."
        ),
    )
    export_3mf: bool = Field(
        default=False,
        description="If true, request a 3MF response with embedded G-code instead of raw G-code.",
    )
    process_overrides: dict[str, Any] | None = Field(
        default=None,
        description=(
            "The user's own process-setting edits from the slice dialog's settings panel, as a "
            "sparse ``{option_key: value}`` map (layer height, wall count, supports, speeds — "
            "OrcaSlicer's process parameter set). Written into the process JSON AFTER the "
            "source's support settings and the designer's carried tweaks, so an explicit choice "
            "here wins over both. Values are normalised to the string forms a process preset "
            "stores; keys that are not valid config keys are dropped rather than failing the "
            "slice. None/empty leaves the picked preset untouched."
        ),
    )
    arrange: bool = Field(
        default=False,
        description=(
            "Run the slicer's auto-arrange pass, repositioning objects on the bed before slicing. "
            "Off by default; unions with the automatic cross-nozzle-class arrange rather than "
            "replacing it."
        ),
    )
    orient: bool = Field(
        default=False,
        description=(
            "Run the slicer's auto-orientation pass: score candidate rotations (overhang area, "
            "contour, unprintability) and rotate each object onto the best one. Off by default and "
            "user-driven only — rotating a deliberately laid-out model is not a change to make "
            "silently."
        ),
    )
    # Bed plate override (sidecar maps to ``--curr-bed-type``). Mirrors the
    use_embedded_settings: bool = Field(
        default=False,
        description=(
            "3MF only. Slice using the file's embedded "
            "``Metadata/project_settings.config`` (the designer's own tweaks — wall "
            "count, infill, etc.) instead of the picked printer/process/filament "
            "triplet. This is the 'slice as designed' path: no ``--load-settings`` "
            "override, so a MakerWorld author's settings survive. Ignored for STL / "
            "plain-model 3MF (no embedded profile to honour). The preset refs are "
            "still required by the validator but go unused on this path. Only makes "
            "sense when the picked printer matches the design's target model — the "
            "UI gates the toggle on that; there is no cross-printer re-targeting "
            "here (that is exactly what the profile path is for)."
        ),
    )
    design_overrides: list[str] | None = Field(
        default=None,
        description=(
            "3MF only. Process setting keys the file's designer changed away from "
            "the stock preset, to carry onto the picked process profile (#2622). "
            "Named individually and authoritative: a key not listed here is not "
            "applied, and a key listed here is applied only if the source really "
            "records it as changed. Distinct from ``use_embedded_settings``, which "
            "is all-or-nothing and only works when the picked printer already "
            "matches the design's target — this is the cross-printer path, where "
            "some of the designer's values are intent (walls, infill) and some are "
            "tuned for their machine (speeds, accelerations, temperatures)."
        ),
    )
    # five enum values BambuStudio's ``curr_bed_type`` accepts (see
    # libslic3r/PrintConfig.cpp:1069+). Without this override the CLI falls
    # back to per-plate value baked into the source 3MF (when present) and
    # finally to ``Cool Plate`` (the upstream config default) — wrong for
    # Textured PEI users on STL inputs. The SliceModal sends this from a
    # dedicated picker so adhesion temps land on the actual plate.
    bed_type: (
        Literal[
            "Cool Plate",
            "Engineering Plate",
            "High Temp Plate",
            "Textured PEI Plate",
            "Supertack Plate",
        ]
        | None
    ) = Field(
        default=None,
        description=(
            "Override the slicer's ``curr_bed_type``. Forwarded to the sidecar's "
            "``bedType`` form field which becomes ``--curr-bed-type`` on the CLI. "
            "Null leaves the slicer's own resolution (3MF embedded → config default)."
        ),
    )
    slicer: Literal["orcaslicer", "bambu_studio"] | None = Field(
        default=None,
        description=(
            "Per-job slicer override. When the user has both OrcaSlicer and BambuStudio "
            "URLs configured, the SliceModal exposes a radio so the slicer can be picked "
            "per source file. Falls back to the global preferred_slicer setting when null."
        ),
    )

    @model_validator(mode="after")
    def normalise_preset_refs(self) -> "SliceRequest":
        """Each slot must end up with a ``PresetRef`` set. Legacy integer ids
        become ``(source='local', id=str(int))`` so the route handler only
        deals with the canonical shape. For filament: a non-empty
        ``filament_presets`` list satisfies the requirement on its own; an
        empty list falls back to the singular fields, which then promote
        into a one-element list.
        """
        for slot, ref_attr, legacy_attr in (
            ("printer", "printer_preset", "printer_preset_id"),
            ("process", "process_preset", "process_preset_id"),
        ):
            ref = getattr(self, ref_attr)
            legacy_id = getattr(self, legacy_attr)
            if ref is None and legacy_id is None:
                raise ValueError(
                    f"{slot} preset is required: provide '{ref_attr}' (preferred) or legacy '{legacy_attr}'"
                )
            if ref is None:
                setattr(self, ref_attr, PresetRef(source="local", id=str(legacy_id)))

        # Filament accepts THREE shapes, in priority order:
        #   1. filament_presets    — multi-color array (new clients)
        #   2. filament_preset     — source-aware singular (single-color new clients)
        #   3. filament_preset_id  — legacy bare integer (old clients)
        # The first non-empty shape wins; missing all three raises.
        if not self.filament_presets:
            if self.filament_preset is not None:
                self.filament_presets = [self.filament_preset]
            elif self.filament_preset_id is not None:
                fallback = PresetRef(source="local", id=str(self.filament_preset_id))
                self.filament_preset = fallback
                self.filament_presets = [fallback]
            else:
                raise ValueError(
                    "filament preset is required: provide 'filament_presets' (preferred), "
                    "'filament_preset', or legacy 'filament_preset_id'"
                )
        elif self.filament_preset is None:
            # Multi-color caller: backfill the singular from the first slot
            # so callers that still read the legacy field see a stable value.
            self.filament_preset = self.filament_presets[0]
        return self


class SliceResponse(BaseModel):
    """Response from ``POST /library/files/{file_id}/slice``. The result lands
    in the user's library as a new ``LibraryFile`` (in the same folder as
    the source)."""

    library_file_id: int
    name: str
    print_time_seconds: int
    filament_used_g: float
    filament_used_mm: float
    used_embedded_settings: bool = False
    # Set when the source lives in an external folder that could not receive
    # the result (read-only, unreachable, not writable), so the file went to
    # managed storage instead — and names which of those it was. ``None`` on
    # every normal slice.
    #
    # ⚠️ Reported rather than silently absorbed. Filing the output somewhere the
    # user is not looking, with no signal, is exactly what made this
    # unreproducible from the UI.
    external_write_fallback: str | None = None


class SliceArchiveResponse(BaseModel):
    """Response from ``POST /archives/{archive_id}/slice``. The result lands
    in the user's archives as a new ``PrintArchive`` row, inheriting
    printer / project metadata from the source archive."""

    archive_id: int
    name: str
    print_time_seconds: int
    filament_used_g: float
    filament_used_mm: float
    used_embedded_settings: bool = False
