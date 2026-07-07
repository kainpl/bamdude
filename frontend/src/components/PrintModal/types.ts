import type { PrintQueueItem, Printer } from '../../api/client';

/**
 * Mode of operation for the PrintModal.
 * - 'reprint': Immediate print from archive (no schedule options)
 * - 'add-to-queue': Schedule print to queue (includes schedule options)
 * - 'edit-queue-item': Edit existing queue item (all options + existing values)
 */
export type PrintModalMode = 'reprint' | 'add-to-queue' | 'edit-queue-item';

/**
 * Props for the unified PrintModal component.
 *
 * Either archiveId or libraryFileId must be provided.
 * - archiveId: For reprinting/queueing archives
 * - libraryFileId: For printing library files directly
 */
export interface PrintModalProps {
  /** Modal operation mode */
  mode: PrintModalMode;
  /** Archive ID to print (mutually exclusive with libraryFileId) */
  archiveId?: number;
  /** Library file ID to print (mutually exclusive with archiveId) */
  libraryFileId?: number;
  /** Display name for the print */
  archiveName: string;
  /** Existing queue item (only for edit-queue-item mode) */
  queueItem?: PrintQueueItem;
  /** Pre-select specific printers when opening the modal */
  initialSelectedPrinterIds?: number[];
  /** Handler for closing the modal */
  onClose: () => void;
  /** Handler for successful operation */
  onSuccess?: () => void;
  /** Project ID to associate the resulting archive with (only when triggered from project view) */
  projectId?: number;
  /** Delete the LibraryFile after dispatch — used by the Printers-page Direct-Print flow
   *  so transient uploads don't linger in File Manager. Only applies to library-file prints. */
  cleanupLibraryAfterDispatch?: boolean;
  /** Initial value for the dispatch-mode toggle ('specific' picks printers,
   *  'auto' routes via auto-queue). Defaults to 'specific'. Only meaningful
   *  for add-to-queue mode. */
  initialDispatchMode?: 'specific' | 'auto';
  /** When true, hide the dispatch-mode toggle so the operator can't switch
   *  between 'specific' and 'auto'. Used by drop-to-queue flows where the
   *  drop target itself implies the mode (queue card → specific, auto-queue
   *  panel → auto). Only meaningful for add-to-queue mode. */
  lockDispatchMode?: boolean;
}

/**
 * Per-item preheat / heat-soak override mode (#1468).
 * - 'inherit': use the global Settings → Printing preheat_enabled toggle
 * - 'on' / 'off': force the per-print decision regardless of the global toggle
 */
export type PreheatOverride = 'inherit' | 'on' | 'off';

/**
 * Print options that can be configured for a print job.
 */
export interface PrintOptions {
  bed_levelling: boolean;
  flow_cali: boolean;
  layer_inspect: boolean;
  timelapse: boolean;
  mesh_mode_fast_check: boolean;
  /** Inject operator-defined G-code snippets at MACHINE_START_GCODE_END / EOF (#422). */
  gcode_injection: boolean;
  /** Nozzle offset calibration before print — dual-nozzle printers only (#1682).
   *  The MQTT layer forces "skip" on single-nozzle machines regardless. */
  nozzle_offset_cali: boolean;
  // Per-item preheat / heat-soak override (#1468). 'inherit' uses the global
  // Settings → Printing toggle; 'on' / 'off' force the per-print decision.
  // chamber_target_override is non-null to bypass the per-filament-type
  // derivation with an explicit °C target.
  preheat_override: PreheatOverride;
  preheat_chamber_target_override: number | null;
}

/**
 * Default print options values.
 */
export const DEFAULT_PRINT_OPTIONS: PrintOptions = {
  bed_levelling: true,
  flow_cali: true,
  layer_inspect: false,
  timelapse: false,
  mesh_mode_fast_check: true,
  gcode_injection: false,
  nozzle_offset_cali: true,
  preheat_override: 'inherit',
  preheat_chamber_target_override: null,
};

/**
 * Swap-mode macro events that can be toggled per print job.
 * Hardcoded because the swap flow only uses these two events.
 */
export const SWAP_MACRO_EVENTS = ['swap_mode_start', 'swap_mode_change_table'] as const;
export type SwapMacroEvent = typeof SWAP_MACRO_EVENTS[number];

/**
 * Swap-macro execution intent for a single print job.
 * `events` is the subset of `SWAP_MACRO_EVENTS` the operator wants to fire.
 */
export interface SwapMacrosOptions {
  execute: boolean;
  events: SwapMacroEvent[];
}

export const DEFAULT_SWAP_MACROS_OPTIONS: SwapMacrosOptions = {
  execute: true,
  events: [...SWAP_MACRO_EVENTS],
};

/**
 * Schedule type for queue items.
 */
export type ScheduleType = 'asap' | 'scheduled' | 'manual';

/**
 * Schedule options for queue items.
 */
export interface ScheduleOptions {
  scheduleType: ScheduleType;
  scheduledTime: string;
  autoOffAfter: boolean;
}

/**
 * Default schedule options values.
 */
export const DEFAULT_SCHEDULE_OPTIONS: ScheduleOptions = {
  scheduleType: 'asap',
  scheduledTime: '',
  autoOffAfter: false,
};

