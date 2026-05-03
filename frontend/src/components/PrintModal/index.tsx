import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertCircle, AlertTriangle, Calendar, Loader2, Pencil, Printer, X } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import type {
  AutoQueueItemCreate,
  PrintQueueItemCreate,
  PrintQueueItemUpdate,
  SpoolAssignment,
} from '../../api/client';
import { api } from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';
import { Card, CardContent } from '../Card';
import { Button } from '../Button';
import { ConfirmModal } from '../ConfirmModal';
import { useToast } from '../../contexts/ToastContext';
import { buildLoadedFilaments, useFilamentMapping } from '../../hooks/useFilamentMapping';
import { useMultiPrinterFilamentMapping, type PerPrinterConfig } from '../../hooks/useMultiPrinterFilamentMapping';
import { getCurrencySymbol } from '../../utils/currency';
import { toDateTimeLocalValue, parseUTCDate } from '../../utils/date';
import { getGlobalTrayId, isPlaceholderDate } from '../../utils/amsHelpers';
import { AutoModeOptions } from './AutoModeOptions';
import { FilamentMapping } from './FilamentMapping';
import { PlateSelector } from './PlateSelector';
import { PrinterSelector } from './PrinterSelector';
import { PrintOptionsPanel } from './PrintOptions';
import { ScheduleOptionsPanel } from './ScheduleOptions';
import { SwapMacrosPanel } from './SwapMacros';
import type {
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
 * Unified PrintModal component that handles three modes:
 * - 'reprint': Immediate print from archive or library file (supports multi-printer)
 * - 'add-to-queue': Schedule print to queue from archive or library file (supports multi-printer)
 * - 'edit-queue-item': Edit existing queue item (supports multi-printer)
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
  initialSelectedPrinterIds,
  onClose,
  onSuccess,
  projectId,
  cleanupLibraryAfterDispatch,
  initialDispatchMode,
  lockDispatchMode,
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
    return [];
  });

  // Multi-select plates: in add-to-queue mode users can pick a subset of plates
  const [selectedPlates, setSelectedPlates] = useState<Set<number>>(() => {
    if (mode === 'edit-queue-item' && queueItem?.plate_id != null) {
      return new Set([queueItem.plate_id]);
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
        mesh_mode_fast_check: queueItem.mesh_mode_fast_check ?? DEFAULT_PRINT_OPTIONS.mesh_mode_fast_check,
        gcode_injection: queueItem.gcode_injection ?? DEFAULT_PRINT_OPTIONS.gcode_injection,
      };
    }
    return DEFAULT_PRINT_OPTIONS;
  });

  const [swapMacros, setSwapMacros] = useState<SwapMacrosOptions>(() => {
    if (mode === 'edit-queue-item' && queueItem) {
      const execute = queueItem.execute_swap_macros ?? false;
      const storedEvents = (queueItem.swap_macro_events ?? null) as SwapMacroEvent[] | null;
      return {
        execute,
        events: storedEvents ?? (execute ? [...SWAP_MACRO_EVENTS] : []),
      };
    }
    return DEFAULT_SWAP_MACROS_OPTIONS;
  });

  const [scheduleOptions, setScheduleOptions] = useState<ScheduleOptions>(() => {
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
      };
    }
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
  const [quantity, setQuantity] = useState<number>(1);

  // Dispatch mode: 'specific' = pick exact printer(s); 'auto' = route via auto-queue.
  // Only meaningful for add-to-queue mode (reprint is always specific, edit-queue-item
  // is already bound to a per-printer queue row).
  const [dispatchMode, setDispatchMode] = useState<'specific' | 'auto'>(initialDispatchMode ?? 'specific');
  const [autoModeOptions, setAutoModeOptions] = useState<AutoModeOptionsState>(DEFAULT_AUTO_MODE_OPTIONS);
  const isAutoMode = mode === 'add-to-queue' && dispatchMode === 'auto';

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

  const { data: printers, isLoading: loadingPrinters } = useQuery({
    queryKey: ['printers'],
    queryFn: api.getPrinters,
  });

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
  const { data: libraryPlatesData } = useQuery({
    queryKey: ['library-file-plates', libraryFileId],
    queryFn: () => api.getLibraryFilePlates(libraryFileId!),
    enabled: isLibraryFile && !!libraryFileId,
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
  const { data: libraryFilamentReqs } = useQuery({
    queryKey: ['library-file-filaments', libraryFileId, selectedPlate],
    queryFn: () => api.getLibraryFileFilamentRequirements(libraryFileId!, selectedPlate ?? undefined),
    enabled: isLibraryFile && !!libraryFileId && (selectedPlate !== null || !platesData?.is_multi_plate),
  });

  // Track if archive data couldn't be loaded (archive deleted or file missing)
  const archiveDataMissing = !isLibraryFile && (archivePlatesError || archiveFilamentReqsError);

  // Combine filament requirements from either source
  const effectiveFilamentReqs = isLibraryFile ? libraryFilamentReqs : archiveFilamentReqs;
  const selectedPlateName = useMemo(() => {
    if (selectedPlate === null || !platesData?.plates?.length) {
      return undefined;
    }
    return platesData.plates.find((plate) => plate.index === selectedPlate)?.name || undefined;
  }, [platesData, selectedPlate]);

  // Only fetch printer status when single printer selected (for filament mapping)
  const { data: printerStatus } = useQuery({
    queryKey: ['printer-status', effectivePrinterId],
    queryFn: () => api.getPrinterStatus(effectivePrinterId!),
    enabled: !!effectivePrinterId,
  });

  // Get AMS mapping from hook (only when single printer selected)
  const { amsMapping } = useFilamentMapping(effectiveFilamentReqs, printerStatus, manualMappings);

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

  // Auto-select first printer when only one available
  useEffect(() => {
    // Skip auto-select for edit mode (already initialized from queueItem)
    if (mode === 'edit-queue-item') return;
    const activePrinters = printers?.filter(p => p.is_active) || [];
    if (activePrinters.length === 1 && selectedPrinters.length === 0) {
      setSelectedPrinters([activePrinters[0].id]);
    }
  }, [mode, printers, selectedPrinters.length]);

  // Clear manual mappings and per-printer configs when printer or plate changes
  useEffect(() => {
    if (mode === 'edit-queue-item') {
      // For edit mode, clear mappings if printer selection or plate changed from initial
      const printersChanged = JSON.stringify(selectedPrinters.sort()) !== JSON.stringify(initialPrinterIds.sort());
      if (printersChanged || selectedPlate !== initialPlateId) {
        setManualMappings({});
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

  const handleSubmit = async (e?: React.FormEvent, options?: { skipFilamentCheck?: boolean }) => {
    e?.preventDefault();

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
          project_id: projectId,
          target_model: autoModeOptions.target_model ?? undefined,
          target_location: autoModeOptions.target_location ?? undefined,
          force_color_match: autoModeOptions.force_color_match,
          plate_ids: platesToQueue.length > 1 ? platesToQueue : undefined,
          plate_id: platesToQueue.length === 1 ? platesToQueue[0] : null,
          ...printOptions,
          execute_swap_macros: !swapCompatible && swapMacros.execute && swapMacros.events.length > 0,
          swap_macro_events:
            !swapCompatible && swapMacros.execute && swapMacros.events.length > 0 ? swapMacros.events : null,
          scheduled_time:
            scheduleOptions.scheduleType === 'scheduled' && scheduleOptions.scheduledTime
              ? new Date(scheduleOptions.scheduledTime).toISOString()
              : undefined,
          manual_start: scheduleOptions.scheduleType === 'manual',
          auto_off_after: scheduleOptions.autoOffAfter,
          quantity,
        };
        await api.addToAutoQueue(payload);
        showToast(quantity > 1 ? t('queue.itemsQueued', { count: quantity }) : t('queue.printQueued'));
        queryClient.invalidateQueries({ queryKey: ['auto-queue'] });
        queryClient.invalidateQueries({ queryKey: ['queue'] });
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
      const filamentReqs = effectiveFilamentReqs?.filaments ?? [];

      if (filamentReqs.length > 0 && spoolAssignmentsByPrinter.size > 0) {
        const getRemainingWeight = (labelWeight: number, weightUsed: number) => {
          if (!Number.isFinite(labelWeight) || labelWeight <= 0) return null;
          if (!Number.isFinite(weightUsed) || weightUsed < 0) return null;
          return Math.max(0, labelWeight - weightUsed);
        };

        for (const printerId of selectedPrinters) {
          const printerMapping = selectedPrinters.length > 1
            ? multiPrinterMapping.getFinalMapping(printerId)
            : amsMapping;
          if (!printerMapping) continue;

          const printerStatusForWarning = selectedPrinters.length > 1
            ? multiPrinterMapping.printerResults.find((result) => result.printerId === printerId)?.status
            : printerStatus;

          const loadedFilaments = buildLoadedFilaments(printerStatusForWarning);
          const slotLabelByTray = new Map(loadedFilaments.map((f) => [f.globalTrayId, f.label]));
          const assignments = spoolAssignmentsByPrinter.get(printerId);
          const printerName = printers?.find((p) => p.id === printerId)?.name ?? `Printer ${printerId}`;

          if (!assignments) continue;

          filamentReqs.forEach((req) => {
            if (!req.slot_id || req.slot_id <= 0) return;
            const globalTrayId = printerMapping[req.slot_id - 1];
            if (!Number.isFinite(globalTrayId) || globalTrayId < 0) return;

            const assignment = assignments.get(globalTrayId);
            const spool = assignment?.spool;
            if (!spool) return;

            const remainingGrams = getRemainingWeight(spool.label_weight, spool.weight_used);
            if (remainingGrams === null) return;
            if (remainingGrams >= req.used_grams) return;

            warningItems.push({
              printerName,
              slotLabel: slotLabelByTray.get(globalTrayId) ?? `Slot ${req.slot_id}`,
              requiredGrams: req.used_grams,
              remainingGrams,
            });
          });
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

    const results: { success: number; failed: number; errors: string[] } = {
      success: 0,
      failed: 0,
      errors: [],
    };

    // Get mapping for a specific printer (per-printer override or default)
    const getMappingForPrinter = (printerId: number): number[] | undefined => {
      // For multi-printer selection, check if this printer has an override
      if (selectedPrinters.length > 1) {
        const printerConfig = perPrinterConfigs[printerId];
        if (printerConfig && !printerConfig.useDefault) {
          return multiPrinterMapping.getFinalMapping(printerId);
        }
      }
      return amsMapping;
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
    const getQueueData = (printerId: number, plateOverride?: number | null): PrintQueueItemCreate => ({
      queue_id: printerId,  // queue_id == printer_id (always per-printer queue)
      // Use library_file_id for library files, archive_id for archives
      archive_id: isLibraryFile ? undefined : archiveId,
      library_file_id: isLibraryFile ? libraryFileId : undefined,
      auto_off_after: scheduleOptions.autoOffAfter,
      manual_start: scheduleOptions.scheduleType === 'manual',
      ams_mapping: getMappingForPrinter(printerId),
      plate_id: plateOverride !== undefined ? plateOverride : selectedPlate,
      scheduled_time: scheduleOptions.scheduleType === 'scheduled' && scheduleOptions.scheduledTime
        ? new Date(scheduleOptions.scheduledTime).toISOString()
        : undefined,
      ...printOptions,
      ...getSwapPayloadForPrinter(printerId),
      quantity: mode === 'edit-queue-item' ? 1 : quantity,
      project_id: projectId,
    });

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
            const printerMapping = getMappingForPrinter(printerId);
            const swapPayload = getSwapPayloadForPrinter(printerId);
            if (isLibraryFile) {
              await api.printLibraryFile(libraryFileId!, printerId, {
                plate_id: selectedPlate ?? undefined,
                plate_name: selectedPlateName,
                ams_mapping: printerMapping,
                ...printOptions,
                ...swapPayload,
                quantity,
                project_id: projectId,
                cleanup_library_after_dispatch: cleanupLibraryAfterDispatch,
              });
            } else {
              // project_id is intentionally omitted here: reprintArchive targets an existing
              // archive that already carries its own project association from the original print.
              await api.reprintArchive(archiveId!, printerId, {
                plate_id: selectedPlate ?? undefined,
                plate_name: selectedPlateName,
                ams_mapping: printerMapping,
                ...printOptions,
                ...swapPayload,
                quantity,
              });
            }
          } else if (mode === 'edit-queue-item' && progressCounter === 1) {
            // Edit mode - update the original queue item for the first entry
            const printerMapping = getMappingForPrinter(printerId);
            const updateData: PrintQueueItemUpdate = {
              queue_id: printerId,  // queue_id == printer_id
              auto_off_after: scheduleOptions.autoOffAfter,
              manual_start: scheduleOptions.scheduleType === 'manual',
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
            await addToQueueMutation.mutateAsync(getQueueData(printerId, plateId));
          }
          results.success++;
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
      if (mode !== 'reprint') {
        if (mode === 'edit-queue-item') {
          showToast(t('printModal.queueItemUpdated'));
        } else if (results.success === 1) {
          showToast(t('queue.printQueued'));
        } else {
          showToast(t('queue.itemsQueued', { count: results.success }));
        }
      }
      queryClient.invalidateQueries({ queryKey: ['queue'] });
      onSuccess?.();
      onClose();
    } else if (results.success === 0) {
      showToast(t('printModal.failedPrefix', { error: results.errors[0] }), 'error');
    } else {
      showToast(t('printModal.partialSuccess', { success: results.success, failed: results.failed }), 'error');
      queryClient.invalidateQueries({ queryKey: ['queue'] });
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

    return true;
  }, [isAutoMode, selectedPrinters.length, isMultiPlate, selectedPlates.size, isPending]);

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

  return (
    <div
      className="fixed inset-0 bg-black/70 flex items-center justify-center z-50 p-4"
      onClick={isSubmitting ? undefined : onClose}
    >
      <Card
        className="w-full max-w-2xl max-h-[90vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <CardContent className={mode === 'reprint' ? '' : 'p-0'}>
          {/* Header */}
          <div
            className={`flex items-center justify-between ${
              mode === 'reprint' ? 'mb-4' : 'p-4 border-b border-bambu-dark-tertiary'
            }`}
          >
            <div className="flex items-center gap-2">
              <TitleIcon className="w-5 h-5 text-bambu-green" />
              <h2 className="text-lg font-semibold text-white">{modalConfig.title}</h2>
            </div>
            <Button variant="ghost" size="sm" onClick={onClose} disabled={isSubmitting}>
              <X className="w-5 h-5" />
            </Button>
          </div>

          <form onSubmit={handleSubmit} className={mode === 'reprint' ? '' : 'p-4 space-y-4'}>
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
                      ? 'bg-bambu-green text-bambu-dark font-medium'
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
                      ? 'bg-bambu-green text-bambu-dark font-medium'
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

            {/* Plate selection - first so users know filament requirements before selecting printers */}
            <PlateSelector
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
            />

            {/* Auto-distribute mode controls — replaces PrinterSelector */}
            {isAutoMode && (
              <AutoModeOptions
                options={autoModeOptions}
                onChange={setAutoModeOptions}
                printers={printers}
                slicedForModel={slicedForModel}
              />
            )}

            {/* Printer selection with per-printer mapping - hidden when printer is pre-selected via props */}
            {!isAutoMode && !initialSelectedPrinterIds?.length && (
              <PrinterSelector
                printers={printers || []}
                selectedPrinterIds={selectedPrinters}
                onMultiSelect={setSelectedPrinters}
                isLoading={loadingPrinters}
                allowMultiple={true}
                showInactive={mode === 'edit-queue-item'}
                disableBusy={mode === 'reprint'}
                printerMappingResults={multiPrinterMapping.printerResults}
                filamentReqs={effectiveFilamentReqs}
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
                  <div className="p-3 mb-2 bg-yellow-500/10 border border-yellow-500/30 rounded-lg flex items-center gap-2">
                    <AlertTriangle className="w-4 h-4 text-yellow-400 flex-shrink-0" />
                    <span className="text-sm text-yellow-400">
                      {t('printModal.slicedForWarning', { slicedModel: slicedForModel, printerModel: selectedPrinter.model })}
                    </span>
                  </div>
                );
              }
              return null;
            })()}

            {/* Warning when archive data couldn't be loaded */}
            {archiveDataMissing && (
              <div className="flex items-start gap-2 p-3 mb-2 bg-orange-500/10 border border-orange-500/30 rounded-lg text-sm">
                <AlertCircle className="w-4 h-4 text-orange-400 mt-0.5 flex-shrink-0" />
                <p className="text-orange-400">
                  {t('printModal.archiveDataUnavailable')}
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

            {/* Print options */}
            {(mode === 'reprint' || effectivePrinterCount > 0 || isAutoMode) && (
              <PrintOptionsPanel options={printOptions} onChange={setPrintOptions} defaultExpanded={!!initialSelectedPrinterIds?.length} />
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
              <SwapMacrosPanel options={swapMacros} onChange={setSwapMacros} />
            )}

            {/* Quantity (batch) - not for edit mode */}
            {mode !== 'edit-queue-item' && (effectivePrinterCount > 0 || isAutoMode) && (
              <div className="mb-4 flex items-center justify-between bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg p-3">
                <div>
                  <div className="text-sm text-white font-medium">{t('printModal.quantity')}</div>
                  <div className="text-xs text-bambu-gray">{t('printModal.quantityHint')}</div>
                </div>
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    onClick={() => setQuantity(q => Math.max(1, q - 1))}
                    disabled={quantity <= 1}
                    className="w-8 h-8 rounded bg-bambu-dark border border-bambu-dark-tertiary text-white hover:border-bambu-green disabled:opacity-40"
                  >−</button>
                  <input
                    type="number"
                    min={1}
                    max={50}
                    value={quantity}
                    onChange={(e) => {
                      const v = parseInt(e.target.value, 10);
                      if (Number.isFinite(v)) setQuantity(Math.min(50, Math.max(1, v)));
                    }}
                    className="w-14 text-center bg-bambu-dark border border-bambu-dark-tertiary rounded text-white py-1"
                  />
                  <button
                    type="button"
                    onClick={() => setQuantity(q => Math.min(50, q + 1))}
                    disabled={quantity >= 50}
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
              <div className="mb-4 p-3 bg-red-500/20 border border-red-500/50 rounded-lg text-sm text-red-400">
                {(updateQueueMutation.error as Error)?.message || t('printModal.failedToComplete')}
              </div>
            )}

            {/* Actions */}
            <div className={`flex gap-3 ${mode === 'reprint' ? '' : 'pt-2'}`}>
              <Button type="button" variant="secondary" onClick={onClose} className="flex-1" disabled={isSubmitting}>
                {t('printModal.cancel')}
              </Button>
              <Button
                type="submit"
                disabled={!canSubmit}
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
export type { PrintModalMode, PrintModalProps } from './types';
