import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useQuery } from '@tanstack/react-query';

import { api } from '../../api/client';
import { PrintOptionsPanel } from '../PrintModal/PrintOptions';
import { SwapMacrosPanel } from '../PrintModal/SwapMacros';
import {
  DEFAULT_PRINT_OPTIONS,
  DEFAULT_SWAP_MACROS_OPTIONS,
  SWAP_MACRO_EVENTS,
  type PrintOptions,
  type SwapMacroEvent,
  type SwapMacrosOptions,
} from '../PrintModal/types';
import type {
  BedType,
  CalibCapabilities,
  CalibFilamentIn,
  CaliMethod,
  CaliMode,
  NozzleVolumeType,
  PresetRef,
  Printer,
  PrinterStatus,
  SlicerBundle,
  UnifiedPresetsResponse,
} from '../../api/client';
import {
  BundleStringDropdown,
  matchesOwnerFilter,
  type OwnerFilter,
  PresetDropdown,
  PresetSourceControl,
  TIER_ORDER,
} from '../preset-picker/PresetTripletPicker';
import { BedTypePicker } from '../preset-picker/BedTypePicker';
import { SlicerPicker, type SlicerKind } from '../preset-picker/SlicerPicker';

type PresetSource = 'manual' | 'bundle';

interface Props {
  printerId: number;
  caliMode: CaliMode;
  method: CaliMethod;
  capabilities: CalibCapabilities | undefined;
  onBack: () => void;
  onStart: (preset: {
    nozzle_diameter: number;
    nozzle_volume_type: NozzleVolumeType;
    extruder_id: number;
    filaments: CalibFilamentIn[];
    spec?: Record<string, number | string | boolean>;
    bundle?: { bundle_id: string; printer_name: string; process_name: string; filament_names: string[] };
    printer_preset?: PresetRef;
    process_preset?: PresetRef;
    filament_presets?: PresetRef[];
    slicer?: 'orcaslicer' | 'bambu_studio';
    bed_type?: BedType;
    print_options?: {
      bed_levelling: boolean;
      flow_cali: boolean;
      layer_inspect: boolean;
      timelapse: boolean;
      mesh_mode_fast_check: boolean;
      gcode_injection: boolean;
    };
    swap_macros?: {
      execute: boolean;
      events: Array<'swap_mode_start' | 'swap_mode_change_table'>;
    };
  }) => Promise<void>;
}

function pickDefaultRef(
  data: UnifiedPresetsResponse | undefined,
  slot: 'printer' | 'process' | 'filament',
  ownerFilter: OwnerFilter,
): PresetRef | null {
  if (!data) return null;
  for (const tier of TIER_ORDER) {
    const entries = data[tier][slot].filter((p) => matchesOwnerFilter(p, ownerFilter));
    if (entries.length > 0) {
      return { source: entries[0].source, id: entries[0].id };
    }
  }
  return null;
}

/**
 * BS-style shorthand for a printer model: "A1 mini" → "A1M",
 * "X1 Carbon" → "X1C", "P1S" → "P1S", "H2D" → "H2D". Used to match
 * presets named with the `@BBL <Shorthand>` suffix convention (e.g.
 * "0.20mm Standard @BBL A1M", "Generic PETG @BBL A1M"). One-token
 * models pass through unchanged.
 */
function bsShorthand(model: string): string {
  const tokens = model.toUpperCase().split(/\s+/).filter(Boolean);
  if (tokens.length === 0) return '';
  if (tokens.length === 1) return tokens[0];
  return tokens[0] + tokens.slice(1).map((t) => t[0]).join('');
}

/**
 * Substring-match a preset name against a printer model, accepting
 * both the long form ("Bambu Lab A1 mini 0.4 nozzle") and the
 * `@BBL <Shorthand>` form ("0.20mm Standard @BBL A1M"). When the
 * model isn't known, returns true so unfiltered presets stay visible.
 */
function matchesPrinterModel(presetName: string, model: string | null | undefined): boolean {
  if (!model) return true;
  const normName = presetName.toLowerCase();
  const normModel = model.toLowerCase();
  if (normName.includes(normModel)) return true;
  const stripped = normModel.replace(/^bambu\s+lab\s+/, '').trim();
  if (stripped && normName.includes(stripped)) return true;
  const shorthand = bsShorthand(stripped || normModel);
  if (shorthand && normName.toLowerCase().includes(shorthand.toLowerCase())) return true;
  return false;
}

function filterPresetsByModel(
  data: UnifiedPresetsResponse | undefined,
  model: string | null | undefined,
): UnifiedPresetsResponse | undefined {
  if (!data || !model) return data;
  const filterSlot = (entries: UnifiedPresetsResponse['cloud']['printer']) =>
    entries.filter((p) => matchesPrinterModel(p.name, model));
  return {
    cloud: {
      printer: filterSlot(data.cloud.printer),
      process: filterSlot(data.cloud.process),
      filament: filterSlot(data.cloud.filament),
    },
    local: {
      printer: filterSlot(data.local.printer),
      process: filterSlot(data.local.process),
      filament: filterSlot(data.local.filament),
    },
    standard: {
      printer: filterSlot(data.standard.printer),
      process: filterSlot(data.standard.process),
      filament: filterSlot(data.standard.filament),
    },
    cloud_status: data.cloud_status,
  };
}

