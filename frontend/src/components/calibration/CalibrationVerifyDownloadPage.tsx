import { useEffect, useState } from 'react';
import { NumberInput } from '../NumberInput';
import { useTranslation } from 'react-i18next';
import { useQuery } from '@tanstack/react-query';

import { api } from '../../api/client';
import type { CalibBakeOnlyIn, CaliMode, CalibSliceOnlyIn } from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';
import { useToast } from '../../contexts/ToastContext';
import { tempDefaultsForFilament } from '../../utils/calibrationTemp';
import { usePresetCatalogue } from '../slicing-setup/usePresetCatalogue';
import { CalibrationSlicingSection, type CalibrationPresetChoice } from './CalibrationSlicingSection';
import { EMPTY_CALIBRATION_CHOICE } from './calibrationChoice';

interface Props {
  printerId: number;
  caliMode: CaliMode;
  onBack: () => void;
  onDone: () => void;
}

/**
 * Verification-mode download page (W2 Phase 1+).
 *
 * The shared calibration slicing section in verify mode decides the
 * slicer, the plate and the preset triplet (spec slicing-setup §7.2); the
 * per-mode spec form stays below it. The slicer-bundle path was removed
 * with the feature (#1712).
 *
 * The backend's slice-only route accepts the PresetRef triplet (see
 * ``backend/app/schemas/filament_calibration.py::CalibSliceOnlyIn``).
 */
