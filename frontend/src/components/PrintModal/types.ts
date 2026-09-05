import type { AutoQueueItem, CalibrationMode, PrintQueueItem, Printer } from '../../api/client';
import type { AutoCalibrationCaps } from '../../utils/printerCapabilities';

/**
 * Mode of operation for the PrintModal.
 * - 'reprint': Immediate print from archive (no schedule options)
 * - 'add-to-queue': Schedule print to queue (includes schedule options)
 * - 'edit-queue-item': Edit existing queue item (all options + existing values)
 * - 'edit-auto-item': Edit a pending auto-queue item (or its whole batch)
 */
export type PrintModalMode = 'reprint' | 'add-to-queue' | 'edit-queue-item' | 'edit-auto-item';

/**
 * Everything a submitted dialog was answered WITH — the operator's decisions,
 * and nothing derived from the file.
 *
 * ⚠️ This exists so a grouped run can carry one answer onto the files that
 * would have been answered identically. Before it, `QueueSequencer` mounted
 * each silent member with the component's own defaults and hoped: a member
 * started at no printer and `dispatchMode: 'specific'`, so `canSubmit` was
 * false for ever on any farm with more than one printer, and a leader who
 * chose "Queue Only" produced members that dispatched immediately. The spec's
 * carry table described this type; nothing implemented it.
 *
 * ⚠️ **Filament mapping is deliberately absent, and so is anything keyed by
 * plate index.** A global tray id names a different spool on a different
 * machine and plate 3 of one file is not plate 3 of the next, so both are
 * recomputed per file by the same code the visible dialog uses. The rule for
 * adding a field here: it carries if and only if the answer means the same
 * thing about a different file.
 */
