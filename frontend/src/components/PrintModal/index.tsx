import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertCircle, AlertTriangle, Calendar, Loader2, Pencil, Printer } from 'lucide-react';
import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import type {
  AutoQueueItemCreate,
  AutoQueueItemUpdate,
  PrintQueueItemCreate,
  PrintQueueItemUpdate,
  SpoolAssignment,
} from '../../api/client';
import { api, macrosApi } from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';
import { Button } from '../Button';
import { ConfirmModal } from '../ConfirmModal';
import { Modal } from '../Modal';
import { useToast } from '../../contexts/ToastContext';
import {
  buildAmsMapping,
  buildFilamentComparison,
  buildLoadedFilaments,
  useFilamentMapping,
} from '../../hooks/useFilamentMapping';
import { useMultiPrinterFilamentMapping, type PerPrinterConfig } from '../../hooks/useMultiPrinterFilamentMapping';
import { useOrderCandidates } from '../../hooks/useOrderCandidates';
import { OrderFilingField, type OrderFilingValue } from '../OrderFilingField';
import { canQueueWithoutAsking } from '../../utils/bulkQueueEligibility';
import { isUnknownOutcome, queueAddOutcomeText, type QueueAddFailure } from '../../utils/queueSource';
import { invalidateOrderCandidates, invalidateOrderViews, invalidateQueueViews } from '../../utils/queryInvalidation';
import { getCurrencySymbol } from '../../utils/currency';
import { toDateTimeLocalValue, parseUTCDate } from '../../utils/date';
import { getBedTypeInfo } from '../../utils/bedType';
import { getGlobalTrayId, isPlaceholderDate } from '../../utils/amsHelpers';
import { splitRoundRobin } from '../../lib/quantitySplit';
import { AutoModeOptions } from './AutoModeOptions';
import { groupTraysForBackup, privateBackupGroup, type BackupGroup } from './filamentBackupGroups';
import { FilamentMapping } from './FilamentMapping';
import { PlateSelector } from './PlateSelector';
import { PrinterSelector } from './PrinterSelector';
import { PrintOptionsPanel } from './PrintOptions';
import { autoCalibrationCaps, isDualNozzleModel } from '../../utils/printerCapabilities';
import { ScheduleOptionsPanel } from './ScheduleOptions';
import { SwapMacrosPanel } from './SwapMacros';
import { EventMacrosPanel } from './EventMacros';
import type {
  FilamentReqsData,
  PrintModalProps,
  PrintOptions,
  QuantityMode,
  ScheduleOptions,
  ScheduleType,
  SwapMacroEvent,
  SwapMacrosOptions,
} from './types';
import type { AutoQueueFilamentOverride } from '../../api/client';
import type { AutoModeOptionsState } from './types';
import {
  DEFAULT_AUTO_MODE_OPTIONS,
  DEFAULT_PRINT_OPTIONS,
  DEFAULT_SCHEDULE_OPTIONS,
  DEFAULT_SWAP_MACROS_OPTIONS,
  SWAP_MACRO_EVENTS,
  readStoredQuantityMode,
  storeQuantityMode,
} from './types';

/**
 * Unified PrintModal component that handles four modes:
 * - 'reprint': Immediate print from archive or library file (supports multi-printer)
 * - 'add-to-queue': Schedule print to queue from archive or library file (supports multi-printer)
 * - 'edit-queue-item': Edit an existing per-printer queue item (supports multi-printer)
 * - 'edit-auto-item': Edit an existing auto-queue row (no printer; the auto-queue routes it later)
 *
 * Archive, library, and managed queue-source inputs share one dialog.  A queue
 * source is add-to-queue only: it preserves saved bytes and cannot enter the
 * auto-queue or immediate-reprint routes, which require an archive/library id.
 */
