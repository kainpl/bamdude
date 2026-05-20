import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useQuery } from '@tanstack/react-query';

import { api } from '../../api/client';
import type {
  BedType,
  CalibBakeOnlyIn,
  CaliMode,
  CalibSliceOnlyIn,
  PresetRef,
  SlicerBundle,
  UnifiedPresetsResponse,
} from '../../api/client';
import { useToast } from '../../contexts/ToastContext';
import { tempDefaultsForFilament } from '../../utils/calibrationTemp';
import {
  BundleStringDropdown,
  PresetDropdown,
  PresetSourceControl,
} from '../preset-picker/PresetTripletPicker';
import {
  TIER_ORDER,
  matchesOwnerFilter,
  type OwnerFilter,
} from '../preset-picker/presetPickerUtils';
import { BedTypePicker } from '../preset-picker/BedTypePicker';
import { SlicerPicker, type SlicerKind } from '../preset-picker/SlicerPicker';

interface Props {
  printerId: number;
  caliMode: CaliMode;
  onBack: () => void;
  onDone: () => void;
}

type PresetSource = 'manual' | 'bundle';

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
 * Verification-mode download page (W2 Phase 1+).
 *
 * Mirrors SliceModal's preset-source UX: a "Manual / Bundle" segmented
 * switch (shown only when bundles exist) sitting above the actual
 * picker. Manual mode renders the cloud / local / standard tiered
 * dropdowns with a 3-state owner filter; Bundle mode renders the bundle
 * dropdown + per-bundle process / filament name dropdowns. The PA Tower
 * spec form stays mode-agnostic at the bottom.
 *
 * The backend's slice-only route accepts both shapes (see
 * ``backend/app/schemas/filament_calibration.py::CalibSliceOnlyIn``).
 */
