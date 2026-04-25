export interface PlateFilament {
  slot_id: number;
  type: string;
  color: string;
  used_grams: number;
  used_meters: number;
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
}

export interface LibraryFilePlatesResponse {
  file_id: number;
  filename: string;
  plates: PlateMetadata[];
  is_multi_plate: boolean;
}

export interface ViewerPlateSelectionState {
  selected_plate_id: number | null;
}

export interface PlateAssignment {
  object_id: string;
  plate_id: number | null;
}
