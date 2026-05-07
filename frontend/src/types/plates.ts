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
  // Bound printer model from the source 3MF's project_settings.config (e.g.
  // "Bambu Lab A1"). Used by the SliceModal to warn before slicing if the
  // user picks a profile for a different printer — the slicer CLI cannot
  // convert a 3MF across printer models.
  source_printer_model?: string | null;
}

export interface LibraryFilePlatesResponse {
  file_id: number;
  filename: string;
  plates: PlateMetadata[];
  is_multi_plate: boolean;
  source_printer_model?: string | null;
}

export interface ViewerPlateSelectionState {
  selected_plate_id: number | null;
}

export interface PlateAssignment {
  object_id: string;
  plate_id: number | null;
}
