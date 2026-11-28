import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  EXTERNAL_AMS_ID,
  EXTERNAL_DEPUTY_ID,
  externalSlotIndex,
  layoutGlobalTrayId,
  rackLabel,
  trayLayoutOf,
} from '../../utils/trayIds';
import { NumberInput } from '../NumberInput';
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
} from '../../api/client';
import {
  CalibrationSlicingSection,
  type CalibrationPresetChoice,
  type CalibrationSlot,
} from './CalibrationSlicingSection';
import { EMPTY_CALIBRATION_CHOICE, choiceDecidedNow } from './calibrationChoice';
import { usePresetCatalogue } from '../slicing-setup/usePresetCatalogue';
import { useSlicerChoice } from '../slicing-setup/useSlicerChoice';
import { useAuth } from '../../contexts/AuthContext';
import { tempDefaultsForFilament } from '../../utils/calibrationTemp';
import { Select } from '../Select';

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

interface LoadedSlot {
  ams_id: number;
  slot_id: number;
  tray_id: number;
  filament_id: string;
  filament_setting_id: string | null;
  filament_type: string | null;
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

  // The printer model drives the per-model print-options preference below.
  const printerQuery = useQuery<Printer>({
    queryKey: ['printer', printerId],
    queryFn: () => api.getPrinter(printerId),
    staleTime: 60_000,
  });
  const printerModel = printerQuery.data?.model ?? null;

  // Nozzle the operator is calibrating (defaults to the printer's first
  // installed nozzle). The slicing section proves the printer preset for this
  // nozzle and this printer's model (spec slicing-setup R4).
  const firstNozzleDia = capabilities?.nozzles?.[0]?.diameter ?? 0.4;
  const [nozzleDia, setNozzleDia] = useState<number>(firstNozzleDia);
  const [nozzleVolType, setNozzleVolType] = useState<NozzleVolumeType>('standard');

  // The slicing section (spec slicing-setup §7.2) decides the slicer, the bed
  // plate and the three presets; the page reads its answer and keeps the slot,
  // the nozzle and the K corridor to itself.
  const [slicing, setSlicing] = useState<CalibrationPresetChoice>(EMPTY_CALIBRATION_CHOICE);
  // The same catalogue the section reads: Start is judged on the answer this
  // render shows, not only on the section's last report (review I-2).
  const { hasPermission } = useAuth();
  const canPickPresets = needsPresetPicker && hasPermission('library:upload');
  const slicer = useSlicerChoice(`bamdude:slicer-pick:calibration:${printerId}`, { enabled: canPickPresets });
  const catalogue = usePresetCatalogue({ enabled: canPickPresets, slicer: slicer.forPresets });
  const printerRef = slicing.printer;
  const processRef = slicing.process;
  const filamentRef = slicing.filament;
  const pickedSlicer = slicing.slicer;
  const bedType = slicing.plate;

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

  // Volumetric Speed Tower — start/end/step in mm³/s. Defaults mirror
  // the verification page (5 → 20 step 0.5, the Orca-desktop reference
  // sweep). The tower height is derived server-side as (end−start+1)/step.
  const isVolSpeed = caliMode === 'vol_speed_tower';
  const [volStart, setVolStart] = useState<number>(5);
  const [volEnd, setVolEnd] = useState<number>(20);
  const [volStep, setVolStep] = useState<number>(0.5);

  // VFA Tower — start/end/step in mm/s (linear speed, no volumetric
  // transform). Defaults mirror the BS/Orca VFA_Test_Dlg (40 → 200 step
  // 10) and the verification page. Tower height is derived server-side.
  const isVfa = caliMode === 'vfa_tower';
  const [vfaStart, setVfaStart] = useState<number>(40);
  const [vfaEnd, setVfaEnd] = useState<number>(200);
  const [vfaStep, setVfaStep] = useState<number>(10);

  // Temp Tower — start/end nozzle temperature in °C, descending
  // (start > end). No step: BS fixes the band at 10 mm / 5 °C. Defaults
  // are the BS Temp_Calibration_Dlg PLA preset; the effect below re-seeds
  // them from the selected filament's type.
  const isTemp = caliMode === 'temp_tower';
  const [tempStart, setTempStart] = useState<number>(230);
  const [tempEnd, setTempEnd] = useState<number>(190);

