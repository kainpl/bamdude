import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertCircle, AlertTriangle, Calendar, Loader2, Pencil, Printer, X } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
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
import { Card, CardContent } from '../Card';
import { Button } from '../Button';
import { ConfirmModal } from '../ConfirmModal';
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
import { invalidateOrderCandidates } from '../../utils/queryInvalidation';
import { getCurrencySymbol } from '../../utils/currency';
import { toDateTimeLocalValue, parseUTCDate } from '../../utils/date';
import { getBedTypeInfo } from '../../utils/bedType';
import { getGlobalTrayId, isPlaceholderDate } from '../../utils/amsHelpers';
import { AutoModeOptions } from './AutoModeOptions';
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
  ScheduleOptions,
  ScheduleType,
  SwapMacroEvent,
  SwapMacrosOptions,
} from './types';
import type { AutoModeOptionsState } from './types';
import {
  DEFAULT_AUTO_MODE_OPTIONS,
  DEFAULT_PRINT_OPTIONS,
  DEFAULT_SCHEDULE_OPTIONS,
  DEFAULT_SWAP_MACROS_OPTIONS,
  SWAP_MACRO_EVENTS,
} from './types';

/**
 * Unified PrintModal component that handles four modes:
 * - 'reprint': Immediate print from archive or library file (supports multi-printer)
 * - 'add-to-queue': Schedule print to queue from archive or library file (supports multi-printer)
 * - 'edit-queue-item': Edit an existing per-printer queue item (supports multi-printer)
 * - 'edit-auto-item': Edit an existing auto-queue row (no printer; the auto-queue routes it later)
 *
 * Both archiveId and libraryFileId are supported. Library files can be printed immediately
 * or added to queue (archive is created at print start time, not when queued).
 */