export function CalibrationVerifyDownloadPage({ printerId, caliMode, onBack, onDone }: Props) {
  const { t } = useTranslation();
  const { showToast } = useToast();

  const bundlesQuery = useQuery<SlicerBundle[]>({
    queryKey: ['slicer-bundles'],
    queryFn: () => api.listSlicerBundles(),
  });
  const presetsQuery = useQuery<UnifiedPresetsResponse>({
    queryKey: ['slicer-presets'],
    queryFn: () => api.getSlicerPresets(),
    staleTime: 30_000,
  });
  const settingsQuery = useQuery({
    queryKey: ['settings'],
    queryFn: api.getSettings,
    staleTime: 60_000,
  });

  const bundles = bundlesQuery.data ?? [];
  const presets = presetsQuery.data;

  const [presetSource, setPresetSource] = useState<PresetSource>('manual');
  const [ownerFilter, setOwnerFilter] = useState<OwnerFilter>('all');

  // One-shot initial pick — if bundles exist when the page mounts, start
  // in bundle mode (matches SliceModal). After the first run the user is
  // free to flip the segmented control; we must NOT re-assert 'bundle'
  // on every render or "Manual" becomes un-clickable. ``didInitMode``
  // gates the effect to the first non-pending bundles response.
  const [didInitMode, setDidInitMode] = useState(false);
  useEffect(() => {
    if (didInitMode || bundlesQuery.isPending) return;
    if (bundles.length > 0) setPresetSource('bundle');
    setDidInitMode(true);
  }, [didInitMode, bundlesQuery.isPending, bundles.length]);

  // ---- Manual mode state ----
  const [printerRef, setPrinterRef] = useState<PresetRef | null>(null);
  const [processRef, setProcessRef] = useState<PresetRef | null>(null);
  const [filamentRef, setFilamentRef] = useState<PresetRef | null>(null);

  // Auto-pick defaults when presets arrive / owner filter changes.
  useEffect(() => {
    if (!presets) return;
    setPrinterRef((cur) => cur ?? pickDefaultRef(presets, 'printer', ownerFilter));
    setProcessRef((cur) => cur ?? pickDefaultRef(presets, 'process', ownerFilter));
    setFilamentRef((cur) => cur ?? pickDefaultRef(presets, 'filament', ownerFilter));
  }, [presets, ownerFilter]);

  // ---- Bundle mode state ----
  const [bundleId, setBundleId] = useState<string | null>(null);
  const selectedBundle = useMemo(
    () => bundles.find((b) => b.id === bundleId) ?? null,
    [bundles, bundleId],
  );
  const [bundlePrinterName, setBundlePrinterName] = useState<string | null>(null);
  const [bundleProcessName, setBundleProcessName] = useState<string | null>(null);
  const [bundleFilamentName, setBundleFilamentName] = useState<string | null>(null);

  // Pre-select first bundle when list lands.
  useEffect(() => {
    if (!bundleId && bundles.length > 0) setBundleId(bundles[0].id);
  }, [bundles, bundleId]);

  // Default per-slot names when bundle changes.
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

  // Per-job slicer picker. Default to global preferred_slicer when it
  // lands; user can override via the card-style picker below the
  // preset-source row. ``null`` means "use server's default" — the
  // backend's resolve_sidecar_url falls back to preferred_slicer when
  // the request body doesn't carry an override.
  const [pickedSlicer, setPickedSlicer] = useState<SlicerKind | null>(null);
  useEffect(() => {
    if (pickedSlicer != null) return;
    const preferred = settingsQuery.data?.preferred_slicer;
    if (preferred === 'orcaslicer' || preferred === 'bambu_studio') {
      setPickedSlicer(preferred);
    }
  }, [settingsQuery.data?.preferred_slicer, pickedSlicer]);

  // Bed plate — defaults to Textured PEI Plate (filament-permissive;
  // PETG / TPU don't tolerate Cool Plate, which is BS's hard-coded
  // default in the pa_pattern.3mf scaffold we wrap STLs with).
  const [bedType, setBedType] = useState<BedType>('Textured PEI Plate');

  // PA Tower spec defaults — BS GUI defaults for PA Tower preset.
  const [start, setStart] = useState<number>(0.0);
  const [end, setEnd] = useState<number>(0.1);
  const [step, setStep] = useState<number>(0.002);
  const [layerHeight, setLayerHeight] = useState<number>(0.2);
  const [nozzleDiameter, setNozzleDiameter] = useState<number>(0.4);

  // PA Line uses its own sweep range (mirrors PA Pattern's 0.0/0.08/0.005
  // — BS DDE default 0.1 is too aggressive for direct-drive Bambu
  // printers in practice). Operator can widen via the inputs.
  const [paLineStart, setPaLineStart] = useState<number>(0.0);
  const [paLineEnd, setPaLineEnd] = useState<number>(0.08);
  const [paLineStep, setPaLineStep] = useState<number>(0.005);
  const [paLinePrintNumbers, setPaLinePrintNumbers] = useState<boolean>(true);

  // Vol Speed Tower sweep — volumetric flow in mm³/s. BS/Orca dialog
  // defaults are 5 / 20 / 0.5 (calib_dlg.cpp).
  const [volStart, setVolStart] = useState<number>(5);
  const [volEnd, setVolEnd] = useState<number>(20);
  const [volStep, setVolStep] = useState<number>(0.5);

  // VFA Tower sweep — outer-wall speed in mm/s. BS/Orca dialog defaults
  // are 40 / 200 / 10 (calib_dlg.cpp).
  const [vfaStart, setVfaStart] = useState<number>(40);
  const [vfaEnd, setVfaEnd] = useState<number>(200);
  const [vfaStep, setVfaStep] = useState<number>(10);

  // Temp Tower sweep — nozzle temperature in °C, descending (start > end).
  // No step: BS fixes the band at 10 mm / 5 °C. Defaults are the BS
  // Temp_Calibration_Dlg PLA preset (230 → 190).
  const [tempStart, setTempStart] = useState<number>(230);
  const [tempEnd, setTempEnd] = useState<number>(190);

  // Retraction Tower sweep — retraction length in mm. BS/Orca dialog
  // defaults are 0 / 2 / 0.1 (calib_dlg.cpp Retraction_Test_Dlg).
  const [retractStart, setRetractStart] = useState<number>(0);
  const [retractEnd, setRetractEnd] = useState<number>(2);
  const [retractStep, setRetractStep] = useState<number>(0.1);

  // Flow Rate: two-pass test. Pass 1 = 9-block coarse (-20..+20% step 5),
  // pass 2 = 10-block fine (-9..0% step 1). The operator picks which to
  // verify and the page slices that one.
  const [flowPassN, setFlowPassN] = useState<1 | 2>(1);
  // The baseline the per-block modifiers ride on top of. For pass 1 it
  // should be the filament preset's current filament_flow_ratio (the
  // operator may also test from a fresh 1.0 instead of editing the
  // preset). For pass 2 it should be the result the operator picked
  // from pass 1. The route applies this as a filament-side override so
  // the slice physically prints at that baseline regardless of what's
  // stored in the picked filament preset.
  const [baselineFlowRatio, setBaselineFlowRatio] = useState<number>(1.0);

  const [isDownloading, setIsDownloading] = useState(false);
  const [isBaking, setIsBaking] = useState(false);

  const isPaTower = caliMode === 'pa_tower';
  const isPaPattern = caliMode === 'pa_pattern';
  const isPaLine = caliMode === 'pa_line';
  const isVolSpeed = caliMode === 'vol_speed_tower';
  const isVfa = caliMode === 'vfa_tower';
  const isTemp = caliMode === 'temp_tower';
  const isRetraction = caliMode === 'retraction_tower';
  const isFlowRate = caliMode === 'flow_rate';

  // Temp Tower: seed the start/end defaults from the selected filament's
  // type (BS picks them off its filament-type radio; we have a preset
  // picker instead, so derive the type from the picked preset). Re-runs
  // whenever the filament selection changes — a later manual edit of the
  // inputs survives until the operator picks a different filament.
  useEffect(() => {
    if (!isTemp) return;
    let filDesc = '';
    if (presetSource === 'bundle') {
      filDesc = bundleFilamentName ?? '';
    } else if (presets && filamentRef) {
      const p = presets[filamentRef.source]?.filament.find((x) => x.id === filamentRef.id);
      if (p) filDesc = `${p.filament_type ?? ''} ${p.name}`;
    }
    if (!filDesc.trim()) return;
    const d = tempDefaultsForFilament(filDesc);
    setTempStart(d.start);
    setTempEnd(d.end);
  }, [isTemp, presetSource, filamentRef, bundleFilamentName, presets]);

  const triggerBlobDownload = (blob: Blob, filename: string) => {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  // PA Pattern verification stage ignores start/end/step (the BS-shipped
  // scaffold carries pre-baked K=0..0.08 step 0.005), but the builder
  // needs nozzle_diameter to compute its line-width overrides. PA Tower
  // uses every param. Other modes get undefined and lean on builder
  // defaults.
  const buildSpec = (): Record<string, string | number | boolean> | undefined => {
    if (isPaTower) {
      return {
        start,
        end,
        step,
        layer_height: layerHeight,
        nozzle_diameter: nozzleDiameter,
      };
    }
    if (isPaPattern) {
      return { start, end, step, nozzle_diameter: nozzleDiameter };
    }
    if (isPaLine) {
      return {
        start: paLineStart,
        end: paLineEnd,
        step: paLineStep,
        print_numbers: paLinePrintNumbers,
        nozzle_diameter: nozzleDiameter,
      };
    }
    if (isVolSpeed) {
      return { start: volStart, end: volEnd, step: volStep, nozzle_diameter: nozzleDiameter };
    }
    if (isVfa) {
      return { start: vfaStart, end: vfaEnd, step: vfaStep, nozzle_diameter: nozzleDiameter };
    }
    if (isTemp) {
      return { start: tempStart, end: tempEnd, nozzle_diameter: nozzleDiameter };
    }
    if (isRetraction) {
      return { start: retractStart, end: retractEnd, step: retractStep, nozzle_diameter: nozzleDiameter };
    }
    if (isFlowRate) {
      // Per-block flow_ratio modifiers are baked into the BS-shipped 3MFs
      // (flowrate_<mod> object names); the builder only needs nozzle_diameter
      // for the geometry scale + nozzle/2 layer height. baseline_flow_ratio
      // is the multiplier the per-block overrides ride on top of — the
      // route applies it as a filament-side override so the slice prints
      // at that baseline.
      return { nozzle_diameter: nozzleDiameter, baseline_flow_ratio: baselineFlowRatio };
    }
    return undefined;
  };

  const onBakeOnly = async () => {
    const body: CalibBakeOnlyIn = { cali_mode: caliMode, spec: buildSpec(), bed_type: bedType };
    if (isFlowRate) body.pass_n = flowPassN;
    setIsBaking(true);
    try {
      const { blob, filename } = await api.bakeCalibrationForVerification(printerId, body);
      triggerBlobDownload(blob, filename);
      showToast(t('filamentCali.verifyDownload.bakeSuccess', { filename }), 'success');
    } catch (err) {
      showToast(err instanceof Error ? err.message : String(err), 'error');
    } finally {
      setIsBaking(false);
    }
  };

  // PA Line / Vol Speed / VFA / Retraction use their own start/end/step
  // state; PA Tower / PA Pattern share the shared start/end/step inputs.
  const effectiveEnd = isRetraction
    ? retractEnd
    : isVfa
      ? vfaEnd
      : isVolSpeed
        ? volEnd
        : isPaLine
          ? paLineEnd
          : end;
  const effectiveStart = isRetraction
    ? retractStart
    : isVfa
      ? vfaStart
      : isVolSpeed
        ? volStart
        : isPaLine
          ? paLineStart
          : start;
  const effectiveStep = isRetraction
    ? retractStep
    : isVfa
      ? vfaStep
      : isVolSpeed
        ? volStep
        : isPaLine
          ? paLineStep
          : step;

  // Temp Tower is the odd one out: temperature descends (start > end) and
  // there is no step. Mirror the BS Temp_Calibration_Dlg validation.
  // Flow Rate has no operator-input spec — the per-block modifiers are
  // baked into the BS-shipped 3MFs; nozzle_diameter comes from the
  // preset triplet and is always valid here.
  const specValid = isTemp
    ? tempStart <= 350 && tempEnd >= 180 && tempStart >= tempEnd + 5
    : isFlowRate
      ? baselineFlowRatio > 0 && baselineFlowRatio < 2
      : effectiveEnd > effectiveStart && effectiveStep > 0;

  const canSubmit =
    !isDownloading &&
    specValid &&
    (presetSource === 'bundle'
      ? !!selectedBundle && !!bundlePrinterName && !!bundleProcessName && !!bundleFilamentName
      : !!printerRef && !!processRef && !!filamentRef);

  const onSubmit = async () => {
    let body: CalibSliceOnlyIn;
    const spec = buildSpec();
    if (presetSource === 'bundle') {
      if (!selectedBundle || !bundlePrinterName || !bundleProcessName || !bundleFilamentName) return;
      body = {
        cali_mode: caliMode,
        spec,
        bundle: {
          bundle_id: selectedBundle.id,
          printer_name: bundlePrinterName,
          process_name: bundleProcessName,
          filament_names: [bundleFilamentName],
        },
      };
    } else {
      if (!printerRef || !processRef || !filamentRef) return;
      body = {
        cali_mode: caliMode,
        spec,
        printer_preset: printerRef,
        process_preset: processRef,
        filament_presets: [filamentRef],
      };
    }
    if (pickedSlicer) body.slicer = pickedSlicer;
    body.bed_type = bedType;
    if (isFlowRate) body.pass_n = flowPassN;

    setIsDownloading(true);
    try {
      const { blob, filename } = await api.sliceCalibrationForVerification(printerId, body);
      triggerBlobDownload(blob, filename);
      showToast(t('filamentCali.verifyDownload.success', { filename }), 'success');
      onDone();
    } catch (err) {
      showToast(err instanceof Error ? err.message : String(err), 'error');
    } finally {
      setIsDownloading(false);
    }
  };

  return (
    <div className="space-y-4">
      <h3 className="text-base font-semibold text-white">
        {t('filamentCali.verifyDownload.heading')}
      </h3>
      <p className="text-sm text-bambu-gray">{t('filamentCali.verifyDownload.intro')}</p>

      <section className="space-y-3">
        <SlicerPicker
          value={pickedSlicer}
          onChange={setPickedSlicer}
          disabled={isDownloading || isBaking}
        />

        <BedTypePicker
          value={bedType}
          onChange={setBedType}
          disabled={isDownloading || isBaking}
        />

        <PresetSourceControl
          mode={presetSource}
          onModeChange={setPresetSource}
          ownerFilter={ownerFilter}
          onOwnerFilterChange={setOwnerFilter}
          bundles={bundles}
          selectedBundleId={bundleId}
          onBundleChange={setBundleId}
          disabled={isDownloading}
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
                  disabled={isDownloading}
                  ownerFilter={ownerFilter}
                />
                <PresetDropdown
                  label={t('slice.process', 'Process profile')}
                  slot="process"
                  data={presets}
                  value={processRef}
                  onChange={setProcessRef}
                  disabled={isDownloading}
                  ownerFilter={ownerFilter}
                />
                <PresetDropdown
                  label={t('slice.filament', 'Filament profile')}
                  slot="filament"
                  data={presets}
                  value={filamentRef}
                  onChange={setFilamentRef}
                  disabled={isDownloading}
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
              disabled={isDownloading}
            />
            <BundleStringDropdown
              label={t('slice.process', 'Process profile')}
              options={selectedBundle.process}
              value={bundleProcessName}
              onChange={setBundleProcessName}
              disabled={isDownloading}
            />
            <BundleStringDropdown
              label={t('slice.filament', 'Filament profile')}
              options={selectedBundle.filament}
              value={bundleFilamentName}
              onChange={setBundleFilamentName}
              disabled={isDownloading}
            />
          </div>
        )}
      </section>

      {isPaTower && (
        <section className="space-y-2 border border-bambu-dark-tertiary rounded p-3">
          <h4 className="text-sm font-medium text-bambu-gray">
            {t('filamentCali.verifyDownload.specHeading')}
          </h4>
          <div className="grid grid-cols-2 gap-2">
            <label className="block">
              <span className="text-xs text-bambu-gray">{t('filamentCali.verifyDownload.startK')}</span>
              <input
                type="number"
                step="0.001"
                value={start}
                onChange={(e) => setStart(parseFloat(e.target.value) || 0)}
                className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
              />
            </label>
            <label className="block">
              <span className="text-xs text-bambu-gray">{t('filamentCali.verifyDownload.endK')}</span>
              <input
                type="number"
                step="0.001"
                value={end}
                onChange={(e) => setEnd(parseFloat(e.target.value) || 0)}
                className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
              />
            </label>
            <label className="block">
              <span className="text-xs text-bambu-gray">{t('filamentCali.verifyDownload.stepK')}</span>
              <input
                type="number"
                step="0.001"
                value={step}
                onChange={(e) => setStep(parseFloat(e.target.value) || 0)}
                className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
              />
            </label>
            <label className="block">
              <span className="text-xs text-bambu-gray">{t('filamentCali.verifyDownload.layerHeight')}</span>
              <input
                type="number"
                step="0.05"
                value={layerHeight}
                onChange={(e) => setLayerHeight(parseFloat(e.target.value) || 0.2)}
                className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
              />
            </label>
            <label className="block col-span-2">
              <span className="text-xs text-bambu-gray">{t('filamentCali.verifyDownload.nozzleDia')}</span>
              <input
                type="number"
                step="0.1"
                value={nozzleDiameter}
                onChange={(e) => setNozzleDiameter(parseFloat(e.target.value) || 0.4)}
                className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
              />
            </label>
          </div>
        </section>
      )}

      {isVolSpeed && (
        <section className="space-y-2 border border-bambu-dark-tertiary rounded p-3">
          <h4 className="text-sm font-medium text-bambu-gray">
            {t('filamentCali.verifyDownload.specHeading')}
          </h4>
          <div className="grid grid-cols-2 gap-2">
            <label className="block">
              <span className="text-xs text-bambu-gray">{t('filamentCali.verifyDownload.startVol')}</span>
              <input
                type="number"
                step="0.5"
                value={volStart}
                onChange={(e) => setVolStart(parseFloat(e.target.value) || 0)}
                className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
              />
            </label>
            <label className="block">
              <span className="text-xs text-bambu-gray">{t('filamentCali.verifyDownload.endVol')}</span>
              <input
                type="number"
                step="0.5"
                value={volEnd}
                onChange={(e) => setVolEnd(parseFloat(e.target.value) || 0)}
                className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
              />
            </label>
            <label className="block">
              <span className="text-xs text-bambu-gray">{t('filamentCali.verifyDownload.stepVol')}</span>
              <input
                type="number"
                step="0.1"
                value={volStep}
                onChange={(e) => setVolStep(parseFloat(e.target.value) || 0)}
                className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
              />
            </label>
            <label className="block">
              <span className="text-xs text-bambu-gray">{t('filamentCali.verifyDownload.nozzleDia')}</span>
              <input
                type="number"
                step="0.1"
                value={nozzleDiameter}
                onChange={(e) => setNozzleDiameter(parseFloat(e.target.value) || 0.4)}
                className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
              />
            </label>
          </div>
        </section>
      )}

      {isVfa && (
        <section className="space-y-2 border border-bambu-dark-tertiary rounded p-3">
          <h4 className="text-sm font-medium text-bambu-gray">
            {t('filamentCali.verifyDownload.specHeading')}
          </h4>
          <div className="grid grid-cols-3 gap-2">
            <label className="block">
              <span className="text-xs text-bambu-gray">{t('filamentCali.verifyDownload.startSpeed')}</span>
              <input
                type="number"
                step="5"
                value={vfaStart}
                onChange={(e) => setVfaStart(parseFloat(e.target.value) || 0)}
                className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
              />
            </label>
            <label className="block">
              <span className="text-xs text-bambu-gray">{t('filamentCali.verifyDownload.endSpeed')}</span>
              <input
                type="number"
                step="5"
                value={vfaEnd}
                onChange={(e) => setVfaEnd(parseFloat(e.target.value) || 0)}
                className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
              />
            </label>
            <label className="block">
              <span className="text-xs text-bambu-gray">{t('filamentCali.verifyDownload.stepSpeed')}</span>
              <input
                type="number"
                step="1"
                value={vfaStep}
                onChange={(e) => setVfaStep(parseFloat(e.target.value) || 0)}
                className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
              />
            </label>
          </div>
        </section>
      )}

      {isTemp && (
        <section className="space-y-2 border border-bambu-dark-tertiary rounded p-3">
          <h4 className="text-sm font-medium text-bambu-gray">
            {t('filamentCali.verifyDownload.specHeading')}
          </h4>
          <div className="grid grid-cols-2 gap-2">
            <label className="block">
              <span className="text-xs text-bambu-gray">{t('filamentCali.verifyDownload.startTemp')}</span>
              <input
                type="number"
                step="5"
                value={tempStart}
                onChange={(e) => setTempStart(parseFloat(e.target.value) || 0)}
                className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
              />
            </label>
            <label className="block">
              <span className="text-xs text-bambu-gray">{t('filamentCali.verifyDownload.endTemp')}</span>
              <input
                type="number"
                step="5"
                value={tempEnd}
                onChange={(e) => setTempEnd(parseFloat(e.target.value) || 0)}
                className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
              />
            </label>
          </div>
          <p className="text-xs text-bambu-gray">{t('filamentCali.verifyDownload.tempHint')}</p>
        </section>
      )}

      {isRetraction && (
        <section className="space-y-2 border border-bambu-dark-tertiary rounded p-3">
          <h4 className="text-sm font-medium text-bambu-gray">
            {t('filamentCali.verifyDownload.specHeading')}
          </h4>
          <div className="grid grid-cols-3 gap-2">
            <label className="block">
              <span className="text-xs text-bambu-gray">{t('filamentCali.verifyDownload.startLength')}</span>
              <input
                type="number"
                step="0.1"
                value={retractStart}
                onChange={(e) => setRetractStart(parseFloat(e.target.value) || 0)}
                className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
              />
            </label>
            <label className="block">
              <span className="text-xs text-bambu-gray">{t('filamentCali.verifyDownload.endLength')}</span>
              <input
                type="number"
                step="0.1"
                value={retractEnd}
                onChange={(e) => setRetractEnd(parseFloat(e.target.value) || 0)}
                className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
              />
            </label>
            <label className="block">
              <span className="text-xs text-bambu-gray">{t('filamentCali.verifyDownload.stepLength')}</span>
              <input
                type="number"
                step="0.05"
                value={retractStep}
                onChange={(e) => setRetractStep(parseFloat(e.target.value) || 0)}
                className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
              />
            </label>
          </div>
        </section>
      )}

      {isFlowRate && (
        <section className="space-y-3 border border-bambu-dark-tertiary rounded p-3">
          <h4 className="text-sm font-medium text-bambu-gray">
            {t('filamentCali.verifyDownload.specHeading')}
          </h4>
          <div className="flex items-center gap-2">
            <span className="text-xs text-bambu-gray">{t('filamentCali.verifyDownload.flowRatePass')}</span>
            <div className="inline-flex rounded border border-bambu-dark-tertiary overflow-hidden">
              <button
                type="button"
                onClick={() => setFlowPassN(1)}
                className={`px-3 py-1.5 text-sm ${
                  flowPassN === 1 ? 'bg-bambu-dark-tertiary text-white' : 'bg-bambu-dark text-bambu-gray'
                }`}
              >
                {t('filamentCali.verifyDownload.flowRatePass1')}
              </button>
              <button
                type="button"
                onClick={() => setFlowPassN(2)}
                className={`px-3 py-1.5 text-sm ${
                  flowPassN === 2 ? 'bg-bambu-dark-tertiary text-white' : 'bg-bambu-dark text-bambu-gray'
                }`}
              >
                {t('filamentCali.verifyDownload.flowRatePass2')}
              </button>
            </div>
          </div>
          <label className="block">
            <span className="text-xs text-bambu-gray">
              {flowPassN === 1
                ? t('filamentCali.verifyDownload.flowRateBaselinePass1')
                : t('filamentCali.verifyDownload.flowRateBaselinePass2')}
            </span>
            <input
              type="number"
              step="0.01"
              min="0.5"
              max="1.5"
              value={baselineFlowRatio}
              onChange={(e) => setBaselineFlowRatio(parseFloat(e.target.value) || 0)}
              className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
            />
            <span className="text-xs text-bambu-gray mt-1 block">
              {flowPassN === 1
                ? t('filamentCali.verifyDownload.flowRateBaselinePass1Hint')
                : t('filamentCali.verifyDownload.flowRateBaselinePass2Hint')}
            </span>
          </label>
        </section>
      )}

      {isPaPattern && (
        <section className="space-y-2 border border-bambu-dark-tertiary rounded p-3">
          <h4 className="text-sm font-medium text-bambu-gray">
            {t('filamentCali.verifyDownload.specHeading')}
          </h4>
          <div className="grid grid-cols-2 gap-2">
            <label className="block">
              <span className="text-xs text-bambu-gray">{t('filamentCali.verifyDownload.startK')}</span>
              <input
                type="number"
                step="0.001"
                value={start}
                onChange={(e) => setStart(parseFloat(e.target.value) || 0)}
                className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
              />
            </label>
            <label className="block">
              <span className="text-xs text-bambu-gray">{t('filamentCali.verifyDownload.endK')}</span>
              <input
                type="number"
                step="0.001"
                value={end}
                onChange={(e) => setEnd(parseFloat(e.target.value) || 0)}
                className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
              />
            </label>
            <label className="block">
              <span className="text-xs text-bambu-gray">{t('filamentCali.verifyDownload.stepK')}</span>
              <input
                type="number"
                step="0.001"
                value={step}
                onChange={(e) => setStep(parseFloat(e.target.value) || 0)}
                className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
              />
            </label>
            <label className="block">
              <span className="text-xs text-bambu-gray">{t('filamentCali.verifyDownload.nozzleDia')}</span>
              <input
                type="number"
                step="0.1"
                value={nozzleDiameter}
                onChange={(e) => setNozzleDiameter(parseFloat(e.target.value) || 0.4)}
                className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
              />
            </label>
          </div>
        </section>
      )}

      {isPaLine && (
        <section className="space-y-2 border border-bambu-dark-tertiary rounded p-3">
          <h4 className="text-sm font-medium text-bambu-gray">
            {t('filamentCali.verifyDownload.specHeading')}
          </h4>
          <div className="grid grid-cols-2 gap-2">
            <label className="block">
              <span className="text-xs text-bambu-gray">{t('filamentCali.verifyDownload.startK')}</span>
              <input
                type="number"
                step="0.001"
                value={paLineStart}
                onChange={(e) => setPaLineStart(parseFloat(e.target.value) || 0)}
                className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
              />
            </label>
            <label className="block">
              <span className="text-xs text-bambu-gray">{t('filamentCali.verifyDownload.endK')}</span>
              <input
                type="number"
                step="0.001"
                value={paLineEnd}
                onChange={(e) => setPaLineEnd(parseFloat(e.target.value) || 0)}
                className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
              />
            </label>
            <label className="block">
              <span className="text-xs text-bambu-gray">{t('filamentCali.verifyDownload.stepK')}</span>
              <input
                type="number"
                step="0.001"
                value={paLineStep}
                onChange={(e) => setPaLineStep(parseFloat(e.target.value) || 0)}
                className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
              />
            </label>
            <label className="block">
              <span className="text-xs text-bambu-gray">{t('filamentCali.verifyDownload.nozzleDia')}</span>
              <input
                type="number"
                step="0.1"
                value={nozzleDiameter}
                onChange={(e) => setNozzleDiameter(parseFloat(e.target.value) || 0.4)}
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
        </section>
      )}

      <div className="flex justify-between items-center pt-2 border-t border-bambu-dark-tertiary gap-2">
        <button
          type="button"
          onClick={onBack}
          className="px-3 py-1.5 text-sm text-bambu-gray hover:text-white"
        >
          {t('filamentCali.back')}
        </button>
        <div className="flex gap-2">
          <button
            type="button"
            onClick={onBakeOnly}
            disabled={isBaking || isDownloading || !specValid}
            title={t('filamentCali.verifyDownload.bakeTooltip')}
            className="px-3 py-2 rounded border border-bambu-dark-tertiary text-bambu-gray text-sm font-medium hover:text-white hover:border-bambu-gray disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {isBaking
              ? t('filamentCali.verifyDownload.baking')
              : t('filamentCali.verifyDownload.bake')}
          </button>
          <button
            type="button"
            onClick={onSubmit}
            disabled={!canSubmit}
            className="px-4 py-2 rounded bg-yellow-500 text-bambu-dark text-sm font-medium disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {isDownloading
              ? t('filamentCali.verifyDownload.downloading')
              : t('filamentCali.verifyDownload.download')}
          </button>
        </div>
      </div>
    </div>
  );
}