/**
 * Auto-distribute mode options. Used by the AutoModeOptions panel
 * when the operator picks "Auto" instead of a specific printer.
 */
export interface AutoModeOptionsState {
  target_model: string | null;
  target_location: string | null;
  force_color_match: boolean;
}

export const DEFAULT_AUTO_MODE_OPTIONS: AutoModeOptionsState = {
  target_model: null,
  target_location: null,
  force_color_match: false,
};

/**
 * Plate information from a multi-plate 3MF file.
 *
 * Mirrors the backend ``/library/files/{id}/plates`` and
 * ``/archives/{id}/plates`` response shape — see ``services/archive.py
 * ::parse_plates_from_3mf`` for the parser.
 */
export interface PlateInfo {
  index: number;
  name: string | null;
  has_thumbnail: boolean;
  thumbnail_url: string | null;
  objects: string[];
  /** Counted from per-instance ``identify_id`` (skipped="false"); may exceed
   *  ``objects.length`` when one model is duplicated across the plate. */
  object_count?: number;
  filaments: Array<{
    slot_id?: number;
    type: string;
    color: string;
    used_grams?: number;
    used_meters?: number;
  }>;
  print_time_seconds: number | null;
  filament_used_grams: number | null;
  bed_type?: string | null; // Per-plate build plate type (#1281)
}

/**
 * Response from the archive plates API.
 */
export interface PlatesResponse {
  is_multi_plate: boolean;
  plates: PlateInfo[];
}

/**
 * Props for the PrinterSelector component.
 */
export interface PrinterSelectorProps {
  printers: Printer[];
  selectedPrinterIds: number[];
  onMultiSelect: (printerIds: number[]) => void;
  isLoading?: boolean;
  allowMultiple?: boolean;
  /** Show inactive printers (for edit mode where original assignment may be inactive) */
  showInactive?: boolean;
  /** Disable selection of busy printers (used in reprint mode) */
  disableBusy?: boolean;
  /** Suggested model from sliced file (for pre-selection) */
  slicedForModel?: string | null;
  /** File is swap mode compatible - filter to swap-enabled printers only */
  swapCompatible?: boolean;
}

/**
 * Props for the PlateSelector component.
 */
export interface PlateSelectorProps {
  plates: PlateInfo[];
  isMultiPlate: boolean;
  selectedPlates: Set<number>;
  onToggle: (plateIndex: number) => void;
  onSelectAll?: () => void;
  onDeselectAll?: () => void;
  /** Whether multi-select (checkboxes) is enabled - true in add-to-queue mode */
  multiSelect?: boolean;
}

/**
 * Filament requirement data structure.
 */
export interface FilamentReqsData {
  filaments: Array<{
    slot_id: number;
    type: string;
    color: string;
    used_grams: number;
    used_meters: number;
    nozzle_id?: number;
    /** Bambu SKU code from the 3MF (e.g. `GFA01` = Bambu PLA Matte, `P4d64437`
     *  = user custom). Used to resolve the "original" filament label in
     *  FilamentOverride / FilamentMapping against the builtin + cloud
     *  user-preset maps. #1718. */
    tray_info_idx?: string;
  }>;
}

/**
 * Props for the FilamentMapping component.
 */
export interface FilamentMappingProps {
  printerId: number;
  /** Pre-fetched filament requirements data */
  filamentReqs: FilamentReqsData | undefined;
  manualMappings: Record<number, number>;
  onManualMappingChange: (mappings: Record<number, number>) => void;
  currencySymbol: string;
  defaultCostPerKg: number;
  /** Per-slot force-color-match flags. Upstream #1717 surfaces this checkbox in
   *  specific-printer mode. In BamDude the specific-printer path pins an explicit
   *  ams_mapping (PrintQueueItem has no force_color_match field), so these props
   *  are optional and only render the checkbox when a caller wires the handler. */
  forceColorMatch?: Record<number, boolean>;
  /** Called when a slot's force-color-match checkbox is toggled. */
  onForceColorMatchChange?: (slotId: number, value: boolean) => void;
}

/**
 * Props for the PrintOptions component.
 */
export interface PrintOptionsProps {
  options: PrintOptions;
  onChange: (options: PrintOptions) => void;
  defaultExpanded?: boolean;
  /** Show the dual-nozzle-only options (nozzle offset calibration). Default false.
   *  Pass true when at least one selected printer is dual-nozzle. */
  showDualNozzleOptions?: boolean;
}

/**
 * Props for the SwapMacros panel.
 */
export interface SwapMacrosPanelProps {
  options: SwapMacrosOptions;
  onChange: (options: SwapMacrosOptions) => void;
}

/**
 * Props for the ScheduleOptions component.
 */
export interface ScheduleOptionsProps {
  options: ScheduleOptions;
  onChange: (options: ScheduleOptions) => void;
  /** Date format setting from user preferences */
  dateFormat?: 'system' | 'us' | 'eu' | 'iso';
  /** Time format setting from user preferences */
  timeFormat?: 'system' | '12h' | '24h';
  /** Whether the user has permission to control printers (for auto power off) */
  canControlPrinter?: boolean;
}