export function PrintModal({
  mode,
  archiveId,
  libraryFileId,
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
  onAnswered,
  onAutoSubmitRefused,
  onClose,
  onSuccess,
  projectId,
  projectLineId,
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

  // Determine if we're printing a library file
  const isLibraryFile = !!libraryFileId && !archiveId;

  type FilamentWarningItem = {
    printerName: string;
    slotLabel: string;
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
    if (mode === 'edit-queue-item' && queueItem?.ams_mapping && Array.isArray(queueItem.ams_mapping)) {
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
  const quantityForPlate = (plateIndex: number | null | undefined) =>
    (plateIndex != null ? plateQuantities[plateIndex] : undefined) ?? quantity;

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
      };
    }
    if (seededAnswer) return seededAnswer.autoModeOptions;
    return DEFAULT_AUTO_MODE_OPTIONS;
  });
  // edit-auto-item is auto mode by definition: the row belongs to the router.
  const isAutoMode = (mode === 'add-to-queue' && dispatchMode === 'auto') || mode === 'edit-auto-item';

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
   */
  const reportAnswer = useCallback(() => {
    onAnswered?.({
      selectedPrinterIds: [...selectedPrinters],
      dispatchMode,
      autoModeOptions,
      scheduleOptions,
      quantity,
      printOptions,
      swapMacros,
      selectedMacroIds: [...selectedMacroIds],
    });
  }, [
    onAnswered,
    selectedPrinters,
    dispatchMode,
    autoModeOptions,
    scheduleOptions,
    quantity,
    printOptions,
    swapMacros,
    selectedMacroIds,
  ]);

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
    enabled: !!archiveId && !isLibraryFile,
  });

  // Fetch library file details to get sliced_for_model
  const { data: libraryFileDetails } = useQuery({
    queryKey: ['library-file', libraryFileId],
    queryFn: () => api.getLibraryFile(libraryFileId!),
    enabled: isLibraryFile && !!libraryFileId,
  });

  // Get sliced_for_model from archive or library file
  const slicedForModel = archiveDetails?.sliced_for_model || libraryFileDetails?.sliced_for_model || null;

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
  const swapCompatible = archiveDetails?.swap_compatible || libraryFileDetails?.swap_compatible || false;

  // Fetch plates for archives
  const { data: archivePlatesData, isError: archivePlatesError } = useQuery({
    queryKey: ['archive-plates', archiveId],
    queryFn: () => api.getArchivePlates(archiveId!),
    enabled: !!archiveId && !isLibraryFile,
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

  // Combine plates data from either source
  const platesData = isLibraryFile ? libraryPlatesData : archivePlatesData;

  // Fetch filament requirements for archives
  const { data: archiveFilamentReqs, isError: archiveFilamentReqsError } = useQuery({
    queryKey: ['archive-filaments', archiveId, selectedPlate],
    queryFn: () => api.getArchiveFilamentRequirements(archiveId!, selectedPlate ?? undefined),
    enabled: !!archiveId && !isLibraryFile && (selectedPlate !== null || !platesData?.is_multi_plate),
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

  // Track if archive data couldn't be loaded (archive deleted or file missing)
  const archiveDataMissing = !isLibraryFile && (archivePlatesError || archiveFilamentReqsError);

  // Combine filament requirements from either source
  const effectiveFilamentReqs = isLibraryFile ? libraryFilamentReqs : archiveFilamentReqs;
  // Whether that one query gave up. Only the self-submit below reads it: a
  // silent run must be able to tell "this plate needs no filament" from "we do
  // not know yet / we never will", which `effectiveFilamentReqs` alone cannot.
  const effectiveFilamentReqsError = isLibraryFile ? libraryFilamentReqsError : archiveFilamentReqsError;
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
  const { amsMapping } = useFilamentMapping(effectiveFilamentReqs, printerStatus, manualMappings);

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

  const perPlateReqQueries = useQueries({
    queries: (isMultiPlateSelection ? selectedPlateIds : []).map((plateId) => ({
      queryKey: isLibraryFile
        ? ['library-file-filaments', libraryFileId, plateId]
        : ['archive-filaments', archiveId, plateId],
      queryFn: () =>
        isLibraryFile
          ? api.getLibraryFileFilamentRequirements(libraryFileId!, plateId)
          : api.getArchiveFilamentRequirements(archiveId!, plateId),
      enabled: isLibraryFile ? !!libraryFileId : !!archiveId,
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
      if (data) byPlate.set(plateId, data);
    });
    return byPlate;
    // Keyed on each query's last update stamp, not on the query objects (fresh every
    // render) and not on a spread of their data (a dep array whose *length* changes
    // with the plate count, which React treats as always-changed and warns about).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedPlateIds, perPlateReqQueries.map((q) => q.dataUpdatedAt).join('|')]);

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
            settings?.prefer_lowest_filament ?? false,
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
    effectiveFilamentReqs,
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
  const [chosenOrderFiling, setChosenOrderFiling] = useState<OrderFilingValue | null>(null);
  // Once the operator has answered, the answer stands. Without this, switching
  // plates — which legitimately re-asks the server — would quietly overwrite a
  // deliberate "Without an order" with whatever the next plate needs.
  const [orderFilingTouched, setOrderFilingTouched] = useState(false);
  // ⚠️ `projects:read` is part of ASKING, not just of answering. The candidates
  // endpoint requires it beside the library read (it names orders and how much
  // of them is left), so without it the request is a guaranteed 403 — a round
  // trip whose only visible effect is a submit button disabled while it happens
  // and a field that never appears. A viewer who may print but not read orders
  // gets exactly the dialog they had before this feature existed.
  const asksAboutOrder =
    isLibraryFile &&
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

  // ⚠️ **The proposal is DERIVED, never synced into state by an effect.** The
  // silent members of a grouped run submit from an effect of their own, and
  // effects in one commit run in declaration order: a `setState` here would
  // still be unrendered when that one fires, so the member would send the
  // filing from BEFORE the candidates arrived — the leader beside it filing
  // correctly and nothing on screen to say the rest did not.
  // The first candidate that still NEEDS prints. The list already sorts those
  // first, but the rule is the dialog's own: an order that is already covered
  // is never proposed by itself, only chosen.
  const proposedOrderFiling = useMemo<OrderFilingValue | null>(() => {
    const needy = orderCandidates?.find((c) => c.outstanding_prints > 0);
    return needy ? { projectId: needy.project_id, projectLineId: needy.project_line_id } : null;
  }, [orderCandidates]);

  // ⚠️ **A choice the current plate does not offer is not an answer about this
  // plate.** Switching plates re-asks, and the new list need not contain the
  // order the operator picked for the old one — the `<select>` then falls back
  // to showing «Without an order» while the payload would still carry the old
  // order and line, and when the new plate has no candidates at all the field
  // is not even on screen to be doubted. So a stale choice is dropped and the
  // untouched rule takes over. A deliberate «Without an order» (touched, null)
  // is not stale — it is an answer about every plate, and it survives.
  const orderFiling = useMemo<OrderFilingValue | null>(() => {
    if (!orderFilingTouched) return proposedOrderFiling;
    if (chosenOrderFiling === null) return null;
    const stillOffered = orderCandidates?.some(
      (c) =>
        c.project_id === chosenOrderFiling.projectId &&
        c.project_line_id === chosenOrderFiling.projectLineId,
    );
    return stillOffered ? chosenOrderFiling : proposedOrderFiling;
  }, [orderFilingTouched, chosenOrderFiling, orderCandidates, proposedOrderFiling]);

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
  const filedProjectId = asksAboutOrder ? orderFiling?.projectId : projectId;
  // The line as answered: by the dialog's own field, or by the caller.
  const answeredProjectLineId = asksAboutOrder
    ? (orderFiling?.projectLineId ?? null)
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
      const printersChanged = JSON.stringify(selectedPrinters.sort()) !== JSON.stringify(initialPrinterIds.sort());
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

  // Close on Escape key
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !isSubmitting) onClose();
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [onClose, isSubmitting]);

  const isMultiPlate = platesData?.is_multi_plate ?? false;
  const plates = platesData?.plates ?? [];

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
      t('printModal.insufficientFilamentLine', {
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

  // Update queue item mutation
  const updateQueueMutation = useMutation({
    mutationFn: (data: PrintQueueItemUpdate) => api.updateQueueItem(queueItem!.id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['queue'] });
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

  const handleSubmit = async (e?: React.FormEvent, options?: { skipFilamentCheck?: boolean }) => {
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

    // Edit of a pending auto-queue row (or its whole batch): one PUT, no
    // dispatch. Position is deliberately not sent — the reorder flow owns it.
    if (mode === 'edit-auto-item' && autoQueueItem) {
      setIsSubmitting(true);
      try {
        const payload: AutoQueueItemUpdate = {
          target_model: autoModeOptions.target_model ?? null,
          target_location_id: autoModeOptions.target_location_id ?? null,
          force_color_match: autoModeOptions.force_color_match,
          bed_levelling: printOptions.bed_levelling,
          flow_cali: printOptions.flow_cali,
          layer_inspect: printOptions.layer_inspect,
          timelapse: printOptions.timelapse,
          timelapse_storage: printOptions.timelapse_storage,
          mesh_mode_fast_check: printOptions.mesh_mode_fast_check,
          execute_swap_macros: !swapCompatible && swapMacros.execute && swapMacros.events.length > 0,
          swap_macro_events:
            !swapCompatible && swapMacros.execute && swapMacros.events.length > 0 ? swapMacros.events : null,
          selected_macro_ids: selectedMacroIds,
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
        const platesToQueue =
          selectedPlates.size > 0 ? [...selectedPlates] : selectedPlate !== null ? [selectedPlate] : [];
        const payload: AutoQueueItemCreate = {
          archive_id: isLibraryFile ? undefined : archiveId,
          library_file_id: isLibraryFile ? libraryFileId : undefined,
          project_id: filedProjectId,
          project_line_id: filedProjectLineId,
          target_model: autoModeOptions.target_model ?? undefined,
          target_location_id: autoModeOptions.target_location_id ?? undefined,
          force_color_match: autoModeOptions.force_color_match,
          plate_ids: platesToQueue.length > 1 ? platesToQueue : undefined,
          plate_id: platesToQueue.length === 1 ? platesToQueue[0] : null,
          ...printOptions,
          execute_swap_macros: !swapCompatible && swapMacros.execute && swapMacros.events.length > 0,
          swap_macro_events:
            !swapCompatible && swapMacros.execute && swapMacros.events.length > 0 ? swapMacros.events : null,
          selected_macro_ids: selectedMacroIds,
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
        persistPreference();
        reportAnswer();
        const queuedCount = platesToQueue.length > 0
          ? platesToQueue.reduce((sum, index) => sum + quantityForPlate(index), 0)
          : quantity;
        if (announces) {
          showToast(queuedCount > 1 ? t('queue.itemsQueued', { count: queuedCount }) : t('queue.printQueued'));
        }
        queryClient.invalidateQueries({ queryKey: ['auto-queue'] });
        queryClient.invalidateQueries({ queryKey: ['queue'] });
        invalidateOrderCandidates(queryClient);
        onSuccess?.();
        onClose();
      } catch (err) {
        showToast(t('printModal.failedPrefix', { error: (err as Error).message }), 'error');
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
          const printerStatusForWarning = selectedPrinters.length > 1
            ? multiPrinterMapping.printerResults.find((result) => result.printerId === printerId)?.status
            : printerStatus;

          const loadedFilaments = buildLoadedFilaments(printerStatusForWarning);
          const slotLabelByTray = new Map(loadedFilaments.map((f) => [f.globalTrayId, f.label]));
          const assignments = spoolAssignmentsByPrinter.get(printerId);
          const printerName = printers?.find((p) => p.id === printerId)?.name ?? `Printer ${printerId}`;

          if (!assignments) continue;

          const gramsByTray = new Map<number, number>();
          for (const job of plateJobs) {
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

          for (const [globalTrayId, requiredGrams] of gramsByTray) {
            const spool = assignments.get(globalTrayId)?.spool;
            if (!spool) continue;

            const remainingGrams = getRemainingWeight(spool.label_weight, spool.weight_used);
            if (remainingGrams === null) continue;
            if (remainingGrams >= requiredGrams) continue;

            warningItems.push({
              printerName,
              slotLabel: slotLabelByTray.get(globalTrayId) ?? `Tray ${globalTrayId}`,
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
    // Calculate total API calls: plates × printers
    const platesToQueue = selectedPlates.size > 1
      ? plates.filter(p => selectedPlates.has(p.index))
      : [null];
    const totalCount = selectedPrinters.length * platesToQueue.length;
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
      selected_macro_ids: selectedMacroIds,
      // Use library_file_id for library files, archive_id for archives
      archive_id: isLibraryFile ? undefined : archiveId,
      library_file_id: isLibraryFile ? libraryFileId : undefined,
      auto_off_after: scheduleOptions.autoOffAfter,
      manual_start: scheduleOptions.scheduleType === 'manual',
      require_previous_success: scheduleOptions.requirePreviousSuccess,
      ams_mapping: getMappingForPrinter(printerId, plateId),
      plate_id: plateId,
      scheduled_time: scheduleOptions.scheduleType === 'scheduled' && scheduleOptions.scheduledTime
        ? new Date(scheduleOptions.scheduledTime).toISOString()
        : undefined,
      ...printOptions,
      ...getSwapPayloadForPrinter(printerId),
      quantity: mode === 'edit-queue-item' ? 1 : quantityForPlate(plateId),
      project_id: filedProjectId,
      project_line_id: filedProjectLineId,
      };
    };

    // Loop through plates × printers
    let progressCounter = 0;
    for (const plate of platesToQueue) {
      const plateId = plate ? plate.index : selectedPlate;

      for (let i = 0; i < selectedPrinters.length; i++) {
        const printerId = selectedPrinters[i];
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
                ...printOptions,
                ...swapPayload,
                selected_macro_ids: selectedMacroIds,
                quantity,
                project_id: filedProjectId,
                project_line_id: filedProjectLineId,
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
                ...printOptions,
                ...swapPayload,
                selected_macro_ids: selectedMacroIds,
                quantity,
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
          // Edit mode replaces one row; everything else writes one per copy.
          results.queued += mode === 'edit-queue-item' ? 1 : quantityForPlate(plateId);
        } catch (error) {
          results.failed++;
          const printerName = printers?.find(p => p.id === printerId)?.name || `Printer ${printerId}`;
          const plateName = plate ? (plate.name || `Plate ${plate.index}`) : '';
          const label = plateName ? `${printerName} (${plateName})` : printerName;
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
      queryClient.invalidateQueries({ queryKey: ['queue'] });
      invalidateOrderCandidates(queryClient);
      onSuccess?.();
      onClose();
    } else if (results.success === 0) {
      showToast(t('printModal.failedPrefix', { error: results.errors[0] }), 'error');
    } else {
      showToast(t('printModal.partialSuccess', { success: results.success, failed: results.failed }), 'error');
      queryClient.invalidateQueries({ queryKey: ['queue'] });
      invalidateOrderCandidates(queryClient);
    }
  };

  const isPending = isSubmitting || updateQueueMutation.isPending;

  const canSubmit = useMemo(() => {
    if (isPending) return false;

    // Auto mode: no specific printer required (router picks one). Plate gate still applies.
    if (isAutoMode) {
      if (isMultiPlate && selectedPlates.size === 0) return false;
      return true;
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
    selectedPrinters.length,
    isMultiPlate,
    selectedPlates.size,
    isPending,
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
      const reqsWillNeverAnswer = perPlateReqsFailed;
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
    printersFetched,
    soleActivePrinterId,
    orderAnswerPending,
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
    const printerCount = selectedPrinters.length;

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
        loadingText: submitProgress.total > 1
          ? t('queue.addingProgress', { current: submitProgress.current, total: submitProgress.total })
          : t('queue.adding'),
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
    isLibraryFile || (isMultiPlate ? selectedPlate !== null : true)
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
  // and `announces` in `handleSubmit` is exactly this condition negated — keep
  // the two in step.
  if (autoSubmitWhenUnambiguous && !autoSubmitRefused) return null;

  return (
    <div
      className="fixed inset-0 bg-black/50 backdrop-blur-sm flex items-center justify-center z-50 p-4"
      onClick={isSubmitting ? undefined : onClose}
    >
      <Card
        className="w-full max-w-2xl max-h-[90vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <CardContent>
          {/* Header */}
          <div
            className={`flex items-center justify-between ${
              mode === 'reprint' ? 'mb-4' : 'mb-4 border-b border-bambu-dark-tertiary'
            }`}
          >
            <div className="flex items-center gap-2">
              <TitleIcon className="w-5 h-5 text-bambu-green" />
              <h2 className="text-lg font-semibold text-white">{modalConfig.title}</h2>
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
            </div>
            <Button variant="ghost" size="sm" onClick={onClose} disabled={isSubmitting}>
              <X className="w-5 h-5" />
            </Button>
          </div>

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
            {asksAboutOrder && (
              <OrderFilingField
                value={orderFiling}
                onChange={(v) => { setOrderFilingTouched(true); setChosenOrderFiling(v); }}
                candidates={orderCandidates}
                loading={orderCandidatesLoading}
              />
            )}

            {/* Auto-distribute mode controls — replaces PrinterSelector */}
            {isAutoMode && (
              <AutoModeOptions
                options={autoModeOptions}
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
                filamentReqs={isMultiPlateSelection ? undefined : effectiveFilamentReqs}
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
                filamentReqs={effectiveFilamentReqs}
                manualMappings={manualMappings}
                onManualMappingChange={setManualMappings}
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

            {/* Print options */}
            {(mode === 'reprint' || effectivePrinterCount > 0 || isAutoMode) && (
              <PrintOptionsPanel options={printOptions} onChange={(o) => { touchedOptionsRef.current = true; setPrintOptions(o); }} defaultExpanded={!!initialSelectedPrinterIds?.length} showDualNozzleOptions={showDualNozzleOptions} autoCaps={autoCaps} timelapseBlockers={timelapseBlockers} selectedPrinterCount={selectedPrinters.length} timelapseLowSpace={timelapseLowSpace} canChooseTimelapseStorage={canChooseTimelapseStorage} onFreeTimelapseSpace={(id) => freeTimelapseSpace.mutate(id)} freeingTimelapseSpace={freeTimelapseSpace.isPending} />
            )}

            {/* Swap-mode macros — only relevant when at least one selected
                printer has swap mode enabled AND the source file does not
                already carry swap macros baked in by third-party tooling
                (swap_compatible flag). In auto mode show whenever the file
                isn't swap-compatible (the scheduler will route to a
                swap-enabled printer if one is needed). */}
            {!swapCompatible && (
              isAutoMode
                ? (printers ?? []).some(p => p.swap_mode_enabled)
                : selectedPrinters.some(id => printers?.find(p => p.id === id)?.swap_mode_enabled)
            ) && (
              <SwapMacrosPanel options={swapMacros} onChange={(o) => { touchedOptionsRef.current = true; setSwapMacros(o); }} />
            )}

            {/* Which of the other macros run for this print. Outside the swap
                condition above on purpose — these have nothing to do with swap
                mode, and the panel hides itself when nothing applies. */}
            <EventMacrosPanel
              macros={applicableMacros}
              selectedIds={selectedMacroIds}
              onChange={setSelectedMacroIds}
            />


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

            {/* Schedule options - only for queue modes */}
            {mode !== 'reprint' && (
              <ScheduleOptionsPanel
                options={scheduleOptions}
                onChange={setScheduleOptions}
                dateFormat={settings?.date_format || 'system'}
                timeFormat={settings?.time_format || 'system'}
                canControlPrinter={hasPermission('printers:control')}
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
        </CardContent>
      </Card>

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
    </div>
  );
}

// Re-export types for convenience
export type { PrintModalAnswer, PrintModalMode, PrintModalProps } from './types';