export function PrintModal({
  mode,
  archiveId,
  libraryFileId,
  sourceQueueItemId,
  archiveName,
  queueItem,
  autoQueueItem,
  autoQueueBatchCount,
  initialSelectedPrinterIds,
  preselectedPlateId,
  preselectedPlateIds,
  sequence,
  groupBadge,
  applyToRest,
  onApplyToRestChange,
  onQueued,
  autoSubmitWhenUnambiguous,
  seededAnswer,
  initialRouting,
  onAnswered,
  onAutoSubmitRefused,
  onClose,
  onSuccess,
  projectId,
  projectLineId,
  orderAnswered,
  cleanupLibraryAfterDispatch,
  initialDispatchMode,
  lockDispatchMode,
  lockPrinterSelection,
  lockAutoTarget,
}: PrintModalProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const { hasPermission } = useAuth();
  const headingId = useId();

  const isSnapshotSource = sourceQueueItemId !== undefined;
  const isLibraryFile = !isSnapshotSource && !!libraryFileId && !archiveId;
  const isArchiveSource = !isSnapshotSource && !!archiveId && !isLibraryFile;

  type FilamentWarningItem = {
    printerName: string;
    /** Always `slotLabels[0]` — kept because the single-tray line reads better. */
    slotLabel: string;
    /**
     * Every tray whose filament was weighed together. One entry for the strict
     * per-tray check, several when AMS Filament Backup pooled a group — the
     * message has to name all of them or "needs 300 g, remaining 200 g" reads
     * as a lie about a slot that is not the one that is short.
     */
    slotLabels: string[];
    requiredGrams: number;
    remainingGrams: number;
  };

  // Multiple printer selection (used for all modes now)
  const [selectedPrinters, setSelectedPrinters] = useState<number[]>(() => {
    // Initialize with the queue item's printer if editing
    if (mode === 'edit-queue-item' && queueItem?.printer_id) {
      return [queueItem.printer_id];
    }
    if (initialSelectedPrinterIds?.length) {
      return initialSelectedPrinterIds;
    }
    // The group's visible dialog already answered "which printer". Below the
    // caller's pin, because a pinned run cannot have been answered differently
    // — the selector is locked — and the pin is the caller's constraint.
    if (seededAnswer?.selectedPrinterIds.length) {
      return seededAnswer.selectedPrinterIds;
    }
    return [];
  });

  // Multi-select plates: in add-to-queue mode users can pick a subset of plates
  const [selectedPlates, setSelectedPlates] = useState<Set<number>>(() => {
    if (mode === 'edit-queue-item' && queueItem?.plate_id != null) {
      return new Set([queueItem.plate_id]);
    }
    // A caller that already knows which plates this is about — copying a queue
    // onto another printer of the same model (one plate), or a grouped run that
    // has already read which of this file's plates belong to the group (several).
    // The "fall back to plate 1" effect below only fires on an empty set, so
    // this survives the plates arriving.
    if (preselectedPlateIds && preselectedPlateIds.length > 0) {
      return new Set(preselectedPlateIds);
    }
    if (preselectedPlateId != null) {
      return new Set([preselectedPlateId]);
    }
    if (mode === 'edit-auto-item' && autoQueueItem?.plate_id != null) {
      return new Set([autoQueueItem.plate_id]);
    }
    return new Set();
  });

  // Derived single-plate value for filament queries and single-select contexts
  const selectedPlate = selectedPlates.size === 1 ? [...selectedPlates][0] : null;

  const [printOptions, setPrintOptions] = useState<PrintOptions>(() => {
    if (mode === 'edit-queue-item' && queueItem) {
      return {
        bed_levelling: queueItem.bed_levelling ?? DEFAULT_PRINT_OPTIONS.bed_levelling,
        flow_cali: queueItem.flow_cali ?? DEFAULT_PRINT_OPTIONS.flow_cali,
        layer_inspect: queueItem.layer_inspect ?? DEFAULT_PRINT_OPTIONS.layer_inspect,
        timelapse: queueItem.timelapse ?? DEFAULT_PRINT_OPTIONS.timelapse,
        // ⚠️ `??` and not `||`: null is the meaningful value here ("nobody
        // chose"), so it must survive re-opening the dialog rather than being
        // replaced by a default the operator never picked.
        timelapse_storage: queueItem.timelapse_storage ?? DEFAULT_PRINT_OPTIONS.timelapse_storage,
        mesh_mode_fast_check: queueItem.mesh_mode_fast_check ?? DEFAULT_PRINT_OPTIONS.mesh_mode_fast_check,
        gcode_injection: queueItem.gcode_injection ?? DEFAULT_PRINT_OPTIONS.gcode_injection,
        nozzle_offset_cali: queueItem.nozzle_offset_cali ?? DEFAULT_PRINT_OPTIONS.nozzle_offset_cali,
        preheat_override: queueItem.preheat_override ?? DEFAULT_PRINT_OPTIONS.preheat_override,
        preheat_chamber_target_override: queueItem.preheat_chamber_target_override ?? DEFAULT_PRINT_OPTIONS.preheat_chamber_target_override,
      };
    }
    if (mode === 'edit-auto-item' && autoQueueItem) {
      // The router row stores only what it copies onto the per-printer item;
      // everything it does not carry keeps the modal default.
      return {
        ...DEFAULT_PRINT_OPTIONS,
        bed_levelling: autoQueueItem.bed_levelling,
        flow_cali: autoQueueItem.flow_cali,
        layer_inspect: autoQueueItem.layer_inspect,
        timelapse: autoQueueItem.timelapse,
        timelapse_storage: autoQueueItem.timelapse_storage ?? DEFAULT_PRINT_OPTIONS.timelapse_storage,
        mesh_mode_fast_check: autoQueueItem.mesh_mode_fast_check,
      };
    }
    if (seededAnswer) return seededAnswer.printOptions;
    return DEFAULT_PRINT_OPTIONS;
  });

  const [swapMacros, setSwapMacros] = useState<SwapMacrosOptions>(() => {
    if ((mode === 'edit-queue-item' && queueItem) || (mode === 'edit-auto-item' && autoQueueItem)) {
      const item = mode === 'edit-queue-item' ? queueItem! : autoQueueItem!;
      const execute = item.execute_swap_macros ?? false;
      const storedEvents = (item.swap_macro_events ?? null) as SwapMacroEvent[] | null;
      return {
        execute,
        events: storedEvents ?? (execute ? [...SWAP_MACRO_EVENTS] : []),
      };
    }
    if (seededAnswer) return seededAnswer.swapMacros;
    return DEFAULT_SWAP_MACROS_OPTIONS;
  });

  // Which macros run for this print. Edit mode starts from what the item
  // stored; every other mode is filled in from the model preference below.
  const [selectedMacroIds, setSelectedMacroIds] = useState<number[]>(() => {
    if (mode === 'edit-queue-item' && queueItem) return queueItem.selected_macro_ids ?? [];
    if (mode === 'edit-auto-item' && autoQueueItem) return autoQueueItem.selected_macro_ids ?? [];
    if (seededAnswer) return seededAnswer.selectedMacroIds;
    return [];
  });

  const [scheduleOptions, setScheduleOptions] = useState<ScheduleOptions>(() => {
    if (mode === 'edit-auto-item' && autoQueueItem) {
      let scheduleType: ScheduleType = 'asap';
      if (autoQueueItem.manual_start) scheduleType = 'manual';
      else if (autoQueueItem.scheduled_time && !isPlaceholderDate(autoQueueItem.scheduled_time)) {
        scheduleType = 'scheduled';
      }
      let scheduledTime = '';
      if (autoQueueItem.scheduled_time && !isPlaceholderDate(autoQueueItem.scheduled_time)) {
        const date = parseUTCDate(autoQueueItem.scheduled_time) ?? new Date();
        scheduledTime = toDateTimeLocalValue(date);
      }
      return {
        scheduleType,
        scheduledTime,
        enqueuePosition: 'end',
        autoOffAfter: autoQueueItem.auto_off_after,
        requirePreviousSuccess: autoQueueItem.require_previous_success ?? false,
      };
    }
    if (mode === 'edit-queue-item' && queueItem) {
      let scheduleType: ScheduleType = 'asap';
      if (queueItem.manual_start) {
        scheduleType = 'manual';
      } else if (queueItem.scheduled_time && !isPlaceholderDate(queueItem.scheduled_time)) {
        scheduleType = 'scheduled';
      }

      let scheduledTime = '';
      if (queueItem.scheduled_time && !isPlaceholderDate(queueItem.scheduled_time)) {
        const date = parseUTCDate(queueItem.scheduled_time) ?? new Date();
        // Use toDateTimeLocalValue to convert UTC to local time for datetime-local input
        scheduledTime = toDateTimeLocalValue(date);
      }

      return {
        scheduleType,
        scheduledTime,
        enqueuePosition: 'end',
        autoOffAfter: queueItem.auto_off_after,
        // ?? false: a response cached from before this field existed would make
        // the checkbox uncontrolled for the rest of the modal's life.
        requirePreviousSuccess: queueItem.require_previous_success ?? false,
      };
    }
    // ⚠️ The one carry whose absence was silently DANGEROUS rather than merely
    // annoying: a leader answering "Queue Only" used to produce members that
    // dispatched the moment the printer went idle.
    if (seededAnswer) return seededAnswer.scheduleOptions;
    return DEFAULT_SCHEDULE_OPTIONS;
  });

  // Manual slot overrides: slot_id (1-indexed) -> globalTrayId (default mapping for single printer or all printers)
  const [manualMappings, setManualMappings] = useState<Record<number, number>>(() => {
    if (mode === 'edit-queue-item' && queueItem?.filament_routing?.mode !== 'auto' && queueItem?.ams_mapping && Array.isArray(queueItem.ams_mapping)) {
      const mappings: Record<number, number> = {};
      queueItem.ams_mapping.forEach((globalTrayId, idx) => {
        if (globalTrayId !== -1) {
          mappings[idx + 1] = globalTrayId;
        }
      });
      return mappings;
    }
    return {};
  });

  // Per-printer override configs (for multi-printer selection)
  const [perPrinterConfigs, setPerPrinterConfigs] = useState<Record<number, PerPrinterConfig>>({});

  // Track initial values for clearing mappings on change (edit mode only)
  const [initialPrinterIds] = useState(() => (mode === 'edit-queue-item' && queueItem?.printer_id ? [queueItem.printer_id] : []));
  const [initialPlateId] = useState(() => (mode === 'edit-queue-item' && queueItem ? queueItem.plate_id : null));

  // Submission state for multi-printer
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [submitProgress, setSubmitProgress] = useState({ current: 0, total: 0 });

  // Quantity (batch). Only exposed for reprint + add-to-queue modes.
  const [quantity, setQuantity] = useState<number>(seededAnswer?.quantity ?? 1);
  // Per-plate overrides of ``quantity``, keyed by plate index. A plate absent
  // here takes the shared value — which is what every plate took before this
  // existed, so the common single-plate flow is untouched. Changing the shared
  // Quantity clears the overrides: it is the bulk setter, and leaving stale
  // per-plate numbers behind it would make the visible field a lie.
  const [plateQuantities, setPlateQuantities] = useState<Record<number, number>>({});
  // Memoised because the quantity memos below take it as a dependency: an
  // identity that changed every render would make them all recompute (and
  // would force the `exhaustive-deps` disables this file no longer carries).
  const quantityForPlate = useCallback(
    (plateIndex: number | null | undefined) =>
      (plateIndex != null ? plateQuantities[plateIndex] : undefined) ?? quantity,
    [plateQuantities, quantity],
  );
  // What the number MEANS on several printers (spec 2026-09-11 §3–4). The
  // group's answer wins, then the browser's memory, then «per printer» — the
  // meaning the field has always had.
  const [quantityMode, setQuantityModeState] = useState<QuantityMode>(
    () => seededAnswer?.quantityMode ?? readStoredQuantityMode(),
  );
  const setQuantityMode = (next: QuantityMode) => {
    setQuantityModeState(next);
    storeQuantityMode(next);
  };

  // Dispatch mode: 'specific' = pick exact printer(s); 'auto' = route via auto-queue.
  // Only meaningful for add-to-queue mode (reprint is always specific, edit-queue-item
  // is already bound to a per-printer queue row).
  // Caller's pin first (a locked toggle cannot have been answered otherwise),
  // then the group's answer, then the default.
  const [dispatchMode, setDispatchMode] = useState<'specific' | 'auto'>(
    initialDispatchMode ?? seededAnswer?.dispatchMode ?? 'specific',
  );
  const [autoModeOptions, setAutoModeOptions] = useState<AutoModeOptionsState>(() => {
    if (mode === 'edit-auto-item' && autoQueueItem) {
      return {
        ...DEFAULT_AUTO_MODE_OPTIONS,
        target_model: autoQueueItem.target_model,
        target_location_id: autoQueueItem.target_location_id,
        force_color_match: autoQueueItem.force_color_match,
        allow_base_material_match: autoQueueItem.allow_base_material_match ?? true,
        feed_policy: autoQueueItem.feed_policy ?? (autoQueueItem.use_ams ? 'auto' : 'external_only'),
      };
    }
    const storedRouting = queueItem?.filament_routing ?? initialRouting;
    if (storedRouting) return {
      ...DEFAULT_AUTO_MODE_OPTIONS,
      feed_policy: storedRouting.feed_policy,
      force_color_match: storedRouting.force_color_match,
      allow_base_material_match: storedRouting.allow_base_material_match ?? true,
    };
    if (seededAnswer) return seededAnswer.autoModeOptions;
    return DEFAULT_AUTO_MODE_OPTIONS;
  });
  // edit-auto-item is auto mode by definition: the row belongs to the router.
  const isAutoMode = !isSnapshotSource && ((mode === 'add-to-queue' && dispatchMode === 'auto') || mode === 'edit-auto-item');

  const [autoOverrides, setAutoOverrides] = useState<AutoQueueFilamentOverride[]>(autoQueueItem?.filament_overrides ?? queueItem?.filament_routing?.filament_overrides ?? initialRouting?.filament_overrides ?? []);
  const routingPreviewInput = {
    archive_id: isArchiveSource ? archiveId : undefined,
    library_file_id: isLibraryFile ? libraryFileId : undefined,
    plate_ids: selectedPlates.size ? [...selectedPlates].sort((a, b) => a - b) : [0],
    target_location_id: autoModeOptions.target_location_id,
    feed_policy: autoModeOptions.feed_policy ?? 'auto',
    force_color_match: autoModeOptions.force_color_match,
    allow_base_material_match: autoModeOptions.allow_base_material_match,
    filament_overrides: autoOverrides,
  };
  const routingPreview = useQuery({
    queryKey: ['auto-queue-routing-preview', routingPreviewInput],
    queryFn: () => api.previewAutoQueueRouting(routingPreviewInput),
    enabled: isAutoMode,
    retry: false,
    refetchInterval: 30_000,
  });
  const routingSourceReady = !!routingPreview.data?.plates.length &&
    routingPreview.data.plates.every(plate => plate.status === 'ok');

  const [filamentWarningItems, setFilamentWarningItems] = useState<FilamentWarningItem[] | null>(null);

  // Track which printers have had the "Expand custom mapping by default" setting applied
  // This ensures the setting only affects initial state, not preventing unchecking
  const [initialExpandApplied, setInitialExpandApplied] = useState<Set<number>>(new Set());

  // Printer counts and effective printer for filament mapping
  const effectivePrinterCount = selectedPrinters.length;
  // For filament mapping, use first selected printer (mapping applies to all)
  const effectivePrinterId = selectedPrinters.length > 0 ? selectedPrinters[0] : null;

  // Queries
  const { data: settings } = useQuery({
    queryKey: ['settings'],
    queryFn: api.getSettings,
  });

  const currencySymbol = getCurrencySymbol(settings?.currency || 'USD');
  const defaultCostPerKg = settings?.default_filament_cost ?? 0;

  const { data: printers, isLoading: loadingPrinters, isFetched: printersFetched } = useQuery({
    queryKey: ['printers'],
    queryFn: api.getPrinters,
  });

  // The ONLY thing that fills an empty printer selection by itself. Named here
  // rather than buried in the effect below because the self-submit has to tell
  // "the pick is still coming" from "nobody is ever going to make it" — and on
  // a farm with two printers nobody is.
  const soleActivePrinterId = useMemo(() => {
    const active = (printers ?? []).filter((p) => p.is_active);
    return active.length === 1 ? active[0].id : null;
  }, [printers]);

  // Per-printer queues — needed to BADGE printers whose queue is paused in the
  // add-to-queue picker. Only fetched / used in add-to-queue mode.
  const { data: queues } = useQuery({
    queryKey: ['queues'],
    queryFn: api.getQueues,
    enabled: mode === 'add-to-queue',
  });

  // ⚠️ These printers are shown and selectable — a paused queue accepts items,
  // it just doesn't dispatch them (backend `queue_add.py` says why). The badge
  // exists so the operator plans the wait rather than discovers it: the print
  // lands in the queue and sits there until the queue is resumed. Dropping them
  // from the picker, as this did until 2026-09-01, made the "Schedule" dialog
  // refuse a printer that "Print now" would happily take.
  const pausedQueuePrinterIds = useMemo(
    () => (mode === 'add-to-queue' ? (queues ?? []).filter((q) => q.is_paused).map((q) => q.printer_id) : []),
    [mode, queues],
  );

  // Per-(user, printer-model) saved PrintModal toggles. Preference is keyed
  // by the model string; selecting any printer of the same model loads the
  // same row. In auto mode the model comes from the AutoMode panel directly.
  // Edit mode skips the whole flow — there the values come from queueItem.
  const effectivePrinterModel = useMemo(() => {
    if (mode === 'edit-queue-item') return null;
    // edit-auto-item resolves through the isAutoMode branch below — the
    // target model still names the macros; the PREFERENCE flow is gated off
    // separately (an edit dialog applies the item's own values, not a saved
    // profile, and must never overwrite that profile on save).

    if (isAutoMode) return autoModeOptions.target_model || null;
    if (selectedPrinters.length === 0) return null;
    const first = printers?.find((p) => p.id === selectedPrinters[0]);
    return first?.model || null;
  }, [mode, isAutoMode, autoModeOptions.target_model, selectedPrinters, printers]);

  // Dual-nozzle gate for the Nozzle Offset Calibration toggle (#1682). Auto mode
  // has no concrete printer, so it mirrors the backend model list against the
  // chosen target model; specific / edit mode uses the canonical MQTT-detected
  // nozzle_count so a printer without a stored model still resolves correctly.
  const showDualNozzleOptions = useMemo(() => {
    if (isAutoMode) {
      return isDualNozzleModel(autoModeOptions.target_model);
    }
    if (!printers || selectedPrinters.length === 0) return false;
    return selectedPrinters.some((id) => printers.find((p) => p.id === id)?.nozzle_count === 2);
  }, [isAutoMode, autoModeOptions.target_model, printers, selectedPrinters]);

  // ⚠️ Which selected printers cannot record a timelapse, asked HERE because
  // here there is somebody who can act on the answer. The same query key the
  // printer selector already uses, so these come out of the cache rather than
  // off the wire.
  //
  // Auto mode is deliberately excluded: no printer is chosen yet, so there is
  // nothing to check and nothing to name.
  const timelapseStatuses = useQueries({
    queries: (isAutoMode ? [] : selectedPrinters).map((id) => ({
      queryKey: ['printerStatus', id],
      queryFn: () => api.getPrinterStatus(id),
      staleTime: 5000,
    })),
  });
  // Selected printers whose timelapse storage is nearly full. Separate from the
  // blockers above because this one is FIXABLE from here — the printer can drop
  // its oldest recording — whereas a missing card cannot.
  const timelapseLowSpace = useMemo(() => {
    if (isAutoMode) return [];
    return selectedPrinters.flatMap((id, i) => {
      const capability = timelapseStatuses[i]?.data?.timelapse_capability;
      if (!capability?.storage_low || !capability.supports_internal) return [];
      const name = printers?.find((pr) => pr.id === id)?.name ?? `#${id}`;
      return [{ printerId: id, name }];
    });
  }, [isAutoMode, selectedPrinters, timelapseStatuses, printers]);

  // Whether the picker is offered at all: BambuStudio shows it per machine, we
  // have a selection. Any printer that can choose is enough — the ones that
  // cannot ignore the field (their resolve returns "no question to answer"),
  // and an external pick lands internally on a machine whose card is missing.
  const canChooseTimelapseStorage = useMemo(() => {
    if (isAutoMode) return false;
    return selectedPrinters.some(
      (_id, i) => timelapseStatuses[i]?.data?.timelapse_capability?.can_choose_storage === true
    );
  }, [isAutoMode, selectedPrinters, timelapseStatuses]);

  const freeTimelapseSpace = useMutation({
    mutationFn: (printerId: number) =>
      api.deleteOldestTimelapse(printerId, timelapseTotalLayers ?? 1),
    onSuccess: (_data, printerId) => {
      // The printer republishes its free space in the next status push, so the
      // fresh number arrives by re-reading rather than in this reply.
      queryClient.invalidateQueries({ queryKey: ['printerStatus', printerId] });
    },
  });

  const timelapseBlockers = useMemo(() => {
    if (isAutoMode) return [];
    return selectedPrinters.flatMap((id, i) => {
      const capability = timelapseStatuses[i]?.data?.timelapse_capability;
      // Absent means we have not heard yet — not a refusal. Naming a printer as
      // broken because its status has not arrived would be worse than silence.
      if (!capability || capability.can_enable !== false) return [];
      const name = printers?.find((p) => p.id === id)?.name ?? `#${id}`;
      return [{ name, reason: capability.reason ?? 'unsupported' }];
    });
  }, [isAutoMode, selectedPrinters, timelapseStatuses, printers]);

  // Which calibration steps expose the 3-position off/auto/on control for the
  // effective model. Non-auto models get the plain off/on toggle.
  const autoCaps = useMemo(() => autoCalibrationCaps(effectivePrinterModel), [effectivePrinterModel]);

  const { data: preferenceData } = useQuery({
    queryKey: ['print-options-preference', effectivePrinterModel],
    queryFn: async () => {
      try {
        return await api.getPrintOptionsPreference(effectivePrinterModel!);
      } catch {
        // 404 — no preference saved yet, fall back to built-in defaults.
        return null;
      }
    },
    enabled: !!effectivePrinterModel && mode !== 'edit-auto-item',
    staleTime: 60 * 1000,
  });

  // Macros that could run on the target printer. Non-swap events only — swap
  // macros have their own panel and their own fields.
  const { data: modelMacros } = useQuery({
    queryKey: ['macros', 'for-model', effectivePrinterModel],
    queryFn: () => macrosApi.getMacrosForModel(effectivePrinterModel!),
    enabled: !!effectivePrinterModel,
    staleTime: 60 * 1000,
  });

  const applicableMacros = useMemo(() => {
    const printer = printers?.find((p) => p.id === selectedPrinters[0]);
    return (modelMacros ?? []).filter(
      (m) =>
        m.enabled &&
        !m.event.startsWith('swap_mode_') &&
        (!m.swap_profile || m.swap_profile === printer?.swap_profile),
    );
  }, [modelMacros, printers, selectedPrinters]);

  // Apply the saved preference once per model so user toggles after the
  // initial apply aren't clobbered by a re-render. The set lives in a ref
  // because we don't want it to participate in render-triggered effect deps.
  const appliedPreferenceModelsRef = useRef<Set<string>>(new Set());
  // The operator's own clicks outrank the stored profile. Without this guard,
  // unticking a toggle BEFORE picking a printer (the model — and with it the
  // preference — only resolves after the pick) let the late-arriving profile
  // silently flip the toggle back, and the submit then saved the flip as the
  // new profile (measured live 2026-08-25: vibration fast-check).
  const touchedOptionsRef = useRef(false);
  useEffect(() => {
    // ⚠️ A seeded member is already answered, and by something newer than any
    // stored profile. Letting the profile land here would not merely be
    // redundant — the leader's own preference write is fire-and-forget and this
    // query is a 60-second cache hit, so what arrives is the values from BEFORE
    // the leader changed anything, and it would overwrite the carry.
    if (seededAnswer) return;
    if (!effectivePrinterModel || !preferenceData) return;
    if (appliedPreferenceModelsRef.current.has(effectivePrinterModel)) return;
    appliedPreferenceModelsRef.current.add(effectivePrinterModel);
    if (touchedOptionsRef.current) return;
    // Merge over DEFAULT so a preference saved before a new option existed
    // (e.g. nozzle_offset_cali, #1682) still gets a defined value.
    setPrintOptions({ ...DEFAULT_PRINT_OPTIONS, ...preferenceData.options.print_options });
    setSwapMacros({
      execute: preferenceData.options.swap_macros.execute,
      events: preferenceData.options.swap_macros.events.filter(
        (e): e is SwapMacroEvent => (SWAP_MACRO_EVENTS as readonly string[]).includes(e),
      ),
    });
  }, [effectivePrinterModel, preferenceData, seededAnswer]);

  // Tick everything the operator has not explicitly turned off for this model.
  // Storing the exceptions rather than the selection is what makes a macro
  // created later arrive ticked instead of silently absent. Edit mode is
  // excluded: there the item's own stored list is the authority.
  const appliedMacroModelsRef = useRef<Set<string>>(new Set());
  useEffect(() => {
    // Same reason as the preference effect above: `selectedMacroIds` is seeded
    // from what the group's dialog was answered with, which is the authority.
    if (seededAnswer) return;
    if (mode === 'edit-queue-item' || mode === 'edit-auto-item') return;
    if (!effectivePrinterModel || applicableMacros.length === 0) return;
    if (appliedMacroModelsRef.current.has(effectivePrinterModel)) return;
    const deselected = new Set(preferenceData?.options.event_macros?.deselected_ids ?? []);
    setSelectedMacroIds(applicableMacros.filter((m) => !deselected.has(m.id)).map((m) => m.id));
    appliedMacroModelsRef.current.add(effectivePrinterModel);
  }, [mode, effectivePrinterModel, applicableMacros, preferenceData, seededAnswer]);

  // Best-effort persist on submit. Failure is silently swallowed — the
  // print itself already succeeded; a failed preference write would only
  // mean defaults next time. Called from each successful submit branch
  // (auto-mode + queue + reprint).
  const persistPreference = useCallback(() => {
    if (!effectivePrinterModel || mode === 'edit-auto-item') return;
    void api
      .upsertPrintOptionsPreference(effectivePrinterModel, {
        print_options: printOptions,
        swap_macros: { execute: swapMacros.execute, events: swapMacros.events },
        event_macros: {
          deselected_ids: applicableMacros.filter((m) => !selectedMacroIds.includes(m.id)).map((m) => m.id),
        },
      })
      .then(() => {
        // Drop the cached read NOW: staleTime is 60s, and a dialog reopened
        // inside that window read the pre-save profile, applied it, and its
        // own submit then saved the STALE values back — the user's change
        // quietly reverted itself (measured live 2026-08-25).
        void queryClient.invalidateQueries({ queryKey: ['print-options-preference', effectivePrinterModel] });
      })
      .catch(() => {
        // silent — preference is best-effort
      });
  }, [effectivePrinterModel, mode, printOptions, swapMacros, applicableMacros, selectedMacroIds, queryClient]);

  const { data: spoolAssignments } = useQuery({
    queryKey: ['spool-assignments'],
    queryFn: () => api.getAssignments(),
    staleTime: 30 * 1000,
    enabled: mode === 'reprint' || mode === 'add-to-queue',
  });

  // Fetch archive details to get sliced_for_model
  const { data: archiveDetails } = useQuery({
    queryKey: ['archive', archiveId],
    queryFn: () => api.getArchive(archiveId!),
    enabled: isArchiveSource,
  });

  // Fetch library file details to get sliced_for_model
  const { data: libraryFileDetails } = useQuery({
    queryKey: ['library-file', libraryFileId],
    queryFn: () => api.getLibraryFile(libraryFileId!),
    enabled: isLibraryFile && !!libraryFileId,
  });

  // A copied queued job is self-contained.  Fetch one profile from its managed
  // bytes instead of touching its archive/library records, which may no longer
  // exist or may now point at a re-sliced revision.
  const { data: queueSourceProfile, isError: queueSourceProfileError } = useQuery({
    queryKey: ['queue-copy-source', sourceQueueItemId],
    queryFn: () => api.getQueueCopySource(sourceQueueItemId!),
    enabled: isSnapshotSource,
    retry: false,
  });

  // Get sliced_for_model from archive or library file
  const slicedForModel = queueSourceProfile?.sliced_for_model || archiveDetails?.sliced_for_model || libraryFileDetails?.sliced_for_model || null;

  // ⚠️ The file's model arrives asynchronously, so the target cannot be seeded
  // from props — it is filled in when the details land. Only when pinned: a
  // normal open leaves the empty "detect from the file" value, which is the
  // long-standing behaviour and lets the backend decide.
  useEffect(() => {
    if (!lockAutoTarget || !slicedForModel) return;
    setAutoModeOptions((prev) =>
      prev.target_model === slicedForModel ? prev : { ...prev, target_model: slicedForModel },
    );
  }, [lockAutoTarget, slicedForModel]);

  // Check swap compatibility
  const swapCompatible = queueSourceProfile?.swap_compatible || archiveDetails?.swap_compatible || libraryFileDetails?.swap_compatible || false;

  // Fetch plates for archives
  const { data: archivePlatesData, isError: archivePlatesError } = useQuery({
    queryKey: ['archive-plates', archiveId],
    queryFn: () => api.getArchivePlates(archiveId!),
    enabled: isArchiveSource,
    retry: false,
  });

  // Fetch plates for library files
  const { data: libraryPlatesData, isError: libraryPlatesError } = useQuery({
    queryKey: ['library-file-plates', libraryFileId],
    queryFn: () => api.getLibraryFilePlates(libraryFileId!),
    enabled: isLibraryFile && !!libraryFileId,
    // Same policy as its `archive-plates` twin above. Things downstream WAIT on
    // this query settling — the Order field does — and three silent backoffs
    // before a permanent failure is admitted is a field that never appears.
    retry: false,
  });

  // Combine plates data from the immutable source profile or the regular file endpoints.
  const platesData = isSnapshotSource ? queueSourceProfile : isLibraryFile ? libraryPlatesData : archivePlatesData;

  // Fetch filament requirements for archives
  const { data: archiveFilamentReqs, isError: archiveFilamentReqsError } = useQuery({
    queryKey: ['archive-filaments', archiveId, selectedPlate],
    queryFn: () => api.getArchiveFilamentRequirements(archiveId!, selectedPlate ?? undefined),
    enabled: isArchiveSource && (selectedPlate !== null || !platesData?.is_multi_plate),
    retry: false,
  });

  // Fetch filament requirements for library files (with plate support)
  const { data: libraryFilamentReqs, isError: libraryFilamentReqsError } = useQuery({
    queryKey: ['library-file-filaments', libraryFileId, selectedPlate],
    queryFn: () => api.getLibraryFileFilamentRequirements(libraryFileId!, selectedPlate ?? undefined),
    enabled: isLibraryFile && !!libraryFileId && (selectedPlate !== null || !platesData?.is_multi_plate),
    // Same policy as its archive twin above and as the per-plate queries below.
    // The self-submit consumes this query's ERROR as a refusal, so a retrying
    // observer keeps a grouped member blank through three backoffs before it
    // shows itself — once per member, so the wait multiplies by the group.
    retry: false,
  });

  const snapshotFilamentReqs = useMemo<FilamentReqsData | undefined>(() => {
    if (!queueSourceProfile) return undefined;
    const plate = queueSourceProfile.plates.find((entry) => entry.index === selectedPlate)
      ?? (queueSourceProfile.is_multi_plate ? undefined : queueSourceProfile.plates[0]);
    return plate ? { filaments: plate.filaments } : { filaments: [] };
  }, [queueSourceProfile, selectedPlate]);

  // Track a settled source failure; mapped UI is shared because neither a
  // deleted archive nor a broken managed source can be safely queued.
  const archiveDataMissing = isSnapshotSource
    ? queueSourceProfileError
    : !isLibraryFile && (archivePlatesError || archiveFilamentReqsError);

  // Combine filament requirements from either source
  const effectiveFilamentReqs = isSnapshotSource
    ? snapshotFilamentReqs
    : isLibraryFile
      ? libraryFilamentReqs
      : archiveFilamentReqs;
  // The picker keeps the profile's display name, while its matcher can use the
  // resolved family material when the operator opts in.  The original profile
  // identity stays present as a preference for display and matching, while the
  // material type keeps a generic PETG spool eligible.
  const applyRoutingPolicy = useCallback((requirements: FilamentReqsData | undefined) => {
    // The source query may temporarily contain an error/fallback payload while
    // it settles. Leave that untouched; the normal source-read gate will keep
    // submission disabled instead of crashing the modal during the transition.
    if (!requirements?.filaments) return requirements;
    return {
      ...requirements,
      filaments: requirements.filaments.map(filament => ({
        ...filament,
        ...(filament.filament_type
          ? autoModeOptions.allow_base_material_match
            ? { type: filament.filament_type }
            : { strict_profile_match: true }
          : {}),
        strict_color_match: autoModeOptions.force_color_match,
      })),
    };
  }, [autoModeOptions.allow_base_material_match, autoModeOptions.force_color_match]);
  const routingFilamentReqs = useMemo(
    () => applyRoutingPolicy(effectiveFilamentReqs),
    [applyRoutingPolicy, effectiveFilamentReqs],
  );
  // Whether that one query gave up. Only the self-submit below reads it: a
  // silent run must be able to tell "this plate needs no filament" from "we do
  // not know yet / we never will", which `effectiveFilamentReqs` alone cannot.
  const effectiveFilamentReqsError = isSnapshotSource
    ? queueSourceProfileError
    : isLibraryFile
      ? libraryFilamentReqsError
      : archiveFilamentReqsError;
  // How many layers the storage question is about. ⚠️ Per PLATE — a container's
  // plates routinely differ by hundreds, so the file has no single answer. With
  // several plates picked, the largest is the honest worst case for "will it
  // fit"; with none picked yet there is nothing to ask about.
  const timelapseTotalLayers = useMemo(() => {
    const plates = platesData?.plates ?? [];
    const chosen = plates.filter((pl) => selectedPlates.has(pl.index));
    const counts = (chosen.length ? chosen : plates)
      .map((pl) => pl.total_layers)
      .filter((n): n is number => typeof n === 'number' && n > 0);
    return counts.length ? Math.max(...counts) : null;
  }, [platesData, selectedPlates]);

  const selectedPlateName = useMemo(() => {
    if (selectedPlate === null || !platesData?.plates?.length) {
      return undefined;
    }
    return platesData.plates.find((plate) => plate.index === selectedPlate)?.name || undefined;
  }, [platesData, selectedPlate]);

  // Only fetch printer status when single printer selected (for filament mapping)
  const { data: printerStatus, isSuccess: printerStatusLoaded, isError: printerStatusFailed } = useQuery({
    queryKey: ['printer-status', effectivePrinterId],
    queryFn: () => api.getPrinterStatus(effectivePrinterId!),
    enabled: !!effectivePrinterId,
  });

  // Get AMS mapping from hook (only when single printer selected)
  const { amsMapping } = useFilamentMapping(routingFilamentReqs, printerStatus, manualMappings);

  // --- Per-plate filament mapping (multi-plate submissions) ---------------
  // Each plate prints its own subset of the file's slots and needs its own AMS
  // mapping. `effectiveFilamentReqs` above is keyed on `selectedPlate`, which is
  // null the moment two plates are ticked, so it holds the UNION of every plate's
  // filaments — and tray assignment is stateful, so matching against that union
  // lets two plates that share a colour on different slots compete for the same
  // tray, sending the loser to a worse tray or to none. That one union mapping
  // then went out with every plate and the scheduler uses a stored mapping
  // verbatim, so a plate could print the wrong colour — decided by a panel the
  // user never saw, because it is hidden for a multi-plate selection.
  // So when several plates are selected we fetch each plate's requirements and
  // map them separately (upstream #2551).
  const selectedPlateIds = useMemo(() => [...selectedPlates].sort((a, b) => a - b), [selectedPlates]);
  const isMultiPlateSelection = selectedPlates.size > 1;

  // The mode only bites with several SPECIFIC printers; auto mode's field is
  // the total already, edit modes never show quantity, and one printer makes
  // the two readings the same number.
  const effectiveQuantityMode: QuantityMode =
    !isAutoMode && selectedPrinters.length > 1 && (mode === 'reprint' || mode === 'add-to-queue')
      ? quantityMode
      : 'perPrinter';
  // The picked printers in the order the selector LISTS them (the order of
  // `api.getPrinters`) — not the order they were ticked. A round-robin tail
  // goes to the first of these, and the plan line names them in this order,
  // so what the operator reads is what the deal does.
  // ⚠️ **Every picked printer is in here, listed or not.** Filtering the
  // query's rows alone drops the whole selection while that query is still
  // in flight — and a total dealt over zero targets gives every printer
  // nothing, skips every request, and still reports success: the operator is
  // told the batch was queued and no row exists. Anything the list does not
  // carry comes last, in tick order; the plan line names it `#id`.
  const orderedTargets = useMemo(() => {
    const listed = (printers ?? []).filter((p) => selectedPrinters.includes(p.id)).map((p) => p.id);
    const unlisted = selectedPrinters.filter((id) => !listed.includes(id));
    return [...listed, ...unlisted];
  }, [printers, selectedPrinters]);
  // The plates this submit covers, in ascending order — the one list the deal,
  // the plan lines, the batch order and the copy counts are all read from.
  // Memoised so the memos below can depend on it by identity.
  const planPlateIds = useMemo(
    () => (selectedPlateIds.length > 0 ? selectedPlateIds : [selectedPlate ?? 0]),
    [selectedPlateIds, selectedPlate],
  );
  // Copies per (plate, printer). Per printer: the plate's own number for every
  // target. Total: the plate's number dealt round-robin over the targets, the
  // cursor carrying from plate to plate (spec §3.1).
  const copiesByPlate = useMemo(() => {
    const totals = planPlateIds.map((i) => quantityForPlate(i));
    const rows =
      effectiveQuantityMode === 'total'
        ? splitRoundRobin(totals, orderedTargets.length)
        : totals.map((n) => orderedTargets.map(() => n));
    return new Map(
      planPlateIds.map((plateIndex, r) => [plateIndex, new Map(orderedTargets.map((id, c) => [id, rows[r][c] ?? 0]))]),
    );
  }, [planPlateIds, quantityForPlate, effectiveQuantityMode, orderedTargets]);
  const copiesFor = (plateIndex: number | null | undefined, printerId: number): number => {
    if (effectiveQuantityMode !== 'total') return quantityForPlate(plateIndex);
    return copiesByPlate.get(plateIndex ?? 0)?.get(printerId) ?? 0;
  };
  // What a (plate, printer) pair is actually dealt. `edit-queue-item` replaces
  // ONE row and always carries a single copy; every other mode follows the
  // deal, which in total mode can be zero. Zero is the same answer everywhere
  // it is asked — no request, no attempt, no progress step, and no spool
  // weighed in the shortage warning (spec §3).
  const dealtCopies = (plateIndex: number | null | undefined, printerId: number): number =>
    mode === 'edit-queue-item' ? 1 : copiesFor(plateIndex, printerId);
  // The copies ONE target gets across the plates in the plan: the per-printer
  // plan line's «{{perPrinter}}» and the number `plannedPrints` multiplies.
  // Written once — the line and the count must not be able to disagree.
  const perTargetCopies = useMemo(
    () => planPlateIds.reduce((sum, i) => sum + quantityForPlate(i), 0),
    [planPlateIds, quantityForPlate],
  );

  // Decision 6: a BATCH is a submission that makes two or more prints — copies,
  // plates, printers — whatever the mode. The count is prints, not rows. In
  // total mode the field IS the count; per printer it multiplies by the targets.
  const plannedPrints = useMemo(() => {
    if (effectiveQuantityMode === 'total') return perTargetCopies;
    const targets = isAutoMode ? 1 : Math.max(1, selectedPrinters.length);
    return perTargetCopies * targets;
  }, [perTargetCopies, isAutoMode, selectedPrinters, effectiveQuantityMode]);
  const isBatch = plannedPrints >= 2;

  const perPlateReqQueries = useQueries({
    queries: (isMultiPlateSelection ? selectedPlateIds : []).map((plateId) => ({
      queryKey: isSnapshotSource
        ? ['queue-copy-source-plate', sourceQueueItemId, plateId]
        : isLibraryFile
        ? ['library-file-filaments', libraryFileId, plateId]
        : ['archive-filaments', archiveId, plateId],
      queryFn: () =>
        isSnapshotSource
          ? Promise.resolve({
              filaments: queueSourceProfile?.plates.find((plate) => plate.index === plateId)?.filaments ?? [],
            })
          : isLibraryFile
          ? api.getLibraryFileFilamentRequirements(libraryFileId!, plateId)
          : api.getArchiveFilamentRequirements(archiveId!, plateId),
      enabled: isSnapshotSource ? !!queueSourceProfile : isLibraryFile ? !!libraryFileId : !!archiveId,
      // Same policy as the single-plate query above: these keys are shared, and a
      // retrying observer would leave the plate looking merely slow for seconds.
      retry: false,
    })),
  });

  // A plate that has not answered yet and a plate whose 3MF cannot be read look
  // identical from here - both are simply absent from `perPlateReqs`. Neither may
  // be treated as "this plate needs no filament": that would queue it with no
  // mapping at all and it would print in whatever happens to be loaded. Both
  // states gate submission instead (see `canSubmit`).
  // `isPending` is "no data yet", not "a request is in flight" - a background
  // refetch of a plate we already have must not disable the button under the user.
  const perPlateReqsPending = perPlateReqQueries.some((q) => q.isPending);
  const perPlateReqsFailed = perPlateReqQueries.some((q) => q.isError);

  const perPlateReqs = useMemo(() => {
    const byPlate = new Map<number, FilamentReqsData>();
    selectedPlateIds.forEach((plateId, i) => {
      const data = perPlateReqQueries[i]?.data;
      const routed = applyRoutingPolicy(data);
      if (routed) byPlate.set(plateId, routed);
    });
    return byPlate;
    // Keyed on each query's last update stamp, not on the query objects (fresh every
    // render) and not on a spread of their data (a dep array whose *length* changes
    // with the plate count, which React treats as always-changed and warns about).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [applyRoutingPolicy, selectedPlateIds, perPlateReqQueries.map((q) => q.dataUpdatedAt).join('|')]);

  // Manual slot overrides are per plate: slot 3 of plate 1 and slot 3 of plate 2
  // are different prints and may want different trays.
  const [manualMappingsByPlate, setManualMappingsByPlate] = useState<Record<number, Record<number, number>>>({});

  // Only ever computed for a single target printer: a global tray id names a
  // different spool on a different machine, so a fan-out must not reuse these.
  const perPlateAmsMappings = useMemo(() => {
    const byPlate = new Map<number, number[] | undefined>();
    if (!isMultiPlateSelection || !effectivePrinterId || selectedPrinters.length !== 1) return byPlate;

    const loaded = buildLoadedFilaments(printerStatus);
    const ftsActive = printerStatus?.fila_switch?.installed === true;

    for (const plateId of selectedPlateIds) {
      const reqs = perPlateReqs.get(plateId);
      if (!reqs) continue;
      byPlate.set(
        plateId,
        buildAmsMapping(
          buildFilamentComparison(
            reqs,
            loaded,
            manualMappingsByPlate[plateId] ?? {},
            ftsActive,
            printerStatus?.tray_now,
            settings?.prefer_lowest_filament ?? true,
          ),
        ),
      );
    }
    return byPlate;
  }, [
    isMultiPlateSelection,
    effectivePrinterId,
    printerStatus,
    selectedPlateIds,
    perPlateReqs,
    manualMappingsByPlate,
    selectedPrinters.length,
    settings?.prefer_lowest_filament,
  ]);

  // Multi-printer filament mapping (for per-printer configuration)
  const multiPrinterMapping = useMultiPrinterFilamentMapping(
    selectedPrinters,
    printers,
    routingFilamentReqs,
    manualMappings,
    perPrinterConfigs,
    setPerPrinterConfigs
  );

  // Auto-select first plate when plates load (single or multi-plate)
  useEffect(() => {
    if (platesData?.plates && platesData.plates.length >= 1 && selectedPlates.size === 0) {
      setSelectedPlates(new Set([platesData.plates[0].index]));
    }
  }, [platesData, selectedPlates.size]);

  // --- Which ORDER this print is filed under -------------------------------
  // ⚠️ The dialog asks only when nobody has already answered. A caller that
  // named an order (the plan block names its line too) has made the decision
  // already, and re-asking here would let this dialog move the print to a
  // DIFFERENT order than the one the caller opened it for. An archive carries
  // the original print's own binding, and `reprintArchive` deliberately sends
  // neither id — so the question would have nowhere to go.
  const [chosenOrderFiling, setChosenOrderFiling] = useState<OrderFilingValue>(() =>
    seededAnswer?.orderFilingKind ? { kind: seededAnswer.orderFilingKind } : { kind: 'none' },
  );
  // Once the operator has answered, the answer stands. Without this, switching
  // plates — which legitimately re-asks the server — would quietly overwrite a
  // deliberate "Without an order" with whatever the next plate needs.
  const [orderFilingTouched, setOrderFilingTouched] = useState(() => seededAnswer?.orderFilingKind !== undefined);
  // ⚠️ `projects:read` is part of ASKING, not just of answering. The candidates
  // endpoint requires it beside the library read (it names orders and how much
  // of them is left), so without it the request is a guaranteed 403 — a round
  // trip whose only visible effect is a submit button disabled while it happens
  // and a field that never appears. A viewer who may print but not read orders
  // gets exactly the dialog they had before this feature existed.
  // ⚠️ `orderAnswered` covers the answer "no order" too — a copied queue item
  // whose source was never filed must not be re-asked and handed a proposal.
  const asksAboutOrder =
    isLibraryFile &&
    !orderAnswered &&
    projectId == null &&
    projectLineId == null &&
    hasPermission('projects:read') &&
    (mode === 'reprint' || mode === 'add-to-queue');
  // The plate the dialog asks about: the FIRST ticked one, else the plate the
  // auto-select effect is about to tick (the same rule that effect uses), else
  // 0 — the whole file, which is what a file with no plates at all is.
  // ⚠️ **Not `selectedPlate ?? 0`.** That reads a multi-plate selection as "the
  // whole file", and a product whose plates are registered per plate index has
  // no whole-file plate: the list comes back empty, the field hides, and every
  // row of that selection files under nothing. Asking about one of the ticked
  // plates still proposes the order; the LINE is then left to the backend to
  // resolve per row (see `filedProjectLineId`).
  // ⚠️ And the question waits for the plates to arrive at all, or the first
  // render asks about plate 0 for a file that has plates — a list replaced a
  // moment later, and for a per-plate product a field that appears and vanishes.
  // ⚠️ **SETTLED, not "arrived".** A plates fetch that fails permanently never
  // produces data, and waiting on data alone left the Order field missing for
  // ever on a file whose plate list 500s — silently, since nothing else on the
  // dialog depends on that list. A settled failure is an answer: ask about plate
  // 0, the whole file, which is exactly what a file with no plates is.
  const orderPlateIndex = selectedPlateIds[0] ?? platesData?.plates?.[0]?.index ?? 0;
  // ⚠️ **A ticked plate is already an answer about which plate this is.** The
  // silent members of a grouped run are mounted with `preselectedPlateIds`, so
  // `selectedPlateIds[0]` is right from the FIRST render — waiting on the plates
  // list there would hold the question open for a fetch whose answer this
  // dialog is not going to use, and the member submits the moment its filament
  // requirements land, which can be sooner.
  const candidatesEnabled =
    asksAboutOrder && (selectedPlateIds.length > 0 || platesData !== undefined || libraryPlatesError);
  const {
    data: orderCandidates,
    isLoading: orderCandidatesLoading,
    isPlaceholderData: orderCandidatesPlaceholder,
  } = useOrderCandidates(libraryFileId, orderPlateIndex, candidatesEnabled);

  // Decision 6: offered only for a batch, and only to an operator who can
  // actually create the order this would file itself under.
  const offerNewOrder = asksAboutOrder && isBatch && hasPermission('projects:create');

  // ⚠️ **The proposal is DERIVED, never synced into state by an effect.** The
  // silent members of a grouped run submit from an effect of their own, and
  // effects in one commit run in declaration order: a `setState` here would
  // still be unrendered when that one fires, so the member would send the
  // filing from BEFORE the candidates arrived — the leader beside it filing
  // correctly and nothing on screen to say the rest did not.
  // The first candidate that still NEEDS prints. The list already sorts those
  // first, but the rule is the dialog's own: an order that is already covered
  // is never proposed by itself, only chosen.
  //
  // ⚠️ **A needy candidate always wins over «New order».** Decision 6 proposes
  // a new order only when NOTHING open already wants the plate — printing into
  // an order that is already short of it is the better default, batch or not.
  const proposedOrderFiling = useMemo<OrderFilingValue>(() => {
    const needy = orderCandidates?.find((c) => c.outstanding_prints > 0);
    if (needy) return { kind: 'order', projectId: needy.project_id, projectLineId: needy.project_line_id };
    if (offerNewOrder && settings?.auto_order_for_batches) return { kind: 'new' };
    return { kind: 'none' };
  }, [orderCandidates, offerNewOrder, settings?.auto_order_for_batches]);

  // ⚠️ **A choice the current plate does not offer is not an answer about this
  // plate.** Switching plates re-asks, and the new list need not contain the
  // order the operator picked for the old one — the `<select>` then falls back
  // to showing «Without an order» while the payload would still carry the old
  // order and line, and when the new plate has no candidates at all the field
  // is not even on screen to be doubted. So a stale choice is dropped and the
  // untouched rule takes over. A deliberate «Without an order» (touched,
  // `{ kind: 'none' }`) is not stale — it is an answer about every plate, and
  // it survives. So does «New order for this batch» — it names no candidate to
  // go stale against.
  const orderFiling = useMemo<OrderFilingValue>(() => {
    if (!orderFilingTouched) return proposedOrderFiling;
    // A silent sequencer member inherits the leader's seeded answer, including
    // «New order for this batch» — but the offer itself only exists for a
    // batch (Decision 6). A single-plate member must never mint a one-print
    // order just because it carried forward a choice made for the whole run.
    if (chosenOrderFiling.kind === 'new' && !offerNewOrder) return proposedOrderFiling;
    if (chosenOrderFiling.kind !== 'order') return chosenOrderFiling;
    const stillOffered = orderCandidates?.some(
      (c) => c.project_id === chosenOrderFiling.projectId && c.project_line_id === chosenOrderFiling.projectLineId,
    );
    return stillOffered ? chosenOrderFiling : proposedOrderFiling;
  }, [orderFilingTouched, chosenOrderFiling, orderCandidates, proposedOrderFiling, offerNewOrder]);

  // While the answer is in flight there is nothing to file yet, and nothing to
  // show either.
  //
  // ⚠️ **"Not yet" includes the window BEFORE the query is even enabled.**
  // `isLoading` is false for a DISABLED query, so a dialog that asks about an
  // order while it waits for the plates list reported "nothing pending" for the
  // whole of that wait — and a silent grouped member, which submits as soon as
  // nothing is pending, sent its payload with no order on it. It looked right
  // in every interactive test, because a person cannot click faster than a
  // plates fetch. The gate is the same expression the hook's `enabled` is, so
  // the two cannot drift apart.
  //
  // Bounded, still: the candidates query does not retry, neither does the
  // plates query it waits on, and `isLoading` is false for a settled failure as
  // well as for a finished fetch — so every path out of `candidatesEnabled`
  // being false ends in it becoming true or in the dialog having nothing to ask.
  //
  // ⚠️ **A plate SWITCH is also "not yet", even though `isLoading` alone says
  // otherwise.** The hook's `placeholderData` keeps the previous plate's list
  // on screen while the new plate's request is in flight, keyed on the same
  // file — and TanStack then reports `status: 'success'` / `isPending: false`,
  // so `isLoading` is false throughout. Without `orderCandidatesPlaceholder`
  // here, submitting in that window would file the print under the OLD
  // plate's proposal while `orderCandidates` still holds it. The field's
  // `loading` prop stays `orderCandidatesLoading` on its own — the picker
  // keeps showing the previous list without flicker; only the submit waits.
  const orderAnswerPending =
    asksAboutOrder && (!candidatesEnabled || orderCandidatesLoading || orderCandidatesPlaceholder);

  // What the payloads carry. When the dialog asked, its answer is the whole
  // truth — including "no order", which must not fall back to a prop.
  // ⚠️ «New order for this batch» carries no id yet: `ensureBatchOrder` (in the
  // submit handler) creates it and the writes below carry ITS id, never this one.
  const filedProjectId = asksAboutOrder ? (orderFiling.kind === 'order' ? orderFiling.projectId : undefined) : projectId;
  // The line as answered: by the dialog's own field, or by the caller.
  const answeredProjectLineId = asksAboutOrder
    ? (orderFiling.kind === 'order' ? orderFiling.projectLineId : null)
    : (projectLineId ?? null);
  // ⚠️ **Several plates ticked: the ORDER travels, the LINE does not.** The
  // answer is about ONE plate, and the rows this submit creates are one per
  // plate — a line right for plate 1 is simply wrong on plate 3's row, and can
  // belong to another product. The backend writers resolve the line per row
  // (`auto_queue_add` inside its plate fan-out, `queue_add` and the batch writer
  // per request), which is the only place that knows which plate each row is
  // for, and they refuse to guess where two lines are alike.
  //
  // ⚠️ **And it applies to a line the CALLER named too.** The plan block opens
  // this dialog pinned to its own line; tick a second plate of that file there
  // and every row went out stamped with the line the block was standing on,
  // including the plates that make another product's parts. The block's line is
  // an answer about the plate it offered, not about the file.
  //
  // Only ever dropped when the ORDER survives it: a caller that named a line and
  // no order would otherwise have its filing thrown away entirely, and one row
  // on a slightly wrong line beats every row on nothing.
  const filedProjectLineId =
    isMultiPlateSelection && filedProjectId != null ? null : answeredProjectLineId;

  /**
   * Hand the caller what this dialog was answered with, on a successful submit.
   *
   * ⚠️ Fired beside `persistPreference` and for the same reason — this is the
   * moment the answer is known to be an answer — but it is the opposite kind of
   * carry, and both are needed. The preference is per (user, printer MODEL) and
   * survives the dialog; this is per RUN and dies with it. Only the run-scoped
   * half can carry a printer, a schedule or a quantity, none of which belong to
   * a model, and only it is immune to the preference write being
   * fire-and-forget behind a 60-second cache.
   *
   * ⚠️ **Below the order-filing block, not beside `persistPreference` where it
   * used to live** — it now reads `orderFiling`, and hoisting it back above
   * that `useMemo` would read the binding before its own initializer runs.
   */
  const reportAnswer = useCallback(() => {
    onAnswered?.({
      selectedPrinterIds: [...selectedPrinters],
      dispatchMode,
      autoModeOptions,
      requiresFileReview: autoOverrides.length > 0 || Object.keys(manualMappings).length > 0 ||
        Object.values(manualMappingsByPlate).some(mapping => Object.keys(mapping).length > 0) ||
        Object.values(perPrinterConfigs).some(config => !config.useDefault && !config.autoConfigured),
      scheduleOptions,
      quantity,
      quantityMode,
      printOptions,
      swapMacros,
      selectedMacroIds: [...selectedMacroIds],
      // A specific order never travels to a silent member — only the KIND does.
      orderFilingKind: orderFiling.kind === 'order' ? undefined : orderFiling.kind,
    });
  }, [
    onAnswered,
    autoOverrides,
    manualMappings,
    manualMappingsByPlate,
    perPrinterConfigs,
    selectedPrinters,
    dispatchMode,
    autoModeOptions,
    scheduleOptions,
    quantity,
    quantityMode,
    printOptions,
    swapMacros,
    selectedMacroIds,
    orderFiling,
  ]);

  // Auto-select first printer when only one available
  useEffect(() => {
    // Skip auto-select for edit mode (already initialized from queueItem)
    if (mode === 'edit-queue-item') return;
    if (soleActivePrinterId !== null && selectedPrinters.length === 0) {
      setSelectedPrinters([soleActivePrinterId]);
    }
  }, [mode, soleActivePrinterId, selectedPrinters.length]);

  // Clear manual mappings and per-printer configs when printer or plate changes
  useEffect(() => {
    if (mode === 'edit-queue-item') {
      // For edit mode, clear mappings if printer selection or plate changed from initial
      // Sort COPIES: `selectedPrinters`' own order is load-bearing now (it is
      // the submit loop's walk order and the tick-order tail of
      // `orderedTargets`), and `Array.prototype.sort` reorders in place.
      const printersChanged =
        JSON.stringify([...selectedPrinters].sort()) !== JSON.stringify([...initialPrinterIds].sort());
      if (printersChanged || selectedPlate !== initialPlateId) {
        setManualMappings({});
        setManualMappingsByPlate({});
        setPerPrinterConfigs({});
        setInitialExpandApplied(new Set());
      }
    } else {
      setManualMappings({});
      setPerPrinterConfigs({});
      setInitialExpandApplied(new Set());
    }
  }, [mode, selectedPrinters, selectedPlate, initialPrinterIds, initialPlateId]);

  // Auto-expand per-printer mapping when setting is enabled and multiple printers selected
  // Only applies once per printer on initial selection, not when user unchecks
  useEffect(() => {
    if (!settings?.per_printer_mapping_expanded) return;
    if (selectedPrinters.length <= 1) return;

    // Only auto-configure printers that:
    // 1. Haven't had initial expand applied yet
    // 2. Have their status loaded (so auto-configure will actually work)
    const printersReadyForExpand = selectedPrinters.filter(printerId => {
      if (initialExpandApplied.has(printerId)) return false;

      // Check if this printer has status loaded
      const result = multiPrinterMapping.printerResults.find(r => r.printerId === printerId);
      return result && result.status && !result.isLoading;
    });

    if (printersReadyForExpand.length > 0) {
      // Mark these printers as having been initially expanded
      setInitialExpandApplied(prev => {
        const next = new Set(prev);
        printersReadyForExpand.forEach(id => next.add(id));
        return next;
      });

      // Auto-configure printers
      printersReadyForExpand.forEach(printerId => {
        multiPrinterMapping.autoConfigurePrinter(printerId);
      });
    }
  }, [settings?.per_printer_mapping_expanded, selectedPrinters, initialExpandApplied, multiPrinterMapping]);

  const isMultiPlate = platesData?.is_multi_plate ?? false;
  // Memoised for the same reason as `quantityForPlate`: the plan lines list it
  // as a dependency, and a fresh `[]` every render would recompute them every
  // render (and the `??` would earn an exhaustive-deps warning of its own).
  const plates = useMemo(() => platesData?.plates ?? [], [platesData]);

  // What will actually be printed, in the operator's words — one line, or one
  // per plate in total mode so the plate's number is visibly a total. It sits
  // here rather than beside the split above because it reads `plates`.
  const planLines = useMemo((): string[] => {
    if (isAutoMode || selectedPrinters.length < 2) return [];
    const printerName = (id: number) => printers?.find((p) => p.id === id)?.name ?? `#${id}`;
    if (effectiveQuantityMode !== 'total') {
      return [
        t('printModal.quantityPlan.perPrinter', {
          perPrinter: perTargetCopies,
          count: selectedPrinters.length,
          total: plannedPrints,
        }),
      ];
    }
    const split = (plateIndex: number) =>
      orderedTargets.map((id) => `${printerName(id)}: ${copiesByPlate.get(plateIndex)?.get(id) ?? 0}`).join(' · ');
    if (planPlateIds.length === 1) {
      const p = planPlateIds[0];
      return [t('printModal.quantityPlan.total', { total: quantityForPlate(p), split: split(p) })];
    }
    return planPlateIds.map((p) =>
      t('printModal.quantityPlan.totalPlate', {
        // An unnamed plate is «Plate 1» / «Платформа 1» — the same fallback the
        // plate picker right above this line uses, so the two never disagree in
        // the same dialog.
        plate: plates.find((x) => x.index === p)?.name || t('printModal.plateNFallback', { index: p }),
        total: quantityForPlate(p),
        split: split(p),
      }),
    );
  }, [
    isAutoMode,
    selectedPrinters,
    effectiveQuantityMode,
    copiesByPlate,
    orderedTargets,
    planPlateIds,
    quantityForPlate,
    perTargetCopies,
    plannedPrints,
    printers,
    plates,
    t,
  ]);

  const spoolAssignmentsByPrinter = useMemo(() => {
    const map = new Map<number, Map<number, SpoolAssignment>>();
    if (!spoolAssignments) return map;
    spoolAssignments.forEach((assignment) => {
      const isExternal = assignment.ams_id === 255;
      const globalTrayId = getGlobalTrayId(
        assignment.ams_id,
        assignment.tray_id,
        isExternal
      );
      const printerMap = map.get(assignment.printer_id) ?? new Map();
      printerMap.set(globalTrayId, assignment);
      map.set(assignment.printer_id, printerMap);
    });
    return map;
  }, [spoolAssignments]);

  const filamentWarningMessage = useMemo(() => {
    if (!filamentWarningItems || filamentWarningItems.length === 0) return '';
    const lines = filamentWarningItems.map((item) =>
      item.slotLabels.length > 1
        ? t('printModal.insufficientFilamentGroupLine', {
            printer: item.printerName,
            slots: item.slotLabels.join(', '),
            required: Math.round(item.requiredGrams),
            remaining: Math.round(item.remainingGrams),
          })
        : t('printModal.insufficientFilamentLine', {
            printer: item.printerName,
            slot: item.slotLabel,
            required: Math.round(item.requiredGrams),
            remaining: Math.round(item.remainingGrams),
          })
    );
    return [t('printModal.insufficientFilamentMessage'), ...lines].join('\n');
  }, [filamentWarningItems, t]);

  // Add to queue mutation (single printer)
  const addToQueueMutation = useMutation({
    mutationFn: (data: PrintQueueItemCreate) => api.addToQueue(data),
  });
  const addNextQueueBlockMutation = useMutation({
    mutationFn: (items: PrintQueueItemCreate[]) => api.addNextQueueBlock(items),
  });

  // Update queue item mutation
  const updateQueueMutation = useMutation({
    mutationFn: (data: PrintQueueItemUpdate) => api.updateQueueItem(queueItem!.id, data),
    onSuccess: () => {
      invalidateQueueViews(queryClient);
      showToast(t('printModal.queueItemUpdated'));
      onSuccess?.();
      onClose();
    },
    onError: (error: Error) => {
      showToast(error.message || t('printModal.failedToUpdateQueue'), 'error');
    },
  });

  // Get mapping for a specific printer (per-printer override or default).
  // A multi-plate submission maps each plate on its own — `amsMapping` and the
  // per-printer mappings are both derived from the union of every selected
  // plate's filaments, which is not this plate's print (upstream #2551).
  // Without a per-plate mapping we send NONE at all and let the scheduler
  // compute one at dispatch (which it already does per plate); a union mapping
  // would be used verbatim and could feed a slot from the wrong tray.
  const getMappingForPrinter = (printerId: number, plateId: number | null): number[] | undefined => {
    if (isMultiPlateSelection) {
      // Fanning several plates across several printers would be a mapping per
      // plate *per printer*; those items go out without one and the scheduler
      // maps each plate against the printer it actually picks.
      if (plateId === null || selectedPrinters.length !== 1) return undefined;
      return perPlateAmsMappings.get(plateId);
    }
    // For multi-printer selection, check if this printer has an override
    if (selectedPrinters.length > 1) {
      const printerConfig = perPrinterConfigs[printerId];
      if (printerConfig && !printerConfig.useDefault) {
        return multiPrinterMapping.getFinalMapping(printerId);
      }
    }
    return amsMapping;
  };

  const runSubmit = async (e?: React.FormEvent, options?: { skipFilamentCheck?: boolean }) => {
    e?.preventDefault();

    // ⚠️ A dialog that never showed itself does not announce itself either.
    // A group of 57 plates submits 56 times without rendering, and each of
    // those toasts would stack in the same corner with no cap and no
    // de-duplication — 57 identical "Print queued" cards over a farm view.
    // The one visible dialog of each group still confirms, and QueueSequencer
    // reports the run once at the end. Errors are NOT suppressed: a silent
    // member that fails stops being silent (see the self-submit effect below),
    // so its message arrives with a dialog to explain it.
    //
    // ⚠️ ONE expression for every success toast in this function, because it
    // is the exact negation of the render guard near the bottom of the
    // component: a member announces itself if and only if it rendered. That
    // equivalence is the whole safety argument, and a second, differently
    // worded condition per branch would break it quietly — which is how the
    // auto-queue tier kept toasting per silent member after the specific-printer
    // tier stopped.
    const announces = !autoSubmitWhenUnambiguous || autoSubmitRefused;

    // Decision 6: «New order for this batch» creates the order FIRST, then the
    // usual writes carry its id — and no line: the server files each plate
    // under its own plate product's line (Decision 7).
    let submitProjectId = filedProjectId;
    let submitProjectLineId = filedProjectLineId;
    const ensureBatchOrder = async () => {
      if (!(asksAboutOrder && orderFiling.kind === 'new' && libraryFileId != null)) return;
      const targets = isAutoMode ? 1 : Math.max(1, selectedPrinters.length);
      const created = await api.createOrderFromFiles({
        kind: 'plates',
        library_file_id: libraryFileId,
        plates: planPlateIds.map((i) => ({
          plate_index: i,
          copies: effectiveQuantityMode === 'total' ? quantityForPlate(i) : quantityForPlate(i) * targets,
        })),
      });
      submitProjectId = created.id;
      submitProjectLineId = null;
      invalidateOrderViews(queryClient);
    };

    // Edit of a pending auto-queue row (or its whole batch): one PUT, no
    // dispatch. Position is deliberately not sent — the reorder flow owns it.
    if (mode === 'edit-auto-item' && autoQueueItem) {
      setIsSubmitting(true);
      try {
        const payload: AutoQueueItemUpdate = {
          target_model: autoModeOptions.target_model ?? null,
          target_location_id: autoModeOptions.target_location_id ?? null,
          force_color_match: autoModeOptions.force_color_match,
          allow_base_material_match: autoModeOptions.allow_base_material_match,
          feed_policy: autoModeOptions.feed_policy ?? 'auto',
          filament_overrides: autoOverrides,
          scheduled_time:
            scheduleOptions.scheduleType === 'scheduled' && scheduleOptions.scheduledTime
              ? new Date(scheduleOptions.scheduledTime).toISOString()
              : null,
          manual_start: scheduleOptions.scheduleType === 'manual',
          auto_off_after: scheduleOptions.autoOffAfter,
          require_previous_success: scheduleOptions.requirePreviousSuccess,
        };
        if ((autoQueueBatchCount ?? 1) > 1 && autoQueueItem.batch_id) {
          await api.updateAutoQueueBatch(autoQueueItem.batch_id, payload);
        } else {
          await api.updateAutoQueueItem(autoQueueItem.id, payload);
        }
        showToast(t('autoQueue.itemUpdated'));
        queryClient.invalidateQueries({ queryKey: ['auto-queue'] });
        onSuccess?.();
        onClose();
      } catch (err) {
        showToast(t('printModal.failedPrefix', { error: (err as Error).message }), 'error');
      } finally {
        setIsSubmitting(false);
      }
      return;
    }

    // Auto-distribute path: bypass per-printer mapping entirely.
    // The scheduler picks a printer + computes AMS mapping at dispatch.
    if (isAutoMode) {
      setIsSubmitting(true);
      try {
        await ensureBatchOrder();
        const platesToQueue =
          selectedPlates.size > 0 ? [...selectedPlates] : selectedPlate !== null ? [selectedPlate] : [];
        const payload: AutoQueueItemCreate = {
          archive_id: isArchiveSource ? archiveId : undefined,
          library_file_id: isLibraryFile ? libraryFileId : undefined,
          project_id: submitProjectId,
          project_line_id: submitProjectLineId,
          target_model: autoModeOptions.target_model ?? undefined,
          target_location_id: autoModeOptions.target_location_id ?? undefined,
          force_color_match: autoModeOptions.force_color_match,
          allow_base_material_match: autoModeOptions.allow_base_material_match,
          feed_policy: autoModeOptions.feed_policy ?? 'auto',
          filament_overrides: autoOverrides,
          plate_ids: platesToQueue.length > 1 ? platesToQueue : undefined,
          plate_id: platesToQueue.length === 1 ? platesToQueue[0] : null,
          scheduled_time:
            scheduleOptions.scheduleType === 'scheduled' && scheduleOptions.scheduledTime
              ? new Date(scheduleOptions.scheduledTime).toISOString()
              : undefined,
          manual_start: scheduleOptions.scheduleType === 'manual',
          auto_off_after: scheduleOptions.autoOffAfter,
          require_previous_success: scheduleOptions.requirePreviousSuccess,
          quantity,
          // Auto-queue takes every plate in ONE request, so per-plate counts
          // have to travel as a map. (The per-printer tier gets one request per
          // plate and simply carries a different ``quantity`` in each.) Sent
          // only when something was actually overridden, so an unchanged
          // dialog still produces the payload it always did.
          plate_quantities: Object.keys(plateQuantities).length > 0
            ? Object.fromEntries(platesToQueue.map((index) => [index, quantityForPlate(index)]))
            : undefined,
        };
        await api.addToAutoQueue(payload);
        reportAnswer();
        const queuedCount = platesToQueue.length > 0
          ? platesToQueue.reduce((sum, index) => sum + quantityForPlate(index), 0)
          : quantity;
        if (announces) {
          showToast(queuedCount > 1 ? t('queue.itemsQueued', { count: queuedCount }) : t('queue.printQueued'));
        }
        queryClient.invalidateQueries({ queryKey: ['auto-queue'] });
        invalidateQueueViews(queryClient);
        invalidateOrderCandidates(queryClient);
        onSuccess?.();
        onClose();
      } catch (err) {
        // The router tier is ONE request, so "was it added?" has one answer —
        // and a dropped connection still leaves it unknown rather than refused.
        if (isUnknownOutcome(err)) {
          invalidateQueueViews(queryClient);
          queryClient.invalidateQueries({ queryKey: ['auto-queue'] });
          invalidateOrderCandidates(queryClient);
          showToast(t('queueSpool.failure.uncertain'), 'error');
          onClose();
        } else {
          // One request, one target — the reason needs no printer label, and the
          // builder's bare form is exactly that case.
          showToast(
            queueAddOutcomeText(t, { added: 0, total: 1, failures: [{ label: '', error: err }] }),
            'error',
          );
        }
      } finally {
        setIsSubmitting(false);
      }
      return;
    }

    if (
      !options?.skipFilamentCheck &&
      !settings?.disable_filament_warnings &&
      (mode === 'reprint' || mode === 'add-to-queue')
    ) {
      const warningItems: FilamentWarningItem[] = [];

      // The spool check follows what is actually dispatched: one job per selected
      // plate, each with the mapping that plate's queue item carries. Two plates
      // can also draw on the same spool, so the demand is summed per tray before
      // it is weighed against what is left on it - 60 g left does not cover two
      // plates of 40 g, even though it covers either one of them (upstream #2551).
      const plateJobs = isMultiPlateSelection
        ? selectedPlateIds.map((plateId) => ({ plateId, reqs: perPlateReqs.get(plateId)?.filaments ?? [] }))
        : [{ plateId: selectedPlate, reqs: effectiveFilamentReqs?.filaments ?? [] }];

      if (plateJobs.some((job) => job.reqs.length > 0) && spoolAssignmentsByPrinter.size > 0) {
        const getRemainingWeight = (labelWeight: number, weightUsed: number) => {
          if (!Number.isFinite(labelWeight) || labelWeight <= 0) return null;
          if (!Number.isFinite(weightUsed) || weightUsed < 0) return null;
          return Math.max(0, labelWeight - weightUsed);
        };

        for (const printerId of selectedPrinters) {
          // In total mode a picked printer can end up dealt nothing at all —
          // the submit sends it no request, so a shortage on its spools is not
          // this batch's problem and must not raise a blocking warning about a
          // machine that was never going to print (spec §3).
          if (!plateJobs.some((job) => dealtCopies(job.plateId, printerId) > 0)) continue;

          const printerStatusForWarning = selectedPrinters.length > 1
            ? multiPrinterMapping.printerResults.find((result) => result.printerId === printerId)?.status
            : printerStatus;

          const loadedFilaments = buildLoadedFilaments(printerStatusForWarning);
          const slotLabelByTray = new Map(loadedFilaments.map((f) => [f.globalTrayId, f.label]));
          // AMS Filament Backup makes a group of same-preset, same-colour trays
          // interchangeable, so the print draws on their combined filament and
          // the check must too. `null` is "we could not read the flag",
          // which is not consent to pool — only a literal `true` groups.
          const backupOn = printerStatusForWarning?.ams_auto_switch_filament === true;
          const groupByTray = groupTraysForBackup(loadedFilaments, backupOn);
          const assignments = spoolAssignmentsByPrinter.get(printerId);
          const printerName = printers?.find((p) => p.id === printerId)?.name ?? `Printer ${printerId}`;

          if (!assignments) continue;

          const gramsByTray = new Map<number, number>();
          for (const job of plateJobs) {
            // The same question per plate: a plate this printer is dealt none
            // of costs it no filament.
            if (dealtCopies(job.plateId, printerId) === 0) continue;
            // No mapping means the scheduler picks the trays at dispatch, against
            // an AMS state we cannot see from here - nothing to weigh.
            const printerMapping = getMappingForPrinter(printerId, job.plateId);
            if (!printerMapping) continue;

            job.reqs.forEach((req) => {
              if (!req.slot_id || req.slot_id <= 0) return;
              const globalTrayId = printerMapping[req.slot_id - 1];
              if (!Number.isFinite(globalTrayId) || globalTrayId < 0) return;
              gramsByTray.set(globalTrayId, (gramsByTray.get(globalTrayId) ?? 0) + req.used_grams);
            });
          }

          // Demand pools per group. A demanded tray the status does not list
          // (an external spool the AMS never reported, say) is its own group —
          // there is nothing for the printer to swap it with.
          const demandByGroup = new Map<string, { group: BackupGroup; requiredGrams: number }>();
          for (const [globalTrayId, requiredGrams] of gramsByTray) {
            const group =
              groupByTray.get(globalTrayId) ??
              privateBackupGroup(globalTrayId, slotLabelByTray.get(globalTrayId) ?? `Tray ${globalTrayId}`);
            const pooled = demandByGroup.get(group.key);
            if (pooled) pooled.requiredGrams += requiredGrams;
            else demandByGroup.set(group.key, { group, requiredGrams });
          }

          for (const { group, requiredGrams } of demandByGroup.values()) {
            // What the group has left: the registered spool's own weight,
            // summed over the trays inventory knows. ⚠️ A tray with no assigned
            // spool adds NOTHING — it is unknown, not empty, and the rule is
            // the operator's: register the spool if it should count. The AMS
            // offers a fill percentage and only for RFID spools, so grams
            // guessed from it against an assumed reel would have suppressed
            // real warnings on a number nobody measured.
            let remainingGrams = 0;
            let anyKnown = false;
            for (const trayId of group.trayIds) {
              const spool = assignments.get(trayId)?.spool;
              const assigned = spool ? getRemainingWeight(spool.label_weight, spool.weight_used) : null;
              if (assigned === null) continue;
              remainingGrams += assigned;
              anyKnown = true;
            }

            // Nothing weighable in the whole group — same silence as before.
            if (!anyKnown) continue;
            if (remainingGrams >= requiredGrams) continue;

            warningItems.push({
              printerName,
              // Always the first of `slotLabels` — the two must not drift, and
              // the single-tray line is exactly the group line's one label.
              slotLabel: group.labels[0],
              slotLabels: group.labels,
              requiredGrams,
              remainingGrams,
            });
          }
        }
      }

      if (warningItems.length > 0) {
        setFilamentWarningItems(warningItems);
        return;
      }
    }

    // Validate printer selection
    if (selectedPrinters.length === 0) {
      showToast(t('printModal.selectAtLeastOnePrinter'), 'error');
      return;
    }

    setIsSubmitting(true);
    try {
      await ensureBatchOrder();
    } catch (err) {
      showToast(t('printModal.failedPrefix', { error: (err as Error).message }), 'error');
      setIsSubmitting(false);
      return;
    }
    const platesToQueue = selectedPlates.size > 1
      ? plates.filter(p => selectedPlates.has(p.index))
      : [null];
    // Attempts to make: one per (plate, printer) that gets at least one copy —
    // in total mode a printer can end with none of a plate and is skipped.
    const totalCount = platesToQueue.reduce(
      (sum, plate) =>
        sum + selectedPrinters.filter((id) => dealtCopies(plate ? plate.index : selectedPlate, id) > 0).length,
      0,
    );
    setSubmitProgress({ current: 0, total: totalCount });

    // ⚠️ `success` counts REQUESTS, `queued` counts ROWS, and they are not the
    // same number. One request carries a quantity and the server writes that
    // many items — `for i in range(data.quantity)` in `queue_add`. Four
    // printers at two copies each is four requests and eight queue entries, and
    // the toast reported four. `success`/`failed` stay a pair of attempt counts
    // because the partial-failure message pairs them.
    // Every row this submit creates, across plates and printers. A copy run
    // re-forms the source queue's blocks from these.
    const createdItemIds: number[] = [];
    const results: { success: number; failed: number; queued: number; errors: string[] } = {
      success: 0,
      failed: 0,
      queued: 0,
      errors: [],
    };
    // ⚠️ Every failure is kept as the ERROR OBJECT beside the target it belongs
    // to, not only as its text. The refusal that explains itself to the operator
    // is identified by its machine code (`source_copy_busy` vs
    // `source_unreadable`), and one printer's reason must never be read as
    // every printer's: the message groups these by reason and names the
    // printers. `results.errors` stays what it was — prose for the log-like
    // paths that still use it.
    const failures: QueueAddFailure[] = [];
    // A request whose answer never came back. Not a refusal: it may have been
    // committed, so the list is refreshed and nothing is re-posted (§5).
    let unknownOutcomes = 0;
    // Printers that got at least one row out of this submit. They are unticked
    // before the dialog is handed back, so a second press cannot duplicate them.
    const landedOn = new Set<number>();
    // Rows written per plate, keyed exactly as `planPlateIds` keys them (a
    // plate-less submit is 0). ⚠️ In TOTAL mode the quantity field is a total for
    // the whole submit, so a retry has to ask for `total − what landed`; without
    // this the dialog invites a second press (see `failure.deselected`) that
    // re-orders the full total over the printers that refused, and the farm
    // over-produces by whatever already went out.
    const queuedByPlate = new Map<number, number>();


    // Swap-macro payload is only meaningful on a swap-enabled printer AND
    // when the source file doesn't already ship with swap macros baked in
    // (swap_compatible → third-party tooling embedded them in the gcode).
    // For anything else we emit (false, null) so stored state never implies
    // macros will fire where they can't or would double-fire.
    const getSwapPayloadForPrinter = (printerId: number): {
      execute_swap_macros: boolean;
      swap_macro_events: string[] | null;
    } => {
      const printer = printers?.find(p => p.id === printerId);
      if (swapCompatible || !printer?.swap_mode_enabled || !swapMacros.execute || swapMacros.events.length === 0) {
        return { execute_swap_macros: false, swap_macro_events: null };
      }
      return { execute_swap_macros: true, swap_macro_events: swapMacros.events };
    };

    // Common queue data for add-to-queue and edit modes
    const getQueueData = (printerId: number, plateOverride?: number | null): PrintQueueItemCreate => {
      const plateId = plateOverride !== undefined ? plateOverride : selectedPlate;
      return {
      queue_id: printerId,  // queue_id == printer_id (always per-printer queue)
      enqueue_position: scheduleOptions.enqueuePosition,
      selected_macro_ids: selectedMacroIds,
      // A saved queue source is its own mutually-exclusive input.
      archive_id: isArchiveSource ? archiveId : undefined,
      library_file_id: isLibraryFile ? libraryFileId : undefined,
      source_queue_item_id: isSnapshotSource ? sourceQueueItemId : undefined,
      auto_off_after: scheduleOptions.autoOffAfter,
      manual_start: scheduleOptions.scheduleType === 'manual',
      require_previous_success: scheduleOptions.requirePreviousSuccess,
      ams_mapping: ((mode === 'edit-queue-item' && queueItem?.filament_routing?.mode === 'auto') || initialRouting?.mode === 'auto') &&
        Object.keys(manualMappings).length === 0 && Object.keys(manualMappingsByPlate).length === 0 &&
        !Object.values(perPrinterConfigs).some(config => !config.useDefault && !config.autoConfigured)
        ? undefined : getMappingForPrinter(printerId, plateId),
      ...{
        feed_policy: autoModeOptions.feed_policy,
        force_color_match: autoModeOptions.force_color_match,
        allow_base_material_match: autoModeOptions.allow_base_material_match,
        filament_overrides: autoOverrides,
      },
      plate_id: plateId,
      scheduled_time: scheduleOptions.scheduleType === 'scheduled' && scheduleOptions.scheduledTime
        ? new Date(scheduleOptions.scheduledTime).toISOString()
        : undefined,
      ...printOptions,
      ...getSwapPayloadForPrinter(printerId),
      quantity: dealtCopies(plateId, printerId),
      project_id: submitProjectId,
      project_line_id: submitProjectLineId,
      };
    };

    const isNextBlock = mode === 'add-to-queue' && scheduleOptions.enqueuePosition === 'next';

    // Loop through plates × printers
    let progressCounter = 0;
    if (isNextBlock) {
      // One request per printer keeps a multi-plate urgent job contiguous. A
      // batch is never shared between printers: their queues are independent.
      for (const printerId of selectedPrinters) {
        const entries = platesToQueue
          .map((plate) => ({ plate, plateId: plate ? plate.index : selectedPlate }))
          .filter(({ plateId }) => dealtCopies(plateId, printerId) > 0);
        if (entries.length === 0) continue;

        progressCounter += entries.length;
        setSubmitProgress({ current: progressCounter, total: totalCount });
        const printerName = printers?.find(p => p.id === printerId)?.name || `Printer ${printerId}`;
        try {
          const added = await addNextQueueBlockMutation.mutateAsync(
            entries.map(({ plateId }) => getQueueData(printerId, plateId)),
          );
          createdItemIds.push(...added.map(item => item.id));
          landedOn.add(printerId);
          for (const { plateId } of entries) {
            const copies = dealtCopies(plateId, printerId);
            results.success++;
            results.queued += copies;
            queuedByPlate.set(plateId ?? 0, (queuedByPlate.get(plateId ?? 0) ?? 0) + copies);
          }
        } catch (error) {
          if (isUnknownOutcome(error)) unknownOutcomes += entries.length;
          for (const { plate } of entries) {
            results.failed++;
            const plateName = plate ? (plate.name || t('printModal.plateNFallback', { index: plate.index })) : '';
            const label = plateName ? `${printerName} (${plateName})` : printerName;
            failures.push({ label, error });
            results.errors.push(`${label}: ${(error as Error).message}`);
          }
        }
      }
    } else for (const plate of platesToQueue) {
      const plateId = plate ? plate.index : selectedPlate;

      for (let i = 0; i < selectedPrinters.length; i++) {
        const printerId = selectedPrinters[i];
        const copies = dealtCopies(plateId, printerId);
        // Total mode dealt this printer none of this plate: nothing to send,
        // nothing to count — not an attempt, not a progress step.
        if (copies === 0) continue;
        progressCounter++;
        setSubmitProgress({ current: progressCounter, total: totalCount });

        try {
          if (mode === 'reprint') {
            // Reprint mode - start print immediately (single plate only, multi-select not available)
            const printerMapping = getMappingForPrinter(printerId, plateId);
            const swapPayload = getSwapPayloadForPrinter(printerId);
            if (isLibraryFile) {
              await api.printLibraryFile(libraryFileId!, printerId, {
                plate_id: selectedPlate ?? undefined,
                plate_name: selectedPlateName,
                ams_mapping: printerMapping,
                feed_policy: autoModeOptions.feed_policy,
                force_color_match: autoModeOptions.force_color_match,
                allow_base_material_match: autoModeOptions.allow_base_material_match,
                filament_overrides: autoOverrides,
                ...printOptions,
                ...swapPayload,
                selected_macro_ids: selectedMacroIds,
                quantity: copies,
                project_id: submitProjectId,
                project_line_id: submitProjectLineId,
                cleanup_library_after_dispatch: cleanupLibraryAfterDispatch,
              });
            } else {
              // project_id (and with it project_line_id) is intentionally omitted here:
              // reprintArchive targets an existing archive that already carries its own
              // order association from the original print.
              await api.reprintArchive(archiveId!, printerId, {
                plate_id: selectedPlate ?? undefined,
                plate_name: selectedPlateName,
                ams_mapping: printerMapping,
                feed_policy: autoModeOptions.feed_policy,
                force_color_match: autoModeOptions.force_color_match,
                allow_base_material_match: autoModeOptions.allow_base_material_match,
                filament_overrides: autoOverrides,
                ...printOptions,
                ...swapPayload,
                selected_macro_ids: selectedMacroIds,
                quantity: copies,
              });
            }
          } else if (mode === 'edit-queue-item' && progressCounter === 1) {
            // Edit mode - update the original queue item for the first entry
            const printerMapping = getMappingForPrinter(printerId, plateId);
            const updateData: PrintQueueItemUpdate = {
              queue_id: printerId,  // queue_id == printer_id
              selected_macro_ids: selectedMacroIds,
              auto_off_after: scheduleOptions.autoOffAfter,
              manual_start: scheduleOptions.scheduleType === 'manual',
              require_previous_success: scheduleOptions.requirePreviousSuccess,
              ams_mapping: printerMapping,
              plate_id: plateId,
              scheduled_time: scheduleOptions.scheduleType === 'scheduled' && scheduleOptions.scheduledTime
                ? new Date(scheduleOptions.scheduledTime).toISOString()
                : null,
              ...printOptions,
              ...getSwapPayloadForPrinter(printerId),
            };
            await updateQueueMutation.mutateAsync(updateData);
          } else {
            // Add-to-queue mode OR edit mode with additional entries
            const added = await addToQueueMutation.mutateAsync(getQueueData(printerId, plateId));
            // ⚠️ `created_item_ids`, not `id`: a quantity becomes rows, and the
            // response's own `id` is only the first of them.
            createdItemIds.push(...(added.created_item_ids ?? (added.id != null ? [added.id] : [])));
          }
          results.success++;
          landedOn.add(printerId);
          // Edit mode replaces one row; everything else writes one per copy.
          results.queued += copies;
          queuedByPlate.set(plateId ?? 0, (queuedByPlate.get(plateId ?? 0) ?? 0) + copies);
        } catch (error) {
          results.failed++;
          if (isUnknownOutcome(error)) unknownOutcomes++;
          const printerName = printers?.find(p => p.id === printerId)?.name || `Printer ${printerId}`;
          const plateName = plate ? (plate.name || t('printModal.plateNFallback', { index: plate.index })) : '';
          const label = plateName ? `${printerName} (${plateName})` : printerName;
          failures.push({ label, error });
          results.errors.push(`${label}: ${(error as Error).message}`);
        }
      }
    }

    setIsSubmitting(false);

    // Show result toast (skip for reprint mode - the dispatch toast handles it)
    if (results.failed === 0) {
      // Persist saved-toggles preference once we know at least one submission
      // landed. Skipped automatically in edit mode (effectivePrinterModel is
      // null there). Fire-and-forget — failure to save the preference must
      // not block the success UX.
      persistPreference();
      reportAnswer();
      if (createdItemIds.length > 0) onQueued?.(createdItemIds);
      if (mode !== 'reprint' && announces) {
        if (mode === 'edit-queue-item') {
          showToast(t('printModal.queueItemUpdated'));
        } else if (results.queued === 1) {
          showToast(t('queue.printQueued'));
        } else {
          showToast(t('queue.itemsQueued', { count: results.queued }));
        }
      }
      invalidateQueueViews(queryClient);
      invalidateOrderCandidates(queryClient);
      onSuccess?.();
      onClose();
    } else if (mode === 'add-to-queue') {
      // ⚠️ **Every add refusal leads with whether a job exists**, and then gives
      // a reason PER PRINTER. That is the operator's actual question, and an add
      // is all-or-nothing per request (§5: a copy failure leaves no runnable
      // row), so the counters answer it exactly. `success`/`failed` stay a pair
      // of ATTEMPT counts — the partial sentence pairs them, and folding them
      // into one number is how the queued-count bug happened.
      //
      // ⚠️ **An unanswered request is not a refusal** (§5): the server may well
      // have committed before the connection died, a second POST would be a
      // second job, and the rule is refresh-and-look, never re-send. But the
      // answered refusals beside it keep their own reasons — losing an
      // actionable one to a sibling's uncertainty is the same defect as
      // misattributing it.
      const unknownOnly = unknownOutcomes > 0;
      // ⚠️ **What already landed must not still be armed.** This branch used to
      // leave the dialog standing with every printer ticked and «try again in a
      // moment» on screen: pressing Add again gave the printer that succeeded a
      // SECOND job. So the succeeded printers are unticked; when nothing would
      // be left to retry (one printer, several plates, some of them refused) the
      // dialog closes instead and the queue is where the operator looks.
      const retryable = selectedPrinters.filter((id) => !landedOn.has(id));
      const deselected = landedOn.size > 0 && retryable.length > 0 && !unknownOnly;
      // ⚠️ **And a retry must ask for what is MISSING, not for the original
      // total.** In `total` mode the field is one number for the whole submit,
      // dealt round-robin; leaving it at 10 after 4 copies landed means the next
      // press orders 10 more across the printers that refused. Per-printer mode
      // needs nothing — its number is already per machine, and the machines that
      // took work are gone from the selection.
      //
      // The leftover is computed per PLATE, because each plate carries its own
      // total: one plate writes the shared field (what the operator sees is then
      // what the deal does), several write the per-plate map the plan lines and
      // the plate list already display. Touching the field clears that map, which
      // is the operator taking the number back — exactly right.
      let stillMissing: number | null = null;
      if (deselected) {
        setSelectedPrinters(retryable);
        if (effectiveQuantityMode === 'total') {
          const remaining = planPlateIds.map(
            (plate) => [plate, Math.max(0, quantityForPlate(plate) - (queuedByPlate.get(plate) ?? 0))] as const,
          );
          const total = remaining.reduce((sum, [, left]) => sum + left, 0);
          // `total === 0` needs every row to have landed, which contradicts a
          // failure being in this branch — so it cannot happen, and if it ever
          // did, leaving the field alone beats writing a zero into it.
          if (total > 0) {
            stillMissing = total;
            if (remaining.length === 1) {
              setQuantity(total);
              setPlateQuantities({});
            } else {
              setPlateQuantities(Object.fromEntries(remaining));
            }
          }
        }
      }
      showToast(
        queueAddOutcomeText(t, {
          added: results.success,
          total: results.success + results.failed,
          failures,
          deselected,
          stillMissing,
        }),
        'error',
      );
      if (results.success > 0 || unknownOnly) {
        invalidateQueueViews(queryClient);
        invalidateOrderCandidates(queryClient);
      }
      // Closing is the honest ending whenever the form left standing could
      // duplicate something: an unknown outcome (we do not know what landed) or
      // a submit whose every selected printer already took work.
      if (unknownOnly || (landedOn.size > 0 && retryable.length === 0)) onClose();
    } else if (results.success === 0) {
      showToast(t('printModal.failedPrefix', { error: results.errors[0] }), 'error');
    } else {
      showToast(t('printModal.partialSuccess', { success: results.success, failed: results.failed }), 'error');
      invalidateQueueViews(queryClient);
      invalidateOrderCandidates(queryClient);
    }
  };

  /**
   * The one gate against a second submit (spec §10: "повторне натискання
   * заблоковано").
   *
   * ⚠️ **The disabled button is not that gate.** It only disables on the next
   * render, and neither Enter in a field nor a second click that beats that
   * render goes through it — measured: three POSTs, three jobs, from one press
   * of one dialog. The ref is checked and set synchronously, before the first
   * `await`, so there is no window at all.
   *
   * ⚠️ It must be released on EVERY exit, the early ones included: the low-spool
   * warning returns without submitting and the operator's «print anyway» calls
   * straight back in here.
   */
  const submitInFlightRef = useRef(false);
  const handleSubmit = async (e?: React.FormEvent, options?: { skipFilamentCheck?: boolean }) => {
    // The default has to be prevented even when the guard swallows the submit,
    // or the second Enter navigates the browser away from the app.
    e?.preventDefault();
    if (submitInFlightRef.current) return;
    submitInFlightRef.current = true;
    try {
      await runSubmit(e, options);
    } finally {
      submitInFlightRef.current = false;
    }
  };

  const isPending = isSubmitting || updateQueueMutation.isPending || addNextQueueBlockMutation.isPending;

  const canSubmit = useMemo(() => {
    if (isPending) return false;
    // The payload names only this queue row. Until its source profile has
    // arrived, or after it has refused, there is no safe substitute source.
    if (isSnapshotSource && (!queueSourceProfile || queueSourceProfileError)) return false;

    // Auto mode: no specific printer required (router picks one). Plate gate still applies.
    if (isAutoMode) {
      if (isMultiPlate && selectedPlates.size === 0) return false;
      return routingSourceReady && !routingPreview.isPending && !routingPreview.isError;
    }

    // Need at least one printer selected
    if (selectedPrinters.length === 0) return false;

    // For multi-plate files, need at least one plate selected
    if (isMultiPlate && selectedPlates.size === 0) return false;

    // Every selected plate has to have answered before we can queue it: a plate
    // still in flight would be sent with no mapping, and one that failed to load
    // cannot be mapped at all. Deselect the failing plate to queue the rest - the
    // banner below the plate list says which state we are in (upstream #2552).
    if (perPlateReqsPending || perPlateReqsFailed) return false;

    return true;
  }, [
    isAutoMode,
    routingSourceReady,
    routingPreview.isPending,
    routingPreview.isError,
    selectedPrinters.length,
    isMultiPlate,
    selectedPlates.size,
    isPending,
    isSnapshotSource,
    queueSourceProfile,
    queueSourceProfileError,
    perPlateReqsPending,
    perPlateReqsFailed,
  ]);

  // --- Self-submit for a grouped run ------------------------------------
  // A group's silent members never render: the dialog decides for itself and
  // submits, so the operator answers once per group instead of once per file.
  //
  // ⚠️ `canSubmit` is necessary but NOT sufficient, and the gap is silent.
  // It waits for the plates and — for a multi-plate selection — for each plate's
  // requirements, but it knows nothing about the printer's status or about the
  // single-plate requirements query. Both read `undefined` while loading, and
  // both feed the verdict in opposite directions: an empty `loadedFilaments`
  // refuses EVERY plate that needs any filament, an empty requirement list
  // accepts every plate whatever is loaded. Deciding early therefore either
  // makes the feature do nothing or queues against the wrong reel, and neither
  // looks broken. So we wait for the honest signals — a settled query, not a
  // non-empty array: a printer with genuinely nothing loaded is a real state
  // that must still be allowed to refuse.
  const autoSubmittedRef = useRef(false);
  const [autoSubmitRefused, setAutoSubmitRefused] = useState(false);

  // `handleSubmit` is rebuilt every render, and wrapping it in `useCallback`
  // would mean listing every piece of dialog state it reads — a dependency list
  // that would go stale silently. The self-submit only ever needs the latest
  // one, so it travels by ref instead of through the dep array below. Declared
  // before that effect so the ref is refreshed first in every commit.
  const submitRef = useRef(handleSubmit);
  useEffect(() => {
    submitRef.current = handleSubmit;
  });

  useEffect(() => {
    if (!autoSubmitWhenUnambiguous || autoSubmittedRef.current || autoSubmitRefused) return;

    // ⚠️ `canSubmit` is a two-valued answer to a three-valued question. Most of
    // its false cases are "not yet" — plates loading, per-plate requirements in
    // flight — and waiting is right. Two are "never", and waiting for those is
    // the whole-run hang this guard exists to stop: the member neither submits
    // nor renders, and the sequencer only ever advances on `onClose`.
    if (!canSubmit) {
      // A per-plate requirements query that ERRORED. `retry: false`, so nothing
      // is coming, and multi-plate members are the normal case for a grouped run.
      const reqsWillNeverAnswer =
        perPlateReqsFailed ||
        queueSourceProfileError ||
        (isAutoMode && !routingPreview.isPending && !routingSourceReady);
      // No printer, and nothing left that could choose one. The single-printer
      // auto-select is the only filler, and it fires from the effect above in
      // this same commit — so ask whether it EXISTS (`soleActivePrinterId`)
      // rather than whether it has run, or a one-printer farm would refuse
      // every member in the commit the printer list arrives.
      const printerWillNeverArrive =
        !isAutoMode && printersFetched && selectedPrinters.length === 0 && soleActivePrinterId === null;
      if (reqsWillNeverAnswer || printerWillNeverArrive) {
        // Show ourselves: a dialog the operator can finish beats a blank page.
        setAutoSubmitRefused(true);
      }
      return;
    }

    // Another "not yet": the order question is still in flight. A silent
    // member files itself under the order that needs its plate exactly as the
    // visible one does, and it cannot do that before the answer arrives —
    // it would queue 59 of a 60-plate run under no order at all while the one
    // dialog the operator saw filed correctly. The submit BUTTON is gated on
    // the same flag, so both halves wait for the same thing.
    if (orderAnswerPending) return;

    // Only a run at exactly one specific printer consults filaments at all:
    // `canQueueWithoutAsking` short-circuits on any other printer count. An
    // auto-queue or fan-out member must therefore not wait for a status that
    // is never fetched.
    const consultsPrinter = !isAutoMode && selectedPrinters.length === 1;
    if (consultsPrinter) {
      // No status, no verdict — and a refusal is the safe half of the guess.
      if (printerStatusFailed) {
        setAutoSubmitRefused(true);
        return;
      }
      if (!printerStatusLoaded) return;
    }

    // The requirement source is the one the submit path itself uses: per plate
    // when several are ticked, the file-level query otherwise. `canSubmit`
    // already covers the per-plate half via `perPlateReqsPending/Failed`.
    if (!isMultiPlateSelection) {
      if (effectiveFilamentReqsError) {
        setAutoSubmitRefused(true);
        return;
      }
      if (effectiveFilamentReqs === undefined) return;
    }
    const plateRequirements = isMultiPlateSelection
      ? selectedPlateIds.map((plateId) => perPlateReqs.get(plateId)?.filaments ?? [])
      : [effectiveFilamentReqs?.filaments ?? []];

    const loaded = buildLoadedFilaments(printerStatus);
    const refused = plateRequirements.some(
      (requirements) =>
        !canQueueWithoutAsking({
          requirements,
          loadedFilaments: loaded,
          printerCount: isAutoMode ? 0 : selectedPrinters.length,
          ftsActive: printerStatus?.fila_switch?.installed === true,
          trayNow: printerStatus?.tray_now,
        }).ok,
    );
    if (refused) {
      // Show ourselves instead. The operator sees every plate of this file with
      // its own mapping panel, so which plate is the problem is visible.
      setAutoSubmitRefused(true);
      return;
    }
    // The ref makes it fire once: `canSubmit` stays true after the submit starts.
    autoSubmittedRef.current = true;
    // ⚠️ The submit path does not always end in `onClose`, and a silent member
    // that neither closes nor renders stalls the whole run with nothing on
    // screen — the sequencer advances on `onClose` and has no other signal.
    // Two endings do exactly that: a low-spool warning, whose ConfirmModal
    // lives in the JSX we are suppressing, and a failed or partial dispatch,
    // which only shows a toast and leaves the dialog standing. Both are
    // questions for the operator, so when the submit RETURNS without having
    // closed us, we stop being silent and let them finish it.
    //
    // ⚠️ This hangs off the promise and NOT off watching `isPending` fall back
    // to false. That inference needs React to commit a render while the submit
    // is in flight, and it does not always get one: when the round-trip
    // resolves before the scheduled render flushes, `setIsSubmitting(true)` and
    // `(false)` coalesce, the dep never changes, the effect never re-runs and
    // the member renders `null` for ever. Measured 6 stalls in 8 runs of
    // `QueueSequencerAntiStall` before this became a `.finally`.
    void submitRef.current().finally(() => setAutoSubmitRefused(true));
  }, [
    autoSubmitWhenUnambiguous,
    autoSubmitRefused,
    canSubmit,
    isAutoMode,
    selectedPrinters.length,
    printerStatus,
    printerStatusLoaded,
    printerStatusFailed,
    isMultiPlateSelection,
    selectedPlateIds,
    perPlateReqs,
    effectiveFilamentReqs,
    effectiveFilamentReqsError,
    perPlateReqsFailed,
    queueSourceProfileError,
    printersFetched,
    soleActivePrinterId,
    orderAnswerPending,
    routingPreview.isPending,
    routingSourceReady,
  ]);

  // Tell the run it had to ask after all. Once only, and by ref rather than by
  // dep array: the caller's handler is an inline arrow whose identity changes
  // every render.
  const refusalReportedRef = useRef(false);
  useEffect(() => {
    if (!autoSubmitRefused || refusalReportedRef.current) return;
    refusalReportedRef.current = true;
    onAutoSubmitRefused?.();
  }, [autoSubmitRefused, onAutoSubmitRefused]);

  // Modal title and action button text based on mode
  const getModalConfig = () => {
    // The button counts the printers this submit will actually write to, not
    // the ones ticked: in total mode a picked printer can be dealt nothing of
    // every plate, and «Queue to 3 printers» would then name a machine that
    // gets no request at all.
    const printerCount = selectedPrinters.filter((id) => planPlateIds.some((p) => copiesFor(p, id) > 0)).length;

    if (mode === 'reprint') {
      return {
        title: isLibraryFile ? t('queue.print') : t('queue.reprint'),
        icon: Printer,
        submitText: printerCount > 1 ? t('queue.printToPrinters', { count: printerCount }) : t('queue.print'),
        submitIcon: Printer,
        loadingText: submitProgress.total > 1
          ? t('queue.sendingProgress', { current: submitProgress.current, total: submitProgress.total })
          : t('queue.sending'),
      };
    }
    if (mode === 'edit-auto-item') {
      const batch = (autoQueueBatchCount ?? 1) > 1;
      return {
        title: batch
          ? t('autoQueue.editBatchTitle', { count: autoQueueBatchCount })
          : t('autoQueue.editItemTitle'),
        icon: Pencil,
        submitText: batch
          ? t('autoQueue.saveBatch', { count: autoQueueBatchCount })
          : t('common.save'),
        submitIcon: Pencil,
        loadingText: t('common.saving'),
      };
    }
    if (mode === 'add-to-queue') {
      let submitText = t('queue.addToQueue');
      if (selectedPlates.size > 1) {
        submitText = t('queue.queueSelectedPlates', { count: selectedPlates.size });
      } else if (printerCount > 1) {
        submitText = t('queue.queueToPrinters', { count: printerCount });
      }
      return {
        title: t('queue.schedulePrint'),
        icon: Calendar,
        submitText,
        submitIcon: Calendar,
        // ⚠️ **What the wait actually is** (§10): the file is being copied into
        // BamDude's own data directory, and until that lands there is no job.
        // The count is REQUESTS, not bytes — there is no progress API to read a
        // percentage from, and a made-up one would be a promise about a copy
        // nobody is measuring.
        loadingText: submitProgress.total > 1
          ? t('queueSpool.savingProgress', { current: submitProgress.current, total: submitProgress.total })
          : t('queueSpool.saving'),
      };
    }
    // edit-queue-item mode
    return {
      title: t('queue.editQueueItem'),
      icon: Pencil,
      submitText: t('common.save'),
      submitIcon: Pencil,
      loadingText: submitProgress.total > 1
        ? t('queue.savingProgress', { current: submitProgress.current, total: submitProgress.total })
        : t('common.saving'),
    };
  };

  const modalConfig = getModalConfig();
  const TitleIcon = modalConfig.icon;
  const SubmitIcon = modalConfig.submitIcon;

  // Show filament mapping when:
  // - Single printer selected
  // - For archives: plate is selected (for multi-plate) or not required (single-plate)
  // - For library files: always show (no plate selection)
  const showFilamentMapping = effectivePrinterId && selectedPlates.size <= 1 && (
    isLibraryFile || isSnapshotSource || (isMultiPlate ? selectedPlate !== null : true)
  );

  // Several plates on one printer: one mapping panel per plate, each mapping only
  // the slots its own plate prints. A multi-printer fan-out would be a panel per
  // plate *per printer*, so those items ship without a mapping and the scheduler
  // computes one per plate when it picks the printer (upstream #2551).
  const showPerPlateFilamentMapping =
    !!effectivePrinterId && isMultiPlateSelection && selectedPrinters.length === 1;

  // A grouped run's silent members must not flash on screen. Once refused — by
  // the eligibility gate or by an ending that needs an answer — we render
  // normally and the operator finishes the job. ⚠️ Must stay below every hook,
  // and `announces` in `runSubmit` is exactly this condition negated — keep
  // the two in step.
  if (autoSubmitWhenUnambiguous && !autoSubmitRefused) return null;

  return (
    <>
      <Modal
        onClose={onClose}
        closeDisabled={isSubmitting}
        labelledBy={headingId}
        size="2xl"
        header={
          <>
            <TitleIcon className="w-5 h-5 text-bambu-green" />
            <h2 id={headingId} className="text-lg font-semibold text-white">{modalConfig.title}</h2>
            {/* Only thing that says a run over several files is under way —
                every dialog in it is otherwise identical. */}
            {sequence && (
              <span
                className="px-2 py-0.5 rounded-full bg-bambu-dark text-xs text-bambu-gray tabular-nums"
                title={t('printModal.fileOfTotal', sequence)}
                aria-label={t('printModal.fileOfTotal', sequence)}
              >
                {sequence.current}/{sequence.total}
              </span>
            )}
            {/* Same job for a run over GROUPS: which group this is, and how
                many plates go out when it is answered. */}
            {groupBadge && (
              <span className="px-2 py-0.5 rounded-full bg-bambu-dark text-xs text-bambu-gray tabular-nums">
                {/* ⚠️ `units` travels as `count`: i18next resolves the
                    plural from that name and no other, and a one-unit group
                    is reachable whenever there is more than one group. */}
                {t('queue.groupBadge', {
                  current: groupBadge.current,
                  total: groupBadge.total,
                  count: groupBadge.units,
                })}
              </span>
            )}
            {/* The group's own answer to "must I see the rest of these?"
                ⚠️ Only where there IS a rest: a one-member group has nothing
                to apply to, and offering the choice there is noise.
                ⚠️ The hint counts the OTHERS (units - 1), not the group. */}
            {groupBadge && groupBadge.units > 1 && onApplyToRestChange && (
              <label
                className="flex items-center gap-1.5 text-xs text-bambu-gray cursor-pointer select-none"
                title={t('queue.applyToRestHint', { count: groupBadge.units - 1 })}
              >
                <input
                  type="checkbox"
                  className="accent-bambu-green"
                  checked={applyToRest !== false}
                  onChange={(e) => onApplyToRestChange(e.target.checked)}
                />
                {t('queue.applyToRest')}
              </label>
            )}
          </>
        }
      >
        <div className="p-4">
          <form onSubmit={handleSubmit} className={mode === 'reprint' ? '' : 'space-y-4'}>
            {/* Dispatch mode toggle — only for add-to-queue.
                Reprint is always specific; edit is bound to an existing per-printer row.
                Hidden via lockDispatchMode when the modal was opened from a drop
                target that implies the mode (queue card → specific, auto-queue
                panel → auto). */}
            {mode === 'add-to-queue' && !lockDispatchMode && (
              <div className="flex gap-2 p-1 bg-bambu-dark rounded-lg" role="radiogroup">
                <button
                  type="button"
                  role="radio"
                  aria-checked={dispatchMode === 'specific'}
                  onClick={() => setDispatchMode('specific')}
                  className={`flex-1 text-sm py-1.5 rounded transition-colors ${
                    dispatchMode === 'specific'
                      ? 'bg-bambu-green text-white font-medium'
                      : 'text-bambu-gray hover:text-white'
                  }`}
                >
                  {t('printModal.dispatchModeSpecific')}
                </button>
                <button
                  type="button"
                  role="radio"
                  aria-checked={dispatchMode === 'auto'}
                  onClick={() => setDispatchMode('auto')}
                  className={`flex-1 text-sm py-1.5 rounded transition-colors ${
                    dispatchMode === 'auto'
                      ? 'bg-bambu-green text-white font-medium'
                      : 'text-bambu-gray hover:text-white'
                  }`}
                >
                  {t('printModal.dispatchModeAuto')}
                </button>
              </div>
            )}

            {/* Archive name */}
            <p className={`text-sm text-bambu-gray ${mode === 'reprint' ? 'mb-4' : ''}`}>
              {mode === 'reprint' ? (
                <>
                  {t('printModal.sendLabel')} <span className="text-white">{archiveName}</span> {t('printModal.toLabel')}{' '}
                  {initialSelectedPrinterIds?.length === 1 && printers
                    ? <span className="text-white">{printers.find(p => p.id === initialSelectedPrinterIds[0])?.name ?? t('printModal.selectPrinter')}</span>
                    : t('printModal.selectPrinter')}
                </>
              ) : (
                <>
                  <span className="block text-bambu-gray mb-1">{t('printModal.printJob')}</span>
                  <span className="text-white font-medium truncate block">{archiveName}</span>
                </>
              )}
            </p>

            {/* Build-plate badge for the selected (or sole) plate — surfaced
                early so the user knows which plate to mount before scheduling
                (#1281). PlateSelector renders its own per-plate badges for
                multi-plate files; this covers the single-plate case and the
                multi-plate case where exactly one plate is selected. */}
            {(() => {
              if (!plates.length) return null;
              const target = selectedPlate != null ? plates.find((p) => p.index === selectedPlate) : plates[0];
              const bed = getBedTypeInfo(target?.bed_type);
              if (!bed) return null;
              return (
                <p className="flex items-center gap-1.5 text-xs text-bambu-gray -mt-2" title={bed.label}>
                  <img src={bed.icon} alt="" className="w-4 h-4 object-contain flex-shrink-0" />
                  <span className="truncate">{bed.label}</span>
                </p>
              );
            })()}

            {/* Plate selection - first so users know filament requirements
                before selecting printers. Hidden in edit-auto-item: the plate
                is a property of the row (one plate = one row) and the update
                schema deliberately has no plate_id. */}
            {mode !== 'edit-auto-item' && <PlateSelector
              plates={plates}
              isMultiPlate={isMultiPlate}
              selectedPlates={selectedPlates}
              onToggle={(plateIndex) => {
                setSelectedPlates(prev => {
                  const next = new Set(prev);
                  if (mode === 'add-to-queue') {
                    // Multi-select: toggle the plate
                    if (next.has(plateIndex)) {
                      next.delete(plateIndex);
                    } else {
                      next.add(plateIndex);
                    }
                  } else {
                    // Single-select: replace selection
                    next.clear();
                    next.add(plateIndex);
                  }
                  return next;
                });
              }}
              onSelectAll={mode === 'add-to-queue' ? () => setSelectedPlates(new Set(plates.map(p => p.index))) : undefined}
              onDeselectAll={mode === 'add-to-queue' ? () => setSelectedPlates(new Set()) : undefined}
              multiSelect={mode === 'add-to-queue'}
              quantities={Object.fromEntries(
                [...selectedPlates].map((index) => [index, quantityForPlate(index)]),
              )}
              onQuantityChange={
                mode === 'add-to-queue'
                  ? (plateIndex, value) => setPlateQuantities((prev) => ({ ...prev, [plateIndex]: value }))
                  : undefined
              }
            />}

            {/* Which order this print counts against. Below the plate picker
                because the answer depends on the plate — a different plate
                yields different parts and so answers to a different line. */}
            {asksAboutOrder && (offerNewOrder || (orderCandidates?.length ?? 0) > 0) && (
              <OrderFilingField
                value={orderFiling}
                onChange={(v) => { setOrderFilingTouched(true); setChosenOrderFiling(v); }}
                candidates={orderCandidates}
                loading={orderCandidatesLoading}
                offerNewOrder={offerNewOrder}
              />
            )}

            {initialRouting && !isAutoMode && <p className="text-sm text-amber-300">
              {t(initialRouting.mode === 'pinned' ? 'filamentRouting.copyReview' : 'filamentRouting.copyRules')}
            </p>}

            {!isAutoMode && <div className="space-y-2 text-sm">
              <label className="block text-bambu-gray">{t('filamentRouting.feedPolicy')}
                <select value={autoModeOptions.feed_policy ?? 'auto'} className="ml-2 bg-bambu-dark-secondary text-white rounded p-1"
                  onChange={event => setAutoModeOptions(previous => ({ ...previous, feed_policy: event.target.value as AutoModeOptionsState['feed_policy'] }))}>
                  <option value="auto">{t('filamentRouting.feedAuto')}</option>
                  <option value="ams_only">{t('filamentRouting.feedAms')}</option>
                  <option value="external_only">{t('filamentRouting.feedExternal')}</option>
                </select>
              </label>
              <label className="flex gap-2 items-center text-white">
                <input type="checkbox" checked={autoModeOptions.force_color_match}
                  onChange={event => setAutoModeOptions(previous => ({ ...previous, force_color_match: event.target.checked }))} />
                {t('printModal.autoMode.forceColorMatch')}
              </label>
              <label className="flex gap-2 items-center text-white">
                <input type="checkbox" checked={autoModeOptions.allow_base_material_match}
                  onChange={event => setAutoModeOptions(previous => ({ ...previous, allow_base_material_match: event.target.checked }))} />
                <span>{t('filamentRouting.baseMaterialMatch')}</span>
              </label>
            </div>}

            {/* Auto-distribute mode controls — replaces PrinterSelector */}
            {isAutoMode && (
              <AutoModeOptions
                options={autoModeOptions}
                preview={routingPreview.data}
                loading={routingPreview.isPending}
                failed={routingPreview.isError}
                onRetry={() => { void routingPreview.refetch(); }}
                overrides={autoOverrides}
                onOverridesChange={setAutoOverrides}
                onChange={setAutoModeOptions}
                printers={printers}
                slicedForModel={slicedForModel}
                locked={lockAutoTarget}
              />
            )}

            {/* Printer selection with per-printer mapping.
                ⚠️ Hidden when a printer arrives pre-selected — EXCEPT when the
                run is pinned (``lockPrinterSelection``), where it renders the
                one printer, ticked and untickable. Hiding it answers "which
                printer" by never asking, so the dialog stops saying where the
                print is going; a pinned row says it and takes nothing away. */}
            {!isAutoMode && (!initialSelectedPrinterIds?.length || lockPrinterSelection) && (
              <PrinterSelector
                printers={
                  lockPrinterSelection
                    ? (printers || []).filter((p) => selectedPrinters.includes(p.id))
                    : printers || []
                }
                pausedQueuePrinterIds={pausedQueuePrinterIds}
                locked={lockPrinterSelection}
                selectedPrinterIds={selectedPrinters}
                onMultiSelect={setSelectedPrinters}
                isLoading={loadingPrinters}
                allowMultiple={true}
                showInactive={mode === 'edit-queue-item'}
                disableBusy={mode === 'reprint'}
                printerMappingResults={multiPrinterMapping.printerResults}
                // The per-printer tray editor inside the selector maps ONE filament list
                // onto each printer. Several plates have several lists, and a fan-out across
                // printers ships no mapping at all (the scheduler maps each plate against
                // the printer it picks), so the editor would be collecting tray choices it
                // then throws away. Withhold its input (upstream #2552).
                filamentReqs={isMultiPlateSelection ? undefined : routingFilamentReqs}
                onAutoConfigurePrinter={multiPrinterMapping.autoConfigurePrinter}
                onUpdatePrinterConfig={multiPrinterMapping.updatePrinterConfig}
                slicedForModel={slicedForModel}
                swapCompatible={swapCompatible}
              />
            )}

            {/* Compatibility warning when sliced model doesn't match selected printer */}
            {!isAutoMode && slicedForModel && selectedPrinters.length === 1 && (() => {
              const selectedPrinter = printers?.find(p => p.id === selectedPrinters[0]);
              if (selectedPrinter && selectedPrinter.model && slicedForModel !== selectedPrinter.model) {
                return (
                  <div className="p-3 mb-2 bg-yellow-50 dark:bg-yellow-500/10 border border-yellow-300 dark:border-yellow-500/30 rounded-lg flex items-center gap-2">
                    <AlertTriangle className="w-4 h-4 text-yellow-600 dark:text-yellow-400 flex-shrink-0" />
                    <span className="text-sm text-yellow-700 dark:text-yellow-400">
                      {t('printModal.slicedForWarning', { slicedModel: slicedForModel, printerModel: selectedPrinter.model })}
                    </span>
                  </div>
                );
              }
              return null;
            })()}

            {/* Warning when archive data couldn't be loaded */}
            {archiveDataMissing && (
              <div className="flex items-start gap-2 p-3 mb-2 bg-orange-50 dark:bg-orange-500/10 border border-orange-300 dark:border-orange-500/30 rounded-lg text-sm">
                <AlertCircle className="w-4 h-4 text-orange-600 dark:text-orange-400 mt-0.5 flex-shrink-0" />
                <p className="text-orange-700 dark:text-orange-400">
                  {t('printModal.archiveDataUnavailable')}
                </p>
              </div>
            )}

            {/* A selected plate whose filaments could not be read cannot be mapped, so
                it is not queued silently - say so and hold the button until the plate
                is deselected (upstream #2552). */}
            {perPlateReqsFailed && (
              <div className="flex items-start gap-2 p-3 mb-2 bg-orange-50 dark:bg-orange-500/10 border border-orange-300 dark:border-orange-500/30 rounded-lg text-sm">
                <AlertCircle className="w-4 h-4 text-orange-600 dark:text-orange-400 mt-0.5 flex-shrink-0" />
                <p className="text-orange-700 dark:text-orange-400">
                  {t('printModal.plateFilamentsUnreadable')}
                </p>
              </div>
            )}

            {/* Filament mapping - only show when single printer selected (not in auto mode) */}
            {!isAutoMode && showFilamentMapping && !archiveDataMissing && selectedPrinters.length === 1 && (
              <FilamentMapping
                printerId={effectivePrinterId!}
                filamentReqs={routingFilamentReqs}
                manualMappings={manualMappings}
                onManualMappingChange={setManualMappings}
                requireExactColor={autoModeOptions.force_color_match}
                defaultExpanded={!!initialSelectedPrinterIds?.length || (settings?.per_printer_mapping_expanded ?? false)}
                currencySymbol={currencySymbol}
                defaultCostPerKg={defaultCostPerKg}
              />
            )}

            {/* Filament mapping, one panel per selected plate — each plate is its
                own print with its own slots, so it gets its own AMS mapping. */}
            {!isAutoMode && showPerPlateFilamentMapping && !archiveDataMissing && selectedPlateIds.map((plateId) => {
              const plateReqs = perPlateReqs.get(plateId);
              if (!plateReqs) return null;
              const plate = platesData?.plates?.find((p) => p.index === plateId);
              return (
                <FilamentMapping
                  key={plateId}
                  printerId={effectivePrinterId!}
                  plateLabel={plate?.name || t('printModal.plateNumber', { number: plateId })}
                  filamentReqs={plateReqs}
                  manualMappings={manualMappingsByPlate[plateId] ?? {}}
                  onManualMappingChange={(mappings) =>
                    setManualMappingsByPlate((prev) => ({ ...prev, [plateId]: mappings }))
                  }
                  defaultExpanded={false}
                  currencySymbol={currencySymbol}
                  defaultCostPerKg={defaultCostPerKg}
                />
              );
            })}

            {/* Auto Queue has no concrete model yet. Its promotion applies the
                target printer model's saved profile, so these controls would
                imply one answer can safely describe a mixed-model fleet. */}
            {!isAutoMode && (mode === 'reprint' || effectivePrinterCount > 0) && (
              <PrintOptionsPanel options={printOptions} onChange={(o) => { touchedOptionsRef.current = true; setPrintOptions(o); }} defaultExpanded={!!initialSelectedPrinterIds?.length} showDualNozzleOptions={showDualNozzleOptions} autoCaps={autoCaps} timelapseBlockers={timelapseBlockers} selectedPrinterCount={selectedPrinters.length} timelapseLowSpace={timelapseLowSpace} canChooseTimelapseStorage={canChooseTimelapseStorage} onFreeTimelapseSpace={(id) => freeTimelapseSpace.mutate(id)} freeingTimelapseSpace={freeTimelapseSpace.isPending} />
            )}

            {/* Swap-mode macros apply only to a known, selected printer. */}
            {!isAutoMode && !swapCompatible && selectedPrinters.some(id => printers?.find(p => p.id === id)?.swap_mode_enabled) && (
              <SwapMacrosPanel options={swapMacros} onChange={(o) => { touchedOptionsRef.current = true; setSwapMacros(o); }} />
            )}

            {/* Event macros also come from the selected printer model's profile
                when Auto Queue promotes the item. */}
            {!isAutoMode && (
              <EventMacrosPanel
                macros={applicableMacros}
                selectedIds={selectedMacroIds}
                onChange={setSelectedMacroIds}
              />
            )}


            {/* Quantity (batch) - not for edit mode */}
            {mode !== 'edit-queue-item' && mode !== 'edit-auto-item' && (effectivePrinterCount > 0 || isAutoMode) && (
              <div className="mb-4 flex items-center justify-between bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg p-3">
                <div>
                  <div className="text-sm text-white font-medium">{t('printModal.quantity')}</div>
                  <div className="text-xs text-bambu-gray">{t('printModal.quantityHint')}</div>
                </div>
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    onClick={() => { setQuantity(q => Math.max(1, q - 1)); setPlateQuantities({}); }}
                    disabled={quantity <= 1}
                    className="w-8 h-8 rounded bg-bambu-dark border border-bambu-dark-tertiary text-white hover:border-bambu-green disabled:opacity-40"
                  >−</button>
                  <input
                    type="number"
                    min={1}
                    max={999}
                    value={quantity}
                    onChange={(e) => {
                      const v = parseInt(e.target.value, 10);
                      if (Number.isFinite(v)) { setQuantity(Math.min(999, Math.max(1, v))); setPlateQuantities({}); }
                    }}
                    aria-label={t('printModal.quantity')}
                    className="w-14 text-center bg-bambu-dark border border-bambu-dark-tertiary rounded text-white py-1"
                  />
                  <button
                    type="button"
                    onClick={() => { setQuantity(q => Math.min(999, q + 1)); setPlateQuantities({}); }}
                    disabled={quantity >= 999}
                    className="w-8 h-8 rounded bg-bambu-dark border border-bambu-dark-tertiary text-white hover:border-bambu-green disabled:opacity-40"
                  >+</button>
                </div>
              </div>
            )}

            {/* What the number means on several printers, and what will be printed (spec 2026-09-11 §5). */}
            {mode !== 'edit-queue-item' && mode !== 'edit-auto-item' && !isAutoMode && selectedPrinters.length > 1 && (
              <div className="-mt-2 mb-4 px-3 space-y-1.5">
                <div
                  data-testid="quantity-mode-toggle"
                  role="group"
                  aria-label={t('printModal.quantityMode.label')}
                  className="inline-flex rounded-md border border-bambu-dark-tertiary overflow-hidden text-xs"
                >
                  {(['perPrinter', 'total'] as const).map((m) => (
                    <button
                      key={m}
                      type="button"
                      data-testid={`quantity-mode-${m}`}
                      aria-pressed={quantityMode === m}
                      onClick={() => setQuantityMode(m)}
                      className={`px-3 py-1 transition-colors ${
                        quantityMode === m
                          ? 'bg-bambu-green/20 text-bambu-green'
                          : 'bg-bambu-dark text-bambu-gray hover:text-white'
                      }`}
                    >
                      {t(`printModal.quantityMode.${m}`)}
                    </button>
                  ))}
                </div>
                {/* One line per plate in total mode, so the whole plan is one
                    element (`quantity-plan`) and each line its own. */}
                <div data-testid="quantity-plan" className="space-y-1.5">
                  {planLines.map((line, i) => (
                    <div key={i} data-testid={`quantity-plan-${i}`} className="text-xs text-bambu-gray">
                      {line}
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Schedule options - only for queue modes */}
            {mode !== 'reprint' && (
              <ScheduleOptionsPanel
                options={scheduleOptions}
                onChange={setScheduleOptions}
                dateFormat={settings?.date_format || 'system'}
                timeFormat={settings?.time_format || 'system'}
                canControlPrinter={hasPermission('printers:control')}
                showRunNext={mode === 'add-to-queue' && !isAutoMode && hasPermission('queue:reorder')}
              />
            )}

            {/* Error message */}
            {updateQueueMutation.isError && (
              <div className="mb-4 p-3 bg-red-100 dark:bg-red-500/20 border border-red-500/50 rounded-lg text-sm text-red-700 dark:text-red-400">
                {(updateQueueMutation.error as Error)?.message || t('printModal.failedToComplete')}
              </div>
            )}

            {/* Actions */}
            <div className={`flex gap-3 ${mode === 'reprint' ? '' : 'pt-2'}`}>
              <Button type="button" variant="secondary" onClick={onClose} className="flex-1" disabled={isSubmitting}>
                {t('printModal.cancel')}
              </Button>
              {/* ⚠️ `orderAnswerPending` is the same bounded "not yet" the
                  self-submit waits on. Without it the operator can beat their
                  own dialog: the field is not on screen yet, so the print goes
                  out under no order and nothing says it could have had one. */}
              <Button
                type="submit"
                disabled={!canSubmit || orderAnswerPending}
                title={orderAnswerPending ? t('orderFiling.loading') : undefined}
                className="flex-1"
              >
                {isPending ? (
                  <>
                    <Loader2 className="w-4 h-4 animate-spin" />
                    {modalConfig.loadingText}
                  </>
                ) : (
                  <>
                    <SubmitIcon className="w-4 h-4" />
                    {modalConfig.submitText}
                  </>
                )}
              </Button>
            </div>
          </form>
        </div>
      </Modal>

      {filamentWarningItems && filamentWarningItems.length > 0 && (
        <ConfirmModal
          title={t('printModal.insufficientFilamentTitle')}
          message={filamentWarningMessage}
          confirmText={t('printModal.printAnyway')}
          cancelText={t('common.cancel')}
          variant="warning"
          onConfirm={() => {
            setFilamentWarningItems(null);
            void handleSubmit(undefined, { skipFilamentCheck: true });
          }}
          onCancel={() => setFilamentWarningItems(null)}
        />
      )}
    </>
  );
}

// Re-export types for convenience
export type { PrintModalAnswer, PrintModalMode, PrintModalProps } from './types';