export interface PrintModalAnswer {
  /** Which printers the operator ticked. Empty in auto mode. */
  selectedPrinterIds: number[];
  dispatchMode: 'specific' | 'auto';
  /** The auto-queue's own answers. A group is one `sliced_for_model` by
   *  construction, so `target_model` means the same thing for every member —
   *  and `target_location_id` / `force_color_match` are answers no file
   *  implies. */
  autoModeOptions: AutoModeOptionsState;
  scheduleOptions: ScheduleOptions;
  /** The shared Quantity. Per-plate overrides are NOT carried: they are keyed
   *  by this file's plate indexes. */
  quantity: number;
  printOptions: PrintOptions;
  swapMacros: SwapMacrosOptions;
  selectedMacroIds: number[];
}

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
  /** Existing auto-queue item (only for edit-auto-item mode) */
  autoQueueItem?: AutoQueueItem;
  /** >1 = edit every pending copy of the item's batch at once */
  autoQueueBatchCount?: number;
  /** Pre-select specific printers when opening the modal */
  initialSelectedPrinterIds?: number[];
  /** Open with this plate already chosen instead of the file's first plate.
   *
   *  ⚠️ Only for a caller that knows this file's plates. Copying a queue onto
   *  another printer of the same model does — it is the same file, so the plate
   *  the source item was queued with exists there too, and a "copy" that forgot
   *  which plate was queued would not be one. A caller that has not read this
   *  file's plates must NOT set it: plate 3 of one file need not exist in the
   *  next. A grouped run HAS read them (see ``preselectedPlateIds``), which is
   *  why it may.
   *
   *  Ignored in `edit-queue-item` mode, where the item's own plate wins. */
  preselectedPlateId?: number | null;
  /** Open with several plates of this file already chosen.
   *
   *  Used by a grouped run: the group knows which of THIS file's plates belong
   *  to it, so the dialog must show all of them — showing one while the rest
   *  queue silently would make the visible dialog lie about what it is about
   *  to do.
   *
   *  Takes precedence over ``preselectedPlateId`` when both are given. */
  preselectedPlateIds?: number[];
  /** Position of this dialog in a run over GROUPS, with the group's size.
   *  Rendered as a badge; display only, exactly like ``sequence``. */
  groupBadge?: { current: number; total: number; units: number };
  /** Whether this group's remaining members go in on this answer.
   *
   *  Controlled: the modal renders what it is given and reports changes; the
   *  run owns the state, because the answer is a property of the GROUP and must
   *  reset when the next one opens.
   *
   *  ⚠️ Turning it off does NOT discard the answer. The rest of the group still
   *  opens seeded with it — the operator did not change their mind about the
   *  settings, they want to look at each file. That is why this is separate
   *  from ``autoSubmitWhenUnambiguous`` and from ``seededAnswer``. */
  applyToRest?: boolean;
  onApplyToRestChange?: (next: boolean) => void;
  /** Every queue row this dialog's submit created.
   *
   *  A quantity becomes ROWS — there is no quantity column on ``print_queue`` —
   *  and a multi-plate submit fans out over plates and printers besides, so one
   *  answer can produce many. Reported so a copy run can re-form the blocks the
   *  source queue had; nothing else needs it. */
  onQueued?: (createdItemIds: number[]) => void;
  /** Submit without rendering once the dialog is ready and nothing is
   *  ambiguous. The modal still owns the payload — this only removes the
   *  click. Falls back to rendering normally whenever ``canQueueWithoutAsking``
   *  says no, so a run can never queue a plate the operator would have been
   *  asked about — and likewise whenever the submit itself ends in a question
   *  (a low-spool warning) or a failure, so a silent member can never stall a
   *  run with nothing on screen. */
  autoSubmitWhenUnambiguous?: boolean;
  /** Open already answered the way the group's visible dialog was answered.
   *
   *  ⚠️ Read ONLY by the state initialisers, so the operator can still change
   *  every one of these if the member ends up rendering after all. That is why
   *  it is a separate prop and not a reuse of ``initialSelectedPrinterIds``:
   *  that one HIDES the printer selector when it is set without
   *  ``lockPrinterSelection``, which would leave a refused member — precisely
   *  the dialog that most needs the question — with no way to change printer.
   *
   *  ⚠️ It also outranks the stored per-model preference. The preference is
   *  what the operator answered LAST TIME; this is what they answered a moment
   *  ago, about this very run. (And the preference write from the leader is
   *  fire-and-forget, so a member mounting in the same commit reads a cache hit
   *  of the pre-leader values — carrying the answer is the only ordering that
   *  is right in both races.) */
  seededAnswer?: PrintModalAnswer;
  /** Fired on a successful submit with what the operator actually answered, so
   *  a grouped run can seed the rest of the group. Fires from every mode's
   *  success path, alongside the preference write. Display-agnostic: the modal
   *  does not read it back and does not care whether anyone listens. */
  onAnswered?: (answer: PrintModalAnswer) => void;
  /** Fired once when a member asked to submit itself gives up and renders
   *  instead — no filament match, a dead status query, a low-spool warning, a
   *  failed dispatch.
   *
   *  ⚠️ The run cannot see this any other way: a refused member and a silent
   *  one both end in `onSuccess` + `onClose`, so without this callback the
   *  difference between "answered one dialog per group" and "answered one per
   *  group and then some" is invisible, and the run's summary could not say
   *  it. Display-only for the caller — it never changes what the modal does. */
  onAutoSubmitRefused?: () => void;
  /** Position of this dialog in a run over several files ("2 / 5"), rendered as
   *  a badge beside the title. Display only — the modal does not know a run
   *  exists and cannot advance one; QueueSequencer owns that. Omitted for a
   *  single-file open, which must not look like step 1 of anything. */
  sequence?: { current: number; total: number };
  /** Handler for closing the modal */
  onClose: () => void;
  /** Handler for successful operation */
  onSuccess?: () => void;
  /** Project ID to associate the resulting archive with (only when triggered from project view) */
  projectId?: number;
  /** Which LINE of that order the resulting print counts against.
   *
   *  ⚠️ Only meaningful beside ``projectId`` — a line names nothing without
   *  its order, and the server rejects a line from a different one. Absent
   *  means "bound to the order, to no line of it", which is what the order
   *  page shows as its other prints. */
  projectLineId?: number | null;
  /** The caller has already answered the order question — with ``projectId`` /
   *  ``projectLineId``, or with neither, which is the answer "no order". Hides
   *  the Order field either way. A copied queue item passes this: its source
   *  row already carries the answer, and re-asking handed the dialog's own
   *  proposal to every member that submits silently. */
  orderAnswered?: boolean;
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
  /** The run is pinned to the printers in ``initialSelectedPrinterIds`` and the
   *  operator cannot change them.
   *
   *  ⚠️ Pinned is NOT hidden. Passing ``initialSelectedPrinterIds`` alone makes
   *  the selector disappear, which answers the question by omitting it — the
   *  dialog then never says where the print is going. With this flag the
   *  selector still renders, showing that one printer, ticked and not
   *  untickable. Used by drops onto a printer or its queue, where the target
   *  chose the printer but the dialog should still say so. */
  lockPrinterSelection?: boolean;
  /** Pin the auto-queue target to the file's own ``sliced_for_model`` and stop
   *  the operator changing it.
   *
   *  ⚠️ Sets the target EXPLICITLY rather than leaving it empty. Empty means
   *  "let the backend work it out from the 3MF", which usually lands on the
   *  same answer — but the dialog then shows a blank where the constraint is,
   *  and a run of ten dropped files would say nothing about what any of them is
   *  waiting for. A file with no ``sliced_for_model`` keeps the empty
   *  auto-detect value; there is nothing to pin, and inventing one would be
   *  worse than deferring to the parse. */
  lockAutoTarget?: boolean;
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
/** Where a timelapse is recorded. Mirrors the backend `TimelapseStorage`. */
export type TimelapseStorage = 'internal' | 'external';

