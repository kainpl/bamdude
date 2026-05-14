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

  const [isDownloading, setIsDownloading] = useState(false);
  const [isBaking, setIsBaking] = useState(false);

  const isPaTower = caliMode === 'pa_tower';
  const isPaPattern = caliMode === 'pa_pattern';

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
    return undefined;
  };

  const onBakeOnly = async () => {
    const body: CalibBakeOnlyIn = { cali_mode: caliMode, spec: buildSpec(), bed_type: bedType };
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

  const canSubmit =
    !isDownloading &&
    end > start &&
    step > 0 &&
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
            disabled={isBaking || isDownloading || (isPaTower && (end <= start || step <= 0))}
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
