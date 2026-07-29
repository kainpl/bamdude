export interface PlateFilament {
  slot_id: number;
  type: string;
  color: string;
  used_grams: number;
  used_meters: number;
  // True when this AMS slot is consumed by the picked plate. False means
  // the slot is configured project-wide but the picked plate doesn't
  // paint with it. Sliced 3MFs (.gcode.3mf) report only used filaments
  // so the field is true for every entry. Unsliced project files report
  // ALL project slots; SliceModal disables the unused rows so the user
  // only interacts with the dropdowns that matter, while the backend
  // still passes the complete list to the slicer CLI to prevent silent
  // fallback to embedded defaults.
  used_in_plate?: boolean;
}

export interface PlateMetadata {
  index: number;
  name: string | null;
  objects: string[];
  object_count?: number;
  has_thumbnail: boolean;
  thumbnail_url: string | null;
  print_time_seconds: number | null;
  filament_used_grams: number | null;
  filaments: PlateFilament[];
  // Skip-objects + label-object metadata (added 0.4.1+).
  // ``printable_objects`` is keyed by identify_id so the printer can address
  // each one via ``M623`` directly. ``gcode_label_objects`` + ``exclude_object``
  // are file-global slicer flags duplicated per plate for UI convenience —
  // both must be true for the skip-objects button to be functional.
  printable_objects?: Record<number, string>;
  bbox_all?: [number, number, number, number] | null;
  gcode_label_objects?: boolean;
  exclude_object?: boolean | null;
  bed_type?: string | null; // Per-plate build plate type (#1281)
}

export interface ArchivePlatesResponse {
  archive_id: number;
  filename: string;
  plates: PlateMetadata[];
  is_multi_plate: boolean;
  // True when the on-disk container actually carries sliced gcode.
  // Source-only project 3MFs (no slice) have plates with thumbnails and
  // filament info but no gcode payload, so the gcode-tab in
  // ModelViewerModal can't render anything for them — the modal falls
  // through to the model-only view when this is false. Optional for
  // backwards compatibility with cached responses from before the field
  // was added.
  has_gcode?: boolean;
  // Printer / process preset names the 3MF was prepared with (read from
  // Metadata/project_settings.config). The SliceModal defaults its printer +
  // process dropdowns to these when the matching presets exist in the listing,
  // instead of blindly taking the first preset (#1325). Optional / nullable:
  // absent on cached responses from before the fields were added, null when
  // the 3MF carries no embedded preset ids.
  embedded_printer?: string | null;
  embedded_process?: string | null;
}

export interface LibraryFilePlatesResponse {
  file_id: number;
  filename: string;
  plates: PlateMetadata[];
  is_multi_plate: boolean;
  // See ArchivePlatesResponse.embedded_printer / embedded_process (#1325).
  embedded_printer?: string | null;
  embedded_process?: string | null;
}

export interface ViewerPlateSelectionState {
  selected_plate_id: number | null;
}

export interface PlateAssignment {
  object_id: string;
  plate_id: number | null;
}

/** Read-only plate object preview — GET /{library|archives}/…/plate-objects.
 *
 * Deliberately has no `skipped` field. Nothing in the preview is skippable —
 * the live SkipObjectsModal owns that — and an always-false flag would be an
 * invitation to grow one.
 */
export interface PlateObjectItem {
  id: number;
  name: string;
  // Normalised pick-PNG centroid when `norm` is true, millimetres otherwise,
  // null when the object appears in no positional source at all (markerPosition
  // then lays it out on its tier-4 grid).
  x: number | null;
  y: number | null;
  norm: boolean;
}

export interface PlateObjectsResponse {
  plate_index: number;
  objects: PlateObjectItem[];
  bbox_all: number[] | null;
  // True when NOT ONE object had a pick-PNG centroid: every marker is on the
  // fallback grid and the layout drawn is plausible-looking fiction.
  positions_approximate: boolean;
  // gcode_label_objects AND exclude_object, read live from the 3MF.
  skip_objects_supported: boolean;
  // False when Metadata/top_N.png is absent. The modal then shows the list with
  // NO image — markers are positioned in top-down space and would sit
  // convincingly on the wrong parts of a 3/4 render.
  has_top_view: boolean;
}