  const statusQuery = useQuery<PrinterStatus>({
    queryKey: ['printerStatus', printerId],
    queryFn: () => api.getPrinterStatus(printerId),
    refetchInterval: 5_000,
  });

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
    queryFn: () => api.getFilamentInfo([filamentRef!.id], true),
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

  // Temp Tower: seed start/end from the selected AMS slot's filament type
  // (BS reads it off a filament-type radio; here it comes from the loaded
  // spool). Re-seeds when the slot changes; a later manual edit survives
  // until the operator picks a different slot.
  const selectedSlot = current.selectedSlot;
  useEffect(() => {
    if (!isTemp || !selectedSlot) return;
    const d = tempDefaultsForFilament(`${selectedSlot.filament_type ?? ''} ${selectedSlot.label}`);
    setTempStart(d.start);
    setTempEnd(d.end);
  }, [isTemp, selectedSlot]);

  // The slot the section picks a filament for, by its family (D6): another
  // slot, or the same one on the other extruder, is another key.
  const calibrationSlot = useMemo<CalibrationSlot | null>(
    () =>
      selectedSlot
        ? {
            key: `${activeExtruder}:${selectedSlot.ams_id}:${selectedSlot.slot_id}`,
            label: selectedSlot.label,
            familyId: selectedSlot.filament_id.trim() || null,
          }
        : null,
    [activeExtruder, selectedSlot],
  );

  const patchCurrent = (p: Partial<PerExtruderState>) =>
    setPerExtruder((prev) => ({
      ...prev,
      [activeExtruder]: { ...(prev[activeExtruder] ?? DEFAULT_PER_EXTRUDER), ...p },
    }));