function filterBundlesByModel(bundles: SlicerBundle[], model: string | null | undefined): SlicerBundle[] {
  if (!model) return bundles;
  return bundles.filter(
    (b) =>
      b.printer.some((name) => matchesPrinterModel(name, model)) ||
      matchesPrinterModel(b.printer_preset_name, model),
  );
}

interface LoadedSlot {
  ams_id: number;
  slot_id: number;
  tray_id: number;
  filament_id: string;
  filament_setting_id: string | null;
  label: string;
}

interface PerExtruderState {
  selectedSlot: LoadedSlot | null;
  bedTemp: number;
  nozzleTemp: number;
  maxVolSpeed: number;
}

const DEFAULT_PER_EXTRUDER: PerExtruderState = {
  selectedSlot: null,
  bedTemp: 60,
  nozzleTemp: 220,
  maxVolSpeed: 12,
};

export function CalibrationPresetPage({
  printerId,
  caliMode,
  method,
  capabilities,
  onBack,
  onStart,
}: Props) {
  const { t } = useTranslation();

  // ---------------- Preset / slicer picker state ----------------
  // Manual modes (PA Tower and beyond) need the same preset triplet
  // the verification flow asks for — production dispatch slices
  // through the same sidecar. AUTO modes (AUTO_PA_LINE / FLOW_RATE)
  // ignore these fields server-side, but we still render the picker
  // for consistency (operator picks once, server discards).
  const needsPresetPicker = method !== 'auto';

  const bundlesQuery = useQuery<SlicerBundle[]>({
    queryKey: ['slicer-bundles'],
    queryFn: () => api.listSlicerBundles(),
    enabled: needsPresetPicker,
  });
  const presetsQuery = useQuery<UnifiedPresetsResponse>({
    queryKey: ['slicer-presets'],
    queryFn: () => api.getSlicerPresets(),
    staleTime: 30_000,
    enabled: needsPresetPicker,
  });
  const settingsQuery = useQuery({
    queryKey: ['settings'],
    queryFn: api.getSettings,
    staleTime: 60_000,
    enabled: needsPresetPicker,
  });
  // Printer model drives the preset / bundle filter — only show
  // presets whose name matches the printer's long-form ("Bambu Lab
  // A1 mini") or BS-shorthand ("@BBL A1M") naming convention. Avoids
  // operators picking a P1S preset for an A1 mini and getting -16
  // CLI_3MF_NEW_MACHINE_NOT_SUPPORTED at slice time.
  const printerQuery = useQuery<Printer>({
    queryKey: ['printer', printerId],
    queryFn: () => api.getPrinter(printerId),
    staleTime: 60_000,
  });
  const printerModel = printerQuery.data?.model ?? null;

  const bundles = useMemo(
    () => filterBundlesByModel(bundlesQuery.data ?? [], printerModel),
    [bundlesQuery.data, printerModel],
  );
  const presets = useMemo(
    () => filterPresetsByModel(presetsQuery.data, printerModel),
    [presetsQuery.data, printerModel],
  );

  const [presetSource, setPresetSource] = useState<PresetSource>('manual');
  const [ownerFilter, setOwnerFilter] = useState<OwnerFilter>('all');

  const [didInitMode, setDidInitMode] = useState(false);
  useEffect(() => {
    if (didInitMode || bundlesQuery.isPending) return;
    if (bundles.length > 0) setPresetSource('bundle');
    setDidInitMode(true);
  }, [didInitMode, bundlesQuery.isPending, bundles.length]);

  const [printerRef, setPrinterRef] = useState<PresetRef | null>(null);
  const [processRef, setProcessRef] = useState<PresetRef | null>(null);
  const [filamentRef, setFilamentRef] = useState<PresetRef | null>(null);

  useEffect(() => {
    if (!presets) return;
    setPrinterRef((cur) => cur ?? pickDefaultRef(presets, 'printer', ownerFilter));
    setProcessRef((cur) => cur ?? pickDefaultRef(presets, 'process', ownerFilter));
    setFilamentRef((cur) => cur ?? pickDefaultRef(presets, 'filament', ownerFilter));
  }, [presets, ownerFilter]);

  const [bundleId, setBundleId] = useState<string | null>(null);
  const selectedBundle = useMemo(
    () => bundles.find((b) => b.id === bundleId) ?? null,
    [bundles, bundleId],
  );
  const [bundlePrinterName, setBundlePrinterName] = useState<string | null>(null);
  const [bundleProcessName, setBundleProcessName] = useState<string | null>(null);
  const [bundleFilamentName, setBundleFilamentName] = useState<string | null>(null);

  useEffect(() => {
    if (!bundleId && bundles.length > 0) setBundleId(bundles[0].id);
  }, [bundles, bundleId]);

  useEffect(() => {
    if (!selectedBundle) return;
    setBundlePrinterName((p) =>
      p && selectedBundle.printer.includes(p) ? p : selectedBundle.printer[0] ?? null,
    );
    setBundleProcessName((p) =>
      p && selectedBundle.process.includes(p) ? p : selectedBundle.process[0] ?? null,
    );
    setBundleFilamentName((p) =>
      p && selectedBundle.filament.includes(p) ? p : selectedBundle.filament[0] ?? null,
    );
  }, [selectedBundle]);

  const [pickedSlicer, setPickedSlicer] = useState<SlicerKind | null>(null);
  useEffect(() => {
    if (pickedSlicer != null) return;
    const preferred = settingsQuery.data?.preferred_slicer;
    if (preferred === 'orcaslicer' || preferred === 'bambu_studio') {
      setPickedSlicer(preferred);
    }
  }, [settingsQuery.data?.preferred_slicer, pickedSlicer]);

  const [bedType, setBedType] = useState<BedType>('Textured PEI Plate');

  // PA Tower per-mode spec — mirrors the verification page's defaults.
  const isPaTower = caliMode === 'pa_tower';
  const [paStart, setPaStart] = useState<number>(0.0);
  const [paEnd, setPaEnd] = useState<number>(0.1);
  const [paStep, setPaStep] = useState<number>(0.002);
  const [paLayerHeight, setPaLayerHeight] = useState<number>(0.2);

  // PA Pattern uses a denser (visually-readable) sweep at lower K
  // ceiling — defaults mirror the BS-shipped pa_pattern.3mf scaffold
  // (0.0 → 0.08 step 0.005, 17 K levels) since most filaments land
  // around 0.02..0.06 K on Bambu printers. Operator can widen the
  // range — backend regenerates the comb gcode accordingly.
  const isPaPattern = caliMode === 'pa_pattern';
  const [patternStart, setPatternStart] = useState<number>(0.0);
  const [patternEnd, setPatternEnd] = useState<number>(0.08);
  const [patternStep, setPatternStep] = useState<number>(0.005);

  // PA Line — BS DDE defaults are 0.0/0.1/0.002 (51 rows) but 0.1 is too
  // aggressive for direct-drive Bambu printers in practice. We use the
  // same defaults as PA Pattern (0.0/0.08/0.005 = 17 rows) so operators
  // get a tighter sweep out of the box; widen via the inputs if needed.
  const isPaLine = caliMode === 'pa_line';
  const [paLineStart, setPaLineStart] = useState<number>(0.0);
  const [paLineEnd, setPaLineEnd] = useState<number>(0.08);
  const [paLineStep, setPaLineStep] = useState<number>(0.005);
  const [paLinePrintNumbers, setPaLinePrintNumbers] = useState<boolean>(true);

  const statusQuery = useQuery<PrinterStatus>({
    queryKey: ['printerStatus', printerId],
    queryFn: () => api.getPrinterStatus(printerId),
    refetchInterval: 5_000,
  });

  const firstNozzleDia = capabilities?.nozzles?.[0]?.diameter ?? 0.4;
  const [nozzleDia, setNozzleDia] = useState<number>(firstNozzleDia);
  const [nozzleVolType, setNozzleVolType] = useState<NozzleVolumeType>('standard');

  const isDual = Boolean(capabilities?.dual_extruder);
  const extruderList = useMemo(
    () => capabilities?.extruders ?? [{ id: 0, name: 'Main' }],
    [capabilities?.extruders],
  );
  const [activeExtruder, setActiveExtruder] = useState<number>(extruderList[0]?.id ?? 0);

  const [perExtruder, setPerExtruder] = useState<Record<number, PerExtruderState>>(() => {
    const init: Record<number, PerExtruderState> = {};
    for (const ex of extruderList) init[ex.id] = { ...DEFAULT_PER_EXTRUDER };
    return init;
  });

  // When the operator picks a cloud filament preset, fetch its bed /
  // nozzle / max-volumetric-speed values and write them into the
  // per-extruder state so the production-mode MQTT payload reflects
  // the user's actual filament choice instead of the hidden form
  // defaults (60 / 220 / 12). For local / standard / bundle paths
  // the endpoint either returns null or no temperature fields — the
  // defaults stay in place for those (slicer-side --load-settings
  // still applies the right values in the gcode regardless).
  const filamentInfoQuery = useQuery({
    queryKey: ['calibration', 'filament-info', filamentRef?.source, filamentRef?.id],
    queryFn: () => api.getFilamentInfo([filamentRef!.id]),
    enabled: needsPresetPicker && filamentRef?.source === 'cloud' && !!filamentRef.id,
    staleTime: 60_000,
  });
  useEffect(() => {
    if (!needsPresetPicker || filamentRef?.source !== 'cloud') return;
    const info = filamentInfoQuery.data?.[filamentRef.id];
    if (!info) return;
    setPerExtruder((prev) => {
      const next = { ...prev };
      for (const ex of extruderList) {
        const cur = next[ex.id] ?? DEFAULT_PER_EXTRUDER;
        next[ex.id] = {
          ...cur,
          bedTemp: info.hot_plate_temp != null ? Math.round(info.hot_plate_temp) : cur.bedTemp,
          nozzleTemp:
            info.nozzle_temperature != null ? Math.round(info.nozzle_temperature) : cur.nozzleTemp,
          maxVolSpeed:
            info.filament_max_volumetric_speed != null
              ? info.filament_max_volumetric_speed
              : cur.maxVolSpeed,
        };
      }
      return next;
    });
  }, [filamentInfoQuery.data, filamentRef, needsPresetPicker, extruderList]);

  const current = perExtruder[activeExtruder] ?? DEFAULT_PER_EXTRUDER;

  const patchCurrent = (p: Partial<PerExtruderState>) =>
    setPerExtruder((prev) => ({
      ...prev,
      [activeExtruder]: { ...(prev[activeExtruder] ?? DEFAULT_PER_EXTRUDER), ...p },
    }));

  const loadedSlots = useMemo<LoadedSlot[]>(() => {
    const units = statusQuery.data?.ams ?? [];
    const vtTrays = statusQuery.data?.vt_tray ?? [];
    const out: LoadedSlot[] = [];
    for (const unit of units) {
      for (const tray of unit.tray) {
        const loaded = tray.state === 11 || (tray.tray_info_idx && tray.tray_info_idx !== '');
        if (!loaded || !tray.tray_info_idx) continue;
        const globalTrayId = unit.id * 4 + tray.id;
        const label = `AMS ${unit.id + 1} · Slot ${tray.id + 1} · ${tray.tray_sub_brands ?? tray.tray_info_idx}`;
        out.push({
          ams_id: unit.id,
          slot_id: tray.id,
          tray_id: globalTrayId,
          filament_id: tray.tray_info_idx,
          filament_setting_id: null,
          label,
        });
      }
    }
    // External spool / virtual tray (id=254). A1 mini and other printers
    // without AMS or with operator-fed external spool report the loaded
    // filament under ``vt_tray`` instead of any AMS unit's trays — without
    // surfacing it here the picker incorrectly says "no loaded slot" even
    // though the printer has filament threaded and ready. Bambu's tray-id
    // convention reserves 254 for the virtual / external tray.
    for (const tray of vtTrays) {
      const loaded = tray.state === 11 || (tray.tray_info_idx && tray.tray_info_idx !== '');
      if (!loaded || !tray.tray_info_idx) continue;
      const label = `${t('filamentCali.preset.externalSpool', 'External spool')} · ${tray.tray_sub_brands ?? tray.tray_info_idx}`;
      out.push({
        ams_id: 254,
        slot_id: 0,
        tray_id: 254,
        filament_id: tray.tray_info_idx,
        filament_setting_id: null,
        label,
      });
    }
    return out;
  }, [statusQuery.data, t]);

  const buildFilament = (st: PerExtruderState, exId: number): CalibFilamentIn | null => {
    if (!st.selectedSlot) return null;
    // ``filament_setting_id`` carries the operator's chosen filament
    // preset ID so subsequent extrusion_cali_sel binds the AMS slot
    // to the right user-saved filament setting (post-calibration the
    // K-value lives on this setting_id, not on the raw filament_id).
    // Manual path: hook the cloud / local preset the operator picked
    // in the picker above. AMS slot's raw ``filament_setting_id`` is
    // almost always null (Bambu doesn't track user-preset choice in
    // the slot itself); the picker is the authoritative source.
    const derivedSettingId =
      needsPresetPicker && filamentRef ? filamentRef.id : st.selectedSlot.filament_setting_id;
    // Temperatures + volumetric speed are only validated when the
    // operator actually sees them (AUTO methods). For MANUAL modes
    // the inputs are hidden + ignored downstream — the sidecar slicer
    // applies them from the filament preset bundle via --load-settings,
    // not from this payload. Defaults ride through so the backend's
    // non-optional schema fields stay satisfied.
    if (needsPresetPicker) {
      return {
        ams_id: st.selectedSlot.ams_id,
        slot_id: st.selectedSlot.slot_id,
        tray_id: st.selectedSlot.tray_id,
        filament_id: st.selectedSlot.filament_id,
        filament_setting_id: derivedSettingId,
        bed_temp: st.bedTemp,
        nozzle_temp: st.nozzleTemp,
        max_volumetric_speed: st.maxVolSpeed,
        extruder_id: isDual ? exId : undefined,
      };
    }
    if (st.bedTemp <= 0 || st.nozzleTemp <= 0 || st.maxVolSpeed <= 0) {
      return null;
    }
    return {
      ams_id: st.selectedSlot.ams_id,
      slot_id: st.selectedSlot.slot_id,
      tray_id: st.selectedSlot.tray_id,
      filament_id: st.selectedSlot.filament_id,
      filament_setting_id: derivedSettingId,
      bed_temp: st.bedTemp,
      nozzle_temp: st.nozzleTemp,
      max_volumetric_speed: st.maxVolSpeed,
      extruder_id: isDual ? exId : undefined,
    };
  };

  // Per-job dispatcher toggles — re-use the PrintModal panels so the
  // calibration print runs swap macros / bed-levelling / etc. the same
  // way a regular library print does. Defaults are tuned for a
  // calibration test: bed_levelling on, AUTO flow_cali OFF (gcode M900
  // K changes drive the K sweep — letting the printer's pre-print flow-
  // cali run first would lock K to a single value and mask the test),
  // swap macros opt-in. Operator's saved per-printer-model preference
  // overrides these on apply.
  const CALIBRATION_PRINT_OPTIONS_DEFAULT: PrintOptions = {
    ...DEFAULT_PRINT_OPTIONS,
    flow_cali: false,
  };
  const CALIBRATION_SWAP_MACROS_DEFAULT: SwapMacrosOptions = {
    ...DEFAULT_SWAP_MACROS_OPTIONS,
    execute: false,
  };
  const [printOptions, setPrintOptions] = useState<PrintOptions>(CALIBRATION_PRINT_OPTIONS_DEFAULT);
  const [swapMacros, setSwapMacros] = useState<SwapMacrosOptions>(CALIBRATION_SWAP_MACROS_DEFAULT);

  // Read the operator's per-printer-model preference (set via PrintModal
  // submits). Falls back silently to the calibration defaults above on
  // 404 — same shape as PrintModal/index.tsx:230.
  const { data: preferenceData } = useQuery({
    queryKey: ['print-options-preference', printerModel],
    queryFn: async () => {
      try {
        return await api.getPrintOptionsPreference(printerModel!);
      } catch {
        return null;
      }
    },
    enabled: !!printerModel,
    staleTime: 60 * 1000,
  });
  const appliedPreferenceModelsRef = useRef<Set<string>>(new Set());
  useEffect(() => {
    if (!printerModel || !preferenceData) return;
    if (appliedPreferenceModelsRef.current.has(printerModel)) return;
    setPrintOptions(preferenceData.options.print_options);
    setSwapMacros({
      execute: preferenceData.options.swap_macros.execute,
      events: preferenceData.options.swap_macros.events.filter(
        (e): e is SwapMacroEvent => (SWAP_MACRO_EVENTS as readonly string[]).includes(e),
      ),
    });
    appliedPreferenceModelsRef.current.add(printerModel);
  }, [printerModel, preferenceData]);

  // Best-effort persist on submit. Failure silently swallowed — the
  // calibration print itself already succeeded; a failed preference
  // write only means defaults next time. Same logic as PrintModal/
  // index.tsx::persistPreference.
  const persistPreference = useCallback(() => {
    if (!printerModel) return;
    void api
      .upsertPrintOptionsPreference(printerModel, {
        print_options: printOptions,
        swap_macros: { execute: swapMacros.execute, events: swapMacros.events },
      })
      .catch(() => {});
  }, [printerModel, printOptions, swapMacros]);

  const buildPresetExtras = () => {
    const extras: {
      spec?: Record<string, number | string | boolean>;
      bundle?: { bundle_id: string; printer_name: string; process_name: string; filament_names: string[] };
      printer_preset?: PresetRef;
      process_preset?: PresetRef;
      filament_presets?: PresetRef[];
      slicer?: 'orcaslicer' | 'bambu_studio';
      bed_type?: BedType;
      print_options?: PrintOptions;
      swap_macros?: { execute: boolean; events: Array<'swap_mode_start' | 'swap_mode_change_table'> };
    } = {};
    extras.print_options = printOptions;
    extras.swap_macros = {
      execute: swapMacros.execute,
      events: swapMacros.events,
    };
    if (!needsPresetPicker) return extras;
    if (isPaTower) {
      extras.spec = {
        start: paStart,
        end: paEnd,
        step: paStep,
        layer_height: paLayerHeight,
        nozzle_diameter: nozzleDia,
      };
    }
    if (isPaPattern) {
      extras.spec = {
        start: patternStart,
        end: patternEnd,
        step: patternStep,
        nozzle_diameter: nozzleDia,
      };
    }
    if (isPaLine) {
      extras.spec = {
        start: paLineStart,
        end: paLineEnd,
        step: paLineStep,
        print_numbers: paLinePrintNumbers,
        nozzle_diameter: nozzleDia,
      };
    }
    if (presetSource === 'bundle' && selectedBundle && bundlePrinterName && bundleProcessName && bundleFilamentName) {
      extras.bundle = {
        bundle_id: selectedBundle.id,
        printer_name: bundlePrinterName,
        process_name: bundleProcessName,
        filament_names: [bundleFilamentName],
      };
    } else if (presetSource === 'manual' && printerRef && processRef && filamentRef) {
      extras.printer_preset = printerRef;
      extras.process_preset = processRef;
      extras.filament_presets = [filamentRef];
    }
    if (pickedSlicer) extras.slicer = pickedSlicer;
    extras.bed_type = bedType;
    return extras;
  };

  // Local pending state — Start button stays in pending visual from
  // click until onStart resolves (parent handles slicer-sidecar call,
  // FTP upload, queue-item insert; takes a few seconds). Without this
  // the button looks frozen and operators double-tap.
  const [isStarting, setIsStarting] = useState(false);

  const submit = async () => {
    if (isStarting) return;
    setIsStarting(true);
    try {
      await submitInner();
    } finally {
      setIsStarting(false);
    }
  };

  const submitInner = async () => {
    if (method === 'auto' && isDual) {
      const filaments: CalibFilamentIn[] = [];
      for (const ex of extruderList) {
        const f = buildFilament(perExtruder[ex.id] ?? DEFAULT_PER_EXTRUDER, ex.id);
        if (f) filaments.push(f);
      }
      if (filaments.length === 0) return;
      await onStart({
        nozzle_diameter: nozzleDia,
        nozzle_volume_type: nozzleVolType,
        extruder_id: filaments[0].extruder_id ?? 0,
        filaments,
        ...buildPresetExtras(),
      });
      persistPreference();
      return;
    }

    const f = buildFilament(current, activeExtruder);
    if (!f) return;
    await onStart({
      nozzle_diameter: nozzleDia,
      nozzle_volume_type: nozzleVolType,
      extruder_id: activeExtruder,
      filaments: [f],
      ...buildPresetExtras(),
    });
    persistPreference();
  };

  const presetShapeOk = (() => {
    if (!needsPresetPicker) return true;
    if (presetSource === 'bundle') {
      return !!selectedBundle && !!bundlePrinterName && !!bundleProcessName && !!bundleFilamentName;
    }
    return !!printerRef && !!processRef && !!filamentRef;
  })();

  const canStart = (() => {
    const filamentsOk = (() => {
      if (method === 'auto' && isDual) {
        return extruderList.some(
          (ex) => buildFilament(perExtruder[ex.id] ?? DEFAULT_PER_EXTRUDER, ex.id) != null,
        );
      }
      return buildFilament(current, activeExtruder) != null;
    })();
    return filamentsOk && presetShapeOk;
  })();

  const extruderLabel = (name: string) =>
    t(`filamentCali.extruder.${name.toLowerCase()}`, { defaultValue: name });

  return (
    <div className="space-y-4">
      <h3 className="text-base font-semibold text-white">{t('filamentCali.preset.heading')}</h3>

      {isDual && (
        <div className="flex gap-1 rounded-lg p-1 bg-bambu-dark border border-bambu-dark-tertiary">
          {extruderList.map((ex) => (
            <button
              key={ex.id}
              type="button"
              onClick={() => setActiveExtruder(ex.id)}
              className={`flex-1 px-3 py-1.5 text-sm rounded transition-colors ${
                activeExtruder === ex.id
                  ? 'bg-bambu-green text-white'
                  : 'text-bambu-gray hover:text-white'
              }`}
            >
              {extruderLabel(ex.name)}
            </button>
          ))}
        </div>
      )}

      {needsPresetPicker && (
        <section className="space-y-3">
          <SlicerPicker value={pickedSlicer} onChange={setPickedSlicer} />
          <BedTypePicker value={bedType} onChange={setBedType} />
          <PresetSourceControl
            mode={presetSource}
            onModeChange={setPresetSource}
            ownerFilter={ownerFilter}
            onOwnerFilterChange={setOwnerFilter}
            bundles={bundles}
            selectedBundleId={bundleId}
            onBundleChange={setBundleId}
          />
          {presetSource === 'manual' && (
            <div className="grid grid-cols-1 gap-2">
              {presets ? (
                <>
                  <PresetDropdown
                    label={t('slice.printer', 'Printer profile')}
                    slot="printer"
                    data={presets}
                    value={printerRef}
                    onChange={setPrinterRef}
                    ownerFilter={ownerFilter}
                  />
                  <PresetDropdown
                    label={t('slice.process', 'Process profile')}
                    slot="process"
                    data={presets}
                    value={processRef}
                    onChange={setProcessRef}
                    ownerFilter={ownerFilter}
                  />
                  <PresetDropdown
                    label={t('slice.filament', 'Filament profile')}
                    slot="filament"
                    data={presets}
                    value={filamentRef}
                    onChange={setFilamentRef}
                    ownerFilter={ownerFilter}
                  />
                </>
              ) : (
                <div className="p-3 bg-bambu-dark rounded text-sm text-bambu-gray">
                  {t('filamentCali.verifyDownload.loadingPresets', 'Loading presets…')}
                </div>
              )}
            </div>
          )}
          {presetSource === 'bundle' && selectedBundle && (
            <div className="grid grid-cols-1 gap-2">
              <BundleStringDropdown
                label={t('slice.printer', 'Printer profile')}
                options={selectedBundle.printer}
                value={bundlePrinterName}
                onChange={setBundlePrinterName}
              />
              <BundleStringDropdown
                label={t('slice.process', 'Process profile')}
                options={selectedBundle.process}
                value={bundleProcessName}
                onChange={setBundleProcessName}
              />
              <BundleStringDropdown
                label={t('slice.filament', 'Filament profile')}
                options={selectedBundle.filament}
                value={bundleFilamentName}
                onChange={setBundleFilamentName}
              />
            </div>
          )}
          {isPaTower && (
            <div className="space-y-2 border border-bambu-dark-tertiary rounded p-3">
              <h4 className="text-sm font-medium text-bambu-gray">
                {t('filamentCali.verifyDownload.specHeading')}
              </h4>
              <div className="grid grid-cols-2 gap-2">
                <label className="block">
                  <span className="text-xs text-bambu-gray">
                    {t('filamentCali.verifyDownload.startK')}
                  </span>
                  <input
                    type="number"
                    step="0.001"
                    value={paStart}
                    onChange={(e) => setPaStart(parseFloat(e.target.value) || 0)}
                    className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
                  />
                </label>
                <label className="block">
                  <span className="text-xs text-bambu-gray">
                    {t('filamentCali.verifyDownload.endK')}
                  </span>
                  <input
                    type="number"
                    step="0.001"
                    value={paEnd}
                    onChange={(e) => setPaEnd(parseFloat(e.target.value) || 0)}
                    className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
                  />
                </label>
                <label className="block">
                  <span className="text-xs text-bambu-gray">
                    {t('filamentCali.verifyDownload.stepK')}
                  </span>
                  <input
                    type="number"
                    step="0.001"
                    value={paStep}
                    onChange={(e) => setPaStep(parseFloat(e.target.value) || 0)}
                    className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
                  />
                </label>
                <label className="block">
                  <span className="text-xs text-bambu-gray">
                    {t('filamentCali.verifyDownload.layerHeight')}
                  </span>
                  <input
                    type="number"
                    step="0.05"
                    value={paLayerHeight}
                    onChange={(e) => setPaLayerHeight(parseFloat(e.target.value) || 0.2)}
                    className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
                  />
                </label>
              </div>
            </div>
          )}

          {isPaPattern && (
            <div className="space-y-2 border border-bambu-dark-tertiary rounded p-3">
              <h4 className="text-sm font-medium text-bambu-gray">
                {t('filamentCali.verifyDownload.specHeading')}
              </h4>
              <div className="grid grid-cols-3 gap-2">
                <label className="block">
                  <span className="text-xs text-bambu-gray">
                    {t('filamentCali.verifyDownload.startK')}
                  </span>
                  <input
                    type="number"
                    step="0.001"
                    value={patternStart}
                    onChange={(e) => setPatternStart(parseFloat(e.target.value) || 0)}
                    className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
                  />
                </label>
                <label className="block">
                  <span className="text-xs text-bambu-gray">
                    {t('filamentCali.verifyDownload.endK')}
                  </span>
                  <input
                    type="number"
                    step="0.001"
                    value={patternEnd}
                    onChange={(e) => setPatternEnd(parseFloat(e.target.value) || 0)}
                    className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
                  />
                </label>
                <label className="block">
                  <span className="text-xs text-bambu-gray">
                    {t('filamentCali.verifyDownload.stepK')}
                  </span>
                  <input
                    type="number"
                    step="0.001"
                    value={patternStep}
                    onChange={(e) => setPatternStep(parseFloat(e.target.value) || 0)}
                    className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
                  />
                </label>
              </div>
            </div>
          )}

          {isPaLine && (
            <div className="space-y-2 border border-bambu-dark-tertiary rounded p-3">
              <h4 className="text-sm font-medium text-bambu-gray">
                {t('filamentCali.verifyDownload.specHeading')}
              </h4>
              <div className="grid grid-cols-3 gap-2">
                <label className="block">
                  <span className="text-xs text-bambu-gray">
                    {t('filamentCali.verifyDownload.startK')}
                  </span>
                  <input
                    type="number"
                    step="0.001"
                    value={paLineStart}
                    onChange={(e) => setPaLineStart(parseFloat(e.target.value) || 0)}
                    className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
                  />
                </label>
                <label className="block">
                  <span className="text-xs text-bambu-gray">
                    {t('filamentCali.verifyDownload.endK')}
                  </span>
                  <input
                    type="number"
                    step="0.001"
                    value={paLineEnd}
                    onChange={(e) => setPaLineEnd(parseFloat(e.target.value) || 0)}
                    className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
                  />
                </label>
                <label className="block">
                  <span className="text-xs text-bambu-gray">
                    {t('filamentCali.verifyDownload.stepK')}
                  </span>
                  <input
                    type="number"
                    step="0.001"
                    value={paLineStep}
                    onChange={(e) => setPaLineStep(parseFloat(e.target.value) || 0)}
                    className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
                  />
                </label>
              </div>
              <label className="flex items-center gap-2 text-xs text-bambu-gray cursor-pointer">
                <input
                  type="checkbox"
                  checked={paLinePrintNumbers}
                  onChange={(e) => setPaLinePrintNumbers(e.target.checked)}
                  className="accent-bambu-green"
                />
                {t('filamentCali.preset.paLinePrintNumbers')}
              </label>
            </div>
          )}
        </section>
      )}

      <section>
        <div className="grid grid-cols-2 gap-2">
          <label className="block">
            <span className="text-xs text-bambu-gray">{t('filamentCali.preset.nozzleDia')}</span>
            <select
              value={nozzleDia}
              onChange={(e) => setNozzleDia(parseFloat(e.target.value))}
              className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
            >
              {(capabilities?.nozzles ?? [{ diameter: 0.4 }]).map((n, i) => (
                <option key={i} value={n.diameter ?? 0.4}>
                  {n.diameter ?? 0.4} mm
                </option>
              ))}
            </select>
          </label>
          <label className="block">
            <span className="text-xs text-bambu-gray">{t('filamentCali.preset.nozzleType')}</span>
            <select
              value={nozzleVolType}
              onChange={(e) => setNozzleVolType(e.target.value as NozzleVolumeType)}
              className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
            >
              <option value="standard">Standard</option>
              <option value="high_flow">High Flow</option>
              <option value="tpu_high_flow">TPU High Flow</option>
              <option value="hybrid">Hybrid</option>
            </select>
          </label>
        </div>
      </section>

      <section>
        <span className="text-sm font-medium text-bambu-gray block mb-2">
          {t('filamentCali.preset.selectFilament')}
        </span>
        {loadedSlots.length === 0 ? (
          <div className="p-3 bg-bambu-dark rounded text-sm text-bambu-gray">
            {t('filamentCali.preset.noLoadedSlot')}
          </div>
        ) : (
          <div className="space-y-2">
            {loadedSlots.map((s) => (
              <button
                key={`${s.ams_id}-${s.slot_id}`}
                type="button"
                onClick={() => patchCurrent({ selectedSlot: s })}
                className={`w-full text-left p-2 rounded border ${
                  current.selectedSlot?.ams_id === s.ams_id &&
                  current.selectedSlot.slot_id === s.slot_id
                    ? 'border-bambu-green bg-bambu-green/10'
                    : 'border-bambu-dark-tertiary bg-bambu-dark hover:border-bambu-green/50'
                }`}
              >
                <span className="text-white">{s.label}</span>
              </button>
            ))}
          </div>
        )}
      </section>

      {/* Bed temp / nozzle temp / max volumetric speed are only used by
       *  AUTO-method calibration paths (FLOW_RATE auto, AUTO_PA_LINE)
       *  where the values get embedded directly into the MQTT
       *  extrusion_cali / flow_rate_cali payload. For MANUAL modes the
       *  actual print is sliced through the sidecar — the slicer
       *  applies the selected filament preset's own bed_temp /
       *  nozzle_temperature / filament_max_volumetric_speed and our
       *  three numeric inputs are ignored. Hiding them for manual
       *  modes removes a meaningless extra step from the wizard;
       *  defaults (60 / 220 / 12) ride through the request body to
       *  satisfy the backend schema's non-optional fields. */}
      {!needsPresetPicker && (
        <section className="grid grid-cols-3 gap-2">
          <label className="block">
            <span className="text-xs text-bambu-gray">{t('filamentCali.preset.bedTemp')}</span>
            <input
              type="number"
              value={current.bedTemp}
              onChange={(e) => patchCurrent({ bedTemp: parseInt(e.target.value, 10) || 0 })}
              className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
            />
          </label>
          <label className="block">
            <span className="text-xs text-bambu-gray">{t('filamentCali.preset.nozzleTemp')}</span>
            <input
              type="number"
              value={current.nozzleTemp}
              onChange={(e) => patchCurrent({ nozzleTemp: parseInt(e.target.value, 10) || 0 })}
              className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
            />
          </label>
          <label className="block">
            <span className="text-xs text-bambu-gray">{t('filamentCali.preset.maxVolSpeed')}</span>
            <input
              type="number"
              step="0.5"
              value={current.maxVolSpeed}
              onChange={(e) => patchCurrent({ maxVolSpeed: parseFloat(e.target.value) || 0 })}
              className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
            />
          </label>
        </section>
      )}

      <div className="pt-2 border-t border-bambu-dark-tertiary">
        <PrintOptionsPanel options={printOptions} onChange={setPrintOptions} />
        <SwapMacrosPanel options={swapMacros} onChange={setSwapMacros} />
      </div>

      <div className="flex justify-between pt-2 border-t border-bambu-dark-tertiary">
        <button
          type="button"
          onClick={onBack}
          className="px-3 py-1.5 text-sm text-bambu-gray hover:text-white"
        >
          {t('filamentCali.back')}
        </button>
        <button
          type="button"
          onClick={submit}
          disabled={!canStart || isStarting}
          className="px-4 py-2 rounded bg-bambu-green text-white text-sm font-medium cursor-pointer hover:bg-bambu-green/90 active:bg-bambu-green/80 transition-colors disabled:opacity-40 disabled:cursor-not-allowed disabled:hover:bg-bambu-green flex items-center gap-2"
        >
          {isStarting && (
            <svg
              className="animate-spin w-4 h-4"
              xmlns="http://www.w3.org/2000/svg"
              fill="none"
              viewBox="0 0 24 24"
            >
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
              <path
                className="opacity-75"
                fill="currentColor"
                d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"
              />
            </svg>
          )}
          {isStarting ? t('filamentCali.startingCalibration') : t('filamentCali.startCalibration')}
        </button>
      </div>
    </div>
  );
}