export function CalibrationVerifyDownloadPage({ printerId, caliMode, onBack, onDone }: Props) {
  const { t } = useTranslation();
  const { showToast } = useToast();

  // The section owns the catalogue; the page reads the same listing for the
  // chosen filament's stored flow ratio and type.
  const { hasPermission } = useAuth();
  const presets = usePresetCatalogue({ enabled: hasPermission('library:upload') }).data;

  // The slicer, the bed plate and the preset triplet, as the section decided them.
  const [slicing, setSlicing] = useState<CalibrationPresetChoice>(EMPTY_CALIBRATION_CHOICE);
  const printerRef = slicing.printer;
  const processRef = slicing.process;
  const filamentRef = slicing.filament;
  const bedType = slicing.plate;

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
  // The baseline the per-block modifiers ride on top of. Kept as a string
  // so the user can type decimals freely (a numeric controlled input
  // strips the trailing dot on each keystroke and the user can never
  // finish typing "0.95"). For pass 1, auto-prefilled from the picked
  // filament preset's filament_flow_ratio when the listing exposes it
  // (local presets). For pass 2, the operator types their pass-1 result.
  const [baselineFlowRatioInput, setBaselineFlowRatioInput] = useState<string>('1.0');
  const baselineFlowRatio = parseFloat(baselineFlowRatioInput) || 0;

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

  // Flow Rate pass 1: resolve the picked filament preset's stored
  // filament_flow_ratio on demand. The /slicer/presets listing is thin
  // for cloud / standard tiers (no full JSON resolution per entry), so
  // the listing's UnifiedPreset.filament_flow_ratio is null for them;
  // this query fetches the missing piece via /slicer/filament-preset/info,
  // which resolves the full preset just like the slicer dispatch path
  // does. Cached per (source, id). Pass 2 doesn't auto-prefill — the
  // operator has to type their measured pass-1 result.
  const filamentInfoQuery = useQuery({
    queryKey: ['filament-preset-info', filamentRef?.source, filamentRef?.id],
    queryFn: () => api.getFilamentPresetInfo(filamentRef!),
    enabled: isFlowRate && flowPassN === 1 && !!filamentRef,
    staleTime: 60_000,
  });

  useEffect(() => {
    if (!isFlowRate || flowPassN !== 1) return;
    if (!filamentRef) return;
    // Prefer the listing's cached value (free, no network) when present;
    // fall back to the on-demand resolver for cloud / standard presets.
    const listingRatio = presets?.[filamentRef.source]?.filament.find(
      (x) => x.id === filamentRef.id,
    )?.filament_flow_ratio;
    const resolvedRatio = filamentInfoQuery.data?.flow_ratio ?? null;
    const ratio = (typeof listingRatio === 'number' ? listingRatio : null) ?? resolvedRatio;
    if (typeof ratio === 'number' && ratio > 0) {
      setBaselineFlowRatioInput(String(ratio));
    }
  }, [isFlowRate, flowPassN, filamentRef, presets, filamentInfoQuery.data]);

  // Temp Tower: seed the start/end defaults from the selected filament's
  // type (BS picks them off its filament-type radio; we have a preset
  // picker instead, so derive the type from the picked preset). Re-runs
  // whenever the filament selection changes — a later manual edit of the
  // inputs survives until the operator picks a different filament.
  useEffect(() => {
    if (!isTemp) return;
    let filDesc = '';
    if (presets && filamentRef) {
      const p = presets[filamentRef.source]?.filament.find((x) => x.id === filamentRef.id);
      if (p) filDesc = `${p.filament_type ?? ''} ${p.name}`;
    }
    if (!filDesc.trim()) return;
    const d = tempDefaultsForFilament(filDesc);
    setTempStart(d.start);
    setTempEnd(d.end);
  }, [isTemp, filamentRef, presets]);

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
    // Bake keeps its own readiness; the slicer it is checked against goes only
    // once known — else the server falls back to preferred_slicer (§8.2).
    const body: CalibBakeOnlyIn = {
      cali_mode: caliMode,
      spec: buildSpec(),
      bed_type: bedType,
      ...(slicing.slicer ? { slicer: slicing.slicer } : {}),
    };
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
    slicing.startBlocked === false &&
    !!printerRef && !!processRef && !!filamentRef;

  const onSubmit = async () => {
    const spec = buildSpec();
    if (!printerRef || !processRef || !filamentRef) return;
    const body: CalibSliceOnlyIn = {
      cali_mode: caliMode,
      spec,
      printer_preset: printerRef,
      process_preset: processRef,
      filament_presets: [filamentRef],
    };
    if (slicing.slicer) body.slicer = slicing.slicer;
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
        <CalibrationSlicingSection
          printerId={printerId}
          mode="verify"
          needsPresetPicker
          nozzle={nozzleDiameter}
          slot={null}
          value={slicing}
          onChange={setSlicing}
          disabled={isDownloading || isBaking}
        />
      </section>

      {isPaTower && (
        <section className="space-y-2 border border-bambu-dark-tertiary rounded p-3">
          <h4 className="text-sm font-medium text-bambu-gray">
            {t('filamentCali.verifyDownload.specHeading')}
          </h4>
          <div className="grid grid-cols-2 gap-2">
            <label className="block">
              <span className="text-xs text-bambu-gray">{t('filamentCali.verifyDownload.startK')}</span>
              <NumberInput
                step="0.001"
                value={start}
                onChange={(v) => setStart(v)}
                fallback={0}
                className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
              />
            </label>
            <label className="block">
              <span className="text-xs text-bambu-gray">{t('filamentCali.verifyDownload.endK')}</span>
              <NumberInput
                step="0.001"
                value={end}
                onChange={(v) => setEnd(v)}
                fallback={0}
                className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
              />
            </label>
            <label className="block">
              <span className="text-xs text-bambu-gray">{t('filamentCali.verifyDownload.stepK')}</span>
              <NumberInput
                step="0.001"
                value={step}
                onChange={(v) => setStep(v)}
                fallback={0}
                className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
              />
            </label>
            <label className="block">
              <span className="text-xs text-bambu-gray">{t('filamentCali.verifyDownload.layerHeight')}</span>
              <NumberInput
                step="0.05"
                value={layerHeight}
                onChange={(v) => setLayerHeight(v)}
                fallback={0.2}
                className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
              />
            </label>
            <label className="block col-span-2">
              <span className="text-xs text-bambu-gray">{t('filamentCali.verifyDownload.nozzleDia')}</span>
              <NumberInput
                step="0.1"
                value={nozzleDiameter}
                onChange={(v) => setNozzleDiameter(v)}
                fallback={0.4}
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
              <NumberInput
                step="0.5"
                value={volStart}
                onChange={(v) => setVolStart(v)}
                fallback={0}
                className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
              />
            </label>
            <label className="block">
              <span className="text-xs text-bambu-gray">{t('filamentCali.verifyDownload.endVol')}</span>
              <NumberInput
                step="0.5"
                value={volEnd}
                onChange={(v) => setVolEnd(v)}
                fallback={0}
                className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
              />
            </label>
            <label className="block">
              <span className="text-xs text-bambu-gray">{t('filamentCali.verifyDownload.stepVol')}</span>
              <NumberInput
                step="0.1"
                value={volStep}
                onChange={(v) => setVolStep(v)}
                fallback={0}
                className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
              />
            </label>
            <label className="block">
              <span className="text-xs text-bambu-gray">{t('filamentCali.verifyDownload.nozzleDia')}</span>
              <NumberInput
                step="0.1"
                value={nozzleDiameter}
                onChange={(v) => setNozzleDiameter(v)}
                fallback={0.4}
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
              <NumberInput
                step="5"
                value={vfaStart}
                onChange={(v) => setVfaStart(v)}
                fallback={0}
                className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
              />
            </label>
            <label className="block">
              <span className="text-xs text-bambu-gray">{t('filamentCali.verifyDownload.endSpeed')}</span>
              <NumberInput
                step="5"
                value={vfaEnd}
                onChange={(v) => setVfaEnd(v)}
                fallback={0}
                className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
              />
            </label>
            <label className="block">
              <span className="text-xs text-bambu-gray">{t('filamentCali.verifyDownload.stepSpeed')}</span>
              <NumberInput
                step="1"
                value={vfaStep}
                onChange={(v) => setVfaStep(v)}
                fallback={0}
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
              <NumberInput
                step="5"
                value={tempStart}
                onChange={(v) => setTempStart(v)}
                fallback={0}
                className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
              />
            </label>
            <label className="block">
              <span className="text-xs text-bambu-gray">{t('filamentCali.verifyDownload.endTemp')}</span>
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
              <NumberInput
                step="0.1"
                value={retractStart}
                onChange={(v) => setRetractStart(v)}
                fallback={0}
                className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
              />
            </label>
            <label className="block">
              <span className="text-xs text-bambu-gray">{t('filamentCali.verifyDownload.endLength')}</span>
              <NumberInput
                step="0.1"
                value={retractEnd}
                onChange={(v) => setRetractEnd(v)}
                fallback={0}
                className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
              />
            </label>
            <label className="block">
              <span className="text-xs text-bambu-gray">{t('filamentCali.verifyDownload.stepLength')}</span>
              <NumberInput
                step="0.05"
                value={retractStep}
                onChange={(v) => setRetractStep(v)}
                fallback={0}
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
              type="text"
              inputMode="decimal"
              value={baselineFlowRatioInput}
              onChange={(e) => setBaselineFlowRatioInput(e.target.value.replace(',', '.'))}
              placeholder="1.0"
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
              <NumberInput
                step="0.001"
                value={start}
                onChange={(v) => setStart(v)}
                fallback={0}
                className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
              />
            </label>
            <label className="block">
              <span className="text-xs text-bambu-gray">{t('filamentCali.verifyDownload.endK')}</span>
              <NumberInput
                step="0.001"
                value={end}
                onChange={(v) => setEnd(v)}
                fallback={0}
                className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
              />
            </label>
            <label className="block">
              <span className="text-xs text-bambu-gray">{t('filamentCali.verifyDownload.stepK')}</span>
              <NumberInput
                step="0.001"
                value={step}
                onChange={(v) => setStep(v)}
                fallback={0}
                className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
              />
            </label>
            <label className="block">
              <span className="text-xs text-bambu-gray">{t('filamentCali.verifyDownload.nozzleDia')}</span>
              <NumberInput
                step="0.1"
                value={nozzleDiameter}
                onChange={(v) => setNozzleDiameter(v)}
                fallback={0.4}
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
              <NumberInput
                step="0.001"
                value={paLineStart}
                onChange={(v) => setPaLineStart(v)}
                fallback={0}
                className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
              />
            </label>
            <label className="block">
              <span className="text-xs text-bambu-gray">{t('filamentCali.verifyDownload.endK')}</span>
              <NumberInput
                step="0.001"
                value={paLineEnd}
                onChange={(v) => setPaLineEnd(v)}
                fallback={0}
                className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
              />
            </label>
            <label className="block">
              <span className="text-xs text-bambu-gray">{t('filamentCali.verifyDownload.stepK')}</span>
              <NumberInput
                step="0.001"
                value={paLineStep}
                onChange={(v) => setPaLineStep(v)}
                fallback={0}
                className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
              />
            </label>
            <label className="block">
              <span className="text-xs text-bambu-gray">{t('filamentCali.verifyDownload.nozzleDia')}</span>
              <NumberInput
                step="0.1"
                value={nozzleDiameter}
                onChange={(v) => setNozzleDiameter(v)}
                fallback={0.4}
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