  const loadedSlots = useMemo<LoadedSlot[]>(() => {
    const units = statusQuery.data?.ams ?? [];
    const vtTrays = statusQuery.data?.vt_tray ?? [];
    // The printer's numbering (utils/trayIds): an AMS-HT is its unit id, a rack AMS
    // 28 + k / 37 + k, a direct rack's input its own id.
    const layout = trayLayoutOf(statusQuery.data);
    const out: LoadedSlot[] = [];
    for (const unit of units) {
      for (const tray of unit.tray) {
        const loaded = tray.state === 11 || (tray.tray_info_idx && tray.tray_info_idx !== '');
        if (!loaded || !tray.tray_info_idx) continue;
        const globalTrayId = layoutGlobalTrayId(layout, unit.id, tray.id);
        const where = rackLabel(layout, globalTrayId) ?? `AMS ${unit.id + 1} · Slot ${tray.id + 1}`;
        const label = `${where} · ${tray.tray_sub_brands ?? tray.tray_info_idx}`;
        out.push({
          ams_id: unit.id,
          slot_id: tray.id,
          tray_id: globalTrayId,
          filament_id: tray.tray_info_idx,
          filament_setting_id: null,
          filament_type: tray.tray_type,
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
      // A direct rack (H3C, P3C): each PrintTool's input by its own id, stored (255, index).
      const inputId = typeof tray.id === 'number' ? tray.id : EXTERNAL_DEPUTY_ID;
      const rackInput = layout.directRack ? rackLabel(layout, inputId) : null;
      const label = `${rackInput ?? t('filamentCali.preset.externalSpool', 'External spool')} · ${tray.tray_sub_brands ?? tray.tray_info_idx}`;
      out.push({
        ams_id: rackInput ? EXTERNAL_AMS_ID : 254,
        slot_id: rackInput ? externalSlotIndex(inputId) : 0,
        tray_id: rackInput ? inputId : 254,
        filament_id: tray.tray_info_idx,
        filament_setting_id: null,
        filament_type: tray.tray_type,
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
    flow_cali: 'off',
  };
  const CALIBRATION_SWAP_MACROS_DEFAULT: SwapMacrosOptions = {
    ...DEFAULT_SWAP_MACROS_OPTIONS,
    execute: false,
  };
  const [printOptions, setPrintOptions] = useState<PrintOptions>(CALIBRATION_PRINT_OPTIONS_DEFAULT);
  const [swapMacros, setSwapMacros] = useState<SwapMacrosOptions>(CALIBRATION_SWAP_MACROS_DEFAULT);

  // Read the operator's per-printer-model preference (set via PrintModal
  // submits). Nothing saved (null) or a failed read keeps the calibration
  // defaults above — the same rule as the Print dialog.
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
    // Merge over DEFAULT so a preference saved before a new option existed
    // (e.g. nozzle_offset_cali, #1682) still gets a defined value.
    setPrintOptions({ ...DEFAULT_PRINT_OPTIONS, ...preferenceData.options.print_options });
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
      printer_preset?: PresetRef;
      process_preset?: PresetRef;
      filament_presets?: PresetRef[];
      slicer?: 'orcaslicer' | 'bambu_studio';
      bed_type?: BedType;
      // The calibration StartSession has no 'auto' concept and forbids extra
      // keys — send ONLY the six bool fields it accepts (no nozzle_offset_cali /
      // preheat_*), converting the tri-state (auto/on -> run, off -> skip).
      print_options?: {
        bed_levelling: boolean;
        flow_cali: boolean;
        layer_inspect: boolean;
        timelapse: boolean;
        mesh_mode_fast_check: boolean;
        gcode_injection: boolean;
      };
      swap_macros?: { execute: boolean; events: Array<'swap_mode_start' | 'swap_mode_change_table'> };
    } = {};
    extras.print_options = {
      bed_levelling: printOptions.bed_levelling !== 'off',
      flow_cali: printOptions.flow_cali !== 'off',
      layer_inspect: printOptions.layer_inspect,
      timelapse: printOptions.timelapse,
      mesh_mode_fast_check: printOptions.mesh_mode_fast_check,
      gcode_injection: printOptions.gcode_injection,
    };
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
    if (isVolSpeed) {
      extras.spec = {
        start: volStart,
        end: volEnd,
        step: volStep,
        nozzle_diameter: nozzleDia,
      };
    }
    if (isVfa) {
      extras.spec = {
        start: vfaStart,
        end: vfaEnd,
        step: vfaStep,
        nozzle_diameter: nozzleDia,
      };
    }
    if (isTemp) {
      extras.spec = {
        start: tempStart,
        end: tempEnd,
        nozzle_diameter: nozzleDia,
      };
    }
    if (printerRef && processRef && filamentRef) {
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
    // Asked again at the click: the presets must be the current catalogue's
    // and the printer proven now (review I-2).
    if (!presetShapeOk) return;
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
    // Decided on the catalogue answer, nozzle and slot the page sees now: the
    // printer's proof is the section's, made a commit late (review I-2).
    const now = { catalogue: catalogue.revision, nozzle: nozzleDia, slot: calibrationSlot?.key ?? null };
    return choiceDecidedNow(slicing, now) && !!printerRef && !!processRef && !!filamentRef;
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
          <CalibrationSlicingSection
            printerId={printerId}
            mode="preset"
            needsPresetPicker={needsPresetPicker}
            nozzle={nozzleDia}
            slot={calibrationSlot}
            catalogue={catalogue}
            slicer={slicer}
            value={slicing}
            onChange={setSlicing}
            disabled={isStarting}
          />
          {isPaTower && (
            <div className="space-y-2 border border-bambu-dark-tertiary rounded p-3">
              <h4 className="text-sm font-medium text-bambu-gray">
                {t('filamentCali.verifyDownload.specHeadings.paTower')}
              </h4>
              <div className="grid grid-cols-2 gap-2">
                <label className="block">
                  <span className="text-xs text-bambu-gray">
                    {t('filamentCali.verifyDownload.startK')}
                  </span>
                  <NumberInput
                    step="0.001"
                    value={paStart}
                    onChange={(v) => setPaStart(v)}
                    fallback={0}
                    className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
                  />
                </label>
                <label className="block">
                  <span className="text-xs text-bambu-gray">
                    {t('filamentCali.verifyDownload.endK')}
                  </span>
                  <NumberInput
                    step="0.001"
                    value={paEnd}
                    onChange={(v) => setPaEnd(v)}
                    fallback={0}
                    className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
                  />
                </label>
                <label className="block">
                  <span className="text-xs text-bambu-gray">
                    {t('filamentCali.verifyDownload.stepK')}
                  </span>
                  <NumberInput
                    step="0.001"
                    value={paStep}
                    onChange={(v) => setPaStep(v)}
                    fallback={0}
                    className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
                  />
                </label>
                <label className="block">
                  <span className="text-xs text-bambu-gray">
                    {t('filamentCali.verifyDownload.layerHeight')}
                  </span>
                  <NumberInput
                    step="0.05"
                    value={paLayerHeight}
                    onChange={(v) => setPaLayerHeight(v)}
                    fallback={0.2}
                    className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
                  />
                </label>
              </div>
            </div>
          )}

          {isPaPattern && (
            <div className="space-y-2 border border-bambu-dark-tertiary rounded p-3">
              <h4 className="text-sm font-medium text-bambu-gray">
                {t('filamentCali.verifyDownload.specHeadings.paPattern')}
              </h4>
              <div className="grid grid-cols-3 gap-2">
                <label className="block">
                  <span className="text-xs text-bambu-gray">
                    {t('filamentCali.verifyDownload.startK')}
                  </span>
                  <NumberInput
                    step="0.001"
                    value={patternStart}
                    onChange={(v) => setPatternStart(v)}
                    fallback={0}
                    className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
                  />
                </label>
                <label className="block">
                  <span className="text-xs text-bambu-gray">
                    {t('filamentCali.verifyDownload.endK')}
                  </span>
                  <NumberInput
                    step="0.001"
                    value={patternEnd}
                    onChange={(v) => setPatternEnd(v)}
                    fallback={0}
                    className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
                  />
                </label>
                <label className="block">
                  <span className="text-xs text-bambu-gray">
                    {t('filamentCali.verifyDownload.stepK')}
                  </span>
                  <NumberInput
                    step="0.001"
                    value={patternStep}
                    onChange={(v) => setPatternStep(v)}
                    fallback={0}
                    className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
                  />
                </label>
              </div>
            </div>
          )}

          {isPaLine && (
            <div className="space-y-2 border border-bambu-dark-tertiary rounded p-3">
              <h4 className="text-sm font-medium text-bambu-gray">
                {t('filamentCali.verifyDownload.specHeadings.paLine')}
              </h4>
              <div className="grid grid-cols-3 gap-2">
                <label className="block">
                  <span className="text-xs text-bambu-gray">
                    {t('filamentCali.verifyDownload.startK')}
                  </span>
                  <NumberInput
                    step="0.001"
                    value={paLineStart}
                    onChange={(v) => setPaLineStart(v)}
                    fallback={0}
                    className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
                  />
                </label>
                <label className="block">
                  <span className="text-xs text-bambu-gray">
                    {t('filamentCali.verifyDownload.endK')}
                  </span>
                  <NumberInput
                    step="0.001"
                    value={paLineEnd}
                    onChange={(v) => setPaLineEnd(v)}
                    fallback={0}
                    className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
                  />
                </label>
                <label className="block">
                  <span className="text-xs text-bambu-gray">
                    {t('filamentCali.verifyDownload.stepK')}
                  </span>
                  <NumberInput
                    step="0.001"
                    value={paLineStep}
                    onChange={(v) => setPaLineStep(v)}
                    fallback={0}
                    className="accent-bambu-green w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
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

          {isVolSpeed && (
            <div className="space-y-2 border border-bambu-dark-tertiary rounded p-3">
              <h4 className="text-sm font-medium text-bambu-gray">
                {t('filamentCali.verifyDownload.specHeadings.volSpeedTower')}
              </h4>
              <div className="grid grid-cols-3 gap-2">
                <label className="block">
                  <span className="text-xs text-bambu-gray">
                    {t('filamentCali.verifyDownload.startVol')}
                  </span>
                  <NumberInput
                    step="0.5"
                    value={volStart}
                    onChange={(v) => setVolStart(v)}
                    fallback={0}
                    className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
                  />
                </label>
                <label className="block">
                  <span className="text-xs text-bambu-gray">
                    {t('filamentCali.verifyDownload.endVol')}
                  </span>
                  <NumberInput
                    step="0.5"
                    value={volEnd}
                    onChange={(v) => setVolEnd(v)}
                    fallback={0}
                    className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
                  />
                </label>
                <label className="block">
                  <span className="text-xs text-bambu-gray">
                    {t('filamentCali.verifyDownload.stepVol')}
                  </span>
                  <NumberInput
                    step="0.1"
                    value={volStep}
                    onChange={(v) => setVolStep(v)}
                    fallback={0}
                    className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
                  />
                </label>
              </div>
            </div>
          )}

          {isVfa && (
            <div className="space-y-2 border border-bambu-dark-tertiary rounded p-3">
              <h4 className="text-sm font-medium text-bambu-gray">
                {t('filamentCali.verifyDownload.specHeadings.vfaTower')}
              </h4>
              <div className="grid grid-cols-3 gap-2">
                <label className="block">
                  <span className="text-xs text-bambu-gray">
                    {t('filamentCali.verifyDownload.startSpeed')}
                  </span>
                  <NumberInput
                    step="1"
                    value={vfaStart}
                    onChange={(v) => setVfaStart(v)}
                    fallback={0}
                    className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
                  />
                </label>
                <label className="block">
                  <span className="text-xs text-bambu-gray">
                    {t('filamentCali.verifyDownload.endSpeed')}
                  </span>
                  <NumberInput
                    step="1"
                    value={vfaEnd}
                    onChange={(v) => setVfaEnd(v)}
                    fallback={0}
                    className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
                  />
                </label>
                <label className="block">
                  <span className="text-xs text-bambu-gray">
                    {t('filamentCali.verifyDownload.stepSpeed')}
                  </span>
                  <NumberInput
                    step="1"
                    value={vfaStep}
                    onChange={(v) => setVfaStep(v)}
                    fallback={0}
                    className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
                  />
                </label>
              </div>
            </div>
          )}

          {isTemp && (
            <div className="space-y-2 border border-bambu-dark-tertiary rounded p-3">
              <h4 className="text-sm font-medium text-bambu-gray">
                {t('filamentCali.verifyDownload.specHeadings.tempTower')}
              </h4>
              <div className="grid grid-cols-2 gap-2">
                <label className="block">
                  <span className="text-xs text-bambu-gray">
                    {t('filamentCali.verifyDownload.startTemp')}
                  </span>
                  <NumberInput
                    step="5"
                    value={tempStart}
                    onChange={(v) => setTempStart(v)}
                    fallback={0}
                    className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
                  />
                </label>
                <label className="block">
                  <span className="text-xs text-bambu-gray">
                    {t('filamentCali.verifyDownload.endTemp')}
                  </span>
                  <NumberInput
                    step="5"
                    value={tempEnd}
                    onChange={(v) => setTempEnd(v)}
                    fallback={0}
                    className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
                  />
                </label>
              </div>
              <p className="text-xs text-bambu-gray">{t('filamentCali.verifyDownload.tempHint')}</p>
            </div>
          )}
        </section>
      )}

      <section>
        <div className="grid grid-cols-2 gap-2">
          <label className="block">
            <span className="text-xs text-bambu-gray">{t('filamentCali.preset.nozzleDia')}</span>
            <Select
              size="sm"
              className="w-full"
              value={nozzleDia}
              onChange={(e) => setNozzleDia(parseFloat(e.target.value))}
            >
              {(capabilities?.nozzles ?? [{ diameter: 0.4 }]).map((n, i) => (
                <option key={i} value={n.diameter ?? 0.4}>
                  {n.diameter ?? 0.4} mm
                </option>
              ))}
            </Select>
          </label>
          <label className="block">
            <span className="text-xs text-bambu-gray">{t('filamentCali.preset.nozzleType')}</span>
            <Select
              size="sm"
              className="w-full"
              value={nozzleVolType}
              onChange={(e) => setNozzleVolType(e.target.value as NozzleVolumeType)}
            >
              <option value="standard">Standard</option>
              <option value="high_flow">High Flow</option>
              <option value="tpu_high_flow">TPU High Flow</option>
              <option value="e3d_high_flow">E3D High Flow</option>
              <option value="hybrid">Hybrid</option>
            </Select>
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
            <NumberInput
              value={current.bedTemp}
              onChange={(v) => patchCurrent({ bedTemp: v })}
              integer
              fallback={0}
              className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
            />
          </label>
          <label className="block">
            <span className="text-xs text-bambu-gray">{t('filamentCali.preset.nozzleTemp')}</span>
            <NumberInput
              value={current.nozzleTemp}
              onChange={(v) => patchCurrent({ nozzleTemp: v })}
              integer
              fallback={0}
              className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
            />
          </label>
          <label className="block">
            <span className="text-xs text-bambu-gray">{t('filamentCali.preset.maxVolSpeed')}</span>
            <NumberInput
              step="0.5"
              value={current.maxVolSpeed}
              onChange={(v) => patchCurrent({ maxVolSpeed: v })}
              fallback={0}
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