export interface PrintOptions {
  // Tri-state calibration (off/auto/on). 'auto' is only offered on models whose
  // firmware supports it (see utils/printerCapabilities); saved 'auto' on a
  // non-auto printer displays as 'on'.
  bed_levelling: CalibrationMode;
  flow_cali: CalibrationMode;
  layer_inspect: boolean;
  timelapse: boolean;
  /** Which medium records it, exactly like BambuStudio's folder popup — offered
   *  only when the machine has BOTH an internal store and a healthy card.
   *  `null` means nobody chose, and the printer keeps doing what it did. The
   *  backend re-checks the pick against the card at dispatch, so an external
   *  choice made before somebody pulled the card records internally rather
   *  than failing. */
  timelapse_storage: TimelapseStorage | null;
  mesh_mode_fast_check: boolean;
  /** Inject operator-defined G-code snippets at MACHINE_START_GCODE_END / EOF (#422). */
  gcode_injection: boolean;
  /** Nozzle offset calibration before print — dual-nozzle printers only (#1682).
   *  The MQTT layer forces "skip" on single-nozzle machines regardless. */
  nozzle_offset_cali: CalibrationMode;
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
  bed_levelling: 'on',
  flow_cali: 'on',
  layer_inspect: false,
  timelapse: false,
  timelapse_storage: null,
  mesh_mode_fast_check: true,
  gcode_injection: false,
  nozzle_offset_cali: 'on',
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
  /** Hold this job when the printer's last print failed (m116). */
  requirePreviousSuccess: boolean;
}

/**
 * Default schedule options values.
 */
export const DEFAULT_SCHEDULE_OPTIONS: ScheduleOptions = {
  scheduleType: 'asap',
  scheduledTime: '',
  autoOffAfter: false,
  // Off by default: a gate nobody asked for is a stalled farm.
  requirePreviousSuccess: false,
};

/**
 * Auto-distribute mode options. Used by the AutoModeOptions panel
 * when the operator picks "Auto" instead of a specific printer.
 */
export interface AutoModeOptionsState {
  target_model: string | null;
  target_location_id: number | null;
  force_color_match: boolean;
}

export const DEFAULT_AUTO_MODE_OPTIONS: AutoModeOptionsState = {
  target_model: null,
  target_location_id: null,
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
  /** Render as a statement rather than a question: the printers shown are the
   *  ones this run goes to, ticked, and the tick cannot be removed. */
  locked?: boolean;
  /** Suggested model from sliced file (for pre-selection) */
  slicedForModel?: string | null;
  /** File is swap mode compatible - filter to swap-enabled printers only */
  swapCompatible?: boolean;
  /** Printers whose queue the operator has paused. They stay selectable — the
   *  item is accepted and waits — and only carry a badge saying so. */
  pausedQueuePrinterIds?: number[];
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
  /** How many runs of each plate are wanted, keyed by plate index. A plate
   *  absent from the map takes the dialog's shared Quantity — which is what
   *  every plate took before per-plate counts existed. */
  quantities?: Record<number, number>;
  /** Omitted where per-plate counts make no sense (reprint, edit). The stepper
   *  is only drawn when this is supplied AND more than one plate is selected:
   *  with one plate the shared Quantity field already answers the question, and
   *  two controls for it would be two sources of truth. */
  onQuantityChange?: (plateIndex: number, quantity: number) => void;
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
     *  FilamentMapping against the builtin + cloud user-preset maps. #1718. */
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
  /** Names the plate this panel maps, when one panel is rendered per selected
   *  plate. Each plate prints its own subset of the file's slots and gets its
   *  own AMS mapping, so the panels have to be told apart (upstream #2551). */
  plateLabel?: string;
}

/**
 * Props for the PrintOptions component.
 */
/** A selected printer that cannot record a timelapse, and why. */
export interface TimelapseBlocker {
  name: string;
  /** Key under `printModal.timelapseBlocked.*` — the backend sends a reason
   *  code rather than a sentence, since it does not know the user's language. */
  reason: string;
}

export interface PrintOptionsProps {
  timelapseBlockers?: TimelapseBlocker[];
  selectedPrinterCount?: number;
  /** Selected printers whose timelapse storage is nearly full. Kept apart from
   *  the blockers because this one is FIXABLE from here — the printer can drop
   *  its oldest recording — whereas a missing card cannot. */
  timelapseLowSpace?: { printerId: number; name: string }[];
  /** Whether any selected printer offers both media. False hides the picker
   *  entirely rather than showing one usable option — BambuStudio greys the
   *  dead radio out, which in a farm view is a control that only ever says no. */
  canChooseTimelapseStorage?: boolean;
  onFreeTimelapseSpace?: (printerId: number) => void;
  freeingTimelapseSpace?: boolean;
  options: PrintOptions;
  onChange: (options: PrintOptions) => void;
  defaultExpanded?: boolean;
  /** Show the dual-nozzle-only options (nozzle offset calibration). Default false.
   *  Pass true when at least one selected printer is dual-nozzle. */
  showDualNozzleOptions?: boolean;
  /** Which calibration steps expose the 3-position off/auto/on control for the
   *  effective printer model. Omitted → all off/on (2-position) only. */
  autoCaps?: AutoCalibrationCaps;
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
