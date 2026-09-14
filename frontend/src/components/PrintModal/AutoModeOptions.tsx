import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { PrinterLocationSelect } from '../PrinterLocationSelect';
import { Sparkles } from 'lucide-react';
import type { AutoQueueFilamentOverride, FeedPolicy, Printer, RoutingPreview } from '../../api/client';
import type { AutoModeOptionsState } from './types';

interface AutoModeOptionsProps {
  options: AutoModeOptionsState;
  onChange: (next: AutoModeOptionsState) => void;
  printers: Printer[] | undefined;
  /** Suggested model from the sliced 3MF — pre-selects when target_model is null. */
  slicedForModel?: string | null;
  /** The target came from the file, not the operator — show it, do not offer it. */
  locked?: boolean;
  preview?: RoutingPreview;
  loading?: boolean;
  failed?: boolean;
  onRetry?: () => void;
  overrides?: AutoQueueFilamentOverride[];
  onOverridesChange?: (overrides: AutoQueueFilamentOverride[]) => void;
}

/**
 * Auto-distribute mode controls: target model + location filter +
 * force-color-match toggle. The auto-queue scheduler fans the item out
 * to any matching idle printer; backend auto-extracts target_model and
 * required filaments from the 3MF when target_model is left empty.
 */
export function AutoModeOptions({ options, onChange, printers, slicedForModel, locked = false,
  preview, loading, failed, onRetry, overrides = [], onOverridesChange }: AutoModeOptionsProps) {
  const { t } = useTranslation();

  // ⚠️ A file sliced for one model must not offer another as its target.
  // The auto-queue router filters on target_model at dispatch, so picking a
  // model the file cannot run on does not fail — it produces an item that waits
  // for a printer that will never take it, with nothing on screen saying why.
  // When the file's own model is known, that is the only honest option; the
  // empty "detect from the file" entry above already means the same thing.
  //
  // A file sliced for a model this farm does not own leaves the list empty, and
  // that is the truthful answer rather than a menu of wrong ones.
  const availableModels = useMemo(() => {
    const models = new Set<string>();
    (printers ?? []).forEach((p) => {
      if (p.model) models.add(p.model);
    });
    const all = [...models].sort();
    if (!slicedForModel) return all;
    return all.filter((m) => m.toLowerCase() === slicedForModel.toLowerCase());
  }, [printers, slicedForModel]);


  return (
    <div className="mb-4 bg-bambu-dark rounded-lg p-3 space-y-3 border border-bambu-green/30">
      <div className="flex items-center gap-2 text-sm text-white">
        <Sparkles className="w-4 h-4 text-bambu-green" />
        <span className="font-medium">{t('printModal.autoMode.title')}</span>
      </div>
      <p className="text-xs text-bambu-gray">{t('printModal.autoMode.hint')}</p>

      <div>
        <label className="text-xs text-bambu-gray block mb-1">
          {t('printModal.autoMode.targetModel')}
        </label>
        <select
          value={options.target_model ?? ''}
          onChange={(e) => onChange({ ...options, target_model: e.target.value || null })}
          disabled={locked}
          className="w-full bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded text-white px-2 py-1.5 text-sm disabled:opacity-70 disabled:cursor-not-allowed"
        >
          <option value="">
            {slicedForModel
              ? t('printModal.autoMode.autoDetectFromFile', { model: slicedForModel })
              : t('printModal.autoMode.autoDetect')}
          </option>
          {availableModels.map((model) => (
            <option key={model} value={model}>
              {model}
            </option>
          ))}
        </select>
      </div>

      <div>
        <label className="text-xs text-bambu-gray block mb-1">
          {t('printModal.autoMode.targetLocation')}
        </label>
        {/* The same component the printer form uses. These were two independent
            free-text fields, so a place had to be typed twice and matched
            exactly — a slip meant the work waited for a location no printer
            was in, with nothing to say so. */}
        <PrinterLocationSelect
          value={options.target_location_id ?? null}
          onChange={(id) => onChange({ ...options, target_location_id: id })}
        />
      </div>

      <label className="block text-xs text-bambu-gray">
        {t('filamentRouting.feedPolicy')}
        <select value={options.feed_policy ?? 'auto'}
          onChange={event => onChange({ ...options, feed_policy: event.target.value as FeedPolicy })}
          className="mt-1 w-full bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded text-white px-2 py-1.5 text-sm">
          <option value="auto">{t('filamentRouting.feedAuto')}</option>
          <option value="ams_only">{t('filamentRouting.feedAms')}</option>
          <option value="external_only">{t('filamentRouting.feedExternal')}</option>
        </select>
      </label>

      <label className="flex items-center justify-between gap-3 cursor-pointer">
        <div className="min-w-0 flex-1">
          <span className="text-sm text-white">{t('printModal.autoMode.forceColorMatch')}</span>
          <p className="text-xs text-bambu-gray">{t('printModal.autoMode.forceColorMatchDesc')}</p>
        </div>
        <button type="button" role="switch" aria-checked={options.force_color_match}
          aria-label={t('printModal.autoMode.forceColorMatch')}
          className={`relative w-10 h-5 rounded-full transition-colors flex-shrink-0 ${
            options.force_color_match ? 'bg-bambu-green' : 'bg-bambu-dark-tertiary'
          }`}
          onClick={() => onChange({ ...options, force_color_match: !options.force_color_match })}
        >
          <div
            className={`absolute top-0.5 w-4 h-4 rounded-full bg-white transition-transform ${
              options.force_color_match ? 'translate-x-5' : 'translate-x-0.5'
            }`}
          />
        </button>
      </label>

      <label className="flex items-center justify-between gap-3 cursor-pointer">
        <div className="min-w-0 flex-1">
          <span className="text-sm text-white">{t('filamentRouting.baseMaterialMatch')}</span>
          <p className="text-xs text-bambu-gray">{t('filamentRouting.baseMaterialMatchDesc')}</p>
        </div>
        <button type="button" role="switch" aria-checked={options.allow_base_material_match}
          aria-label={t('filamentRouting.baseMaterialMatch')}
          className={`relative w-10 h-5 rounded-full transition-colors flex-shrink-0 ${
            options.allow_base_material_match ? 'bg-bambu-green' : 'bg-bambu-dark-tertiary'
          }`}
          onClick={() => onChange({ ...options, allow_base_material_match: !options.allow_base_material_match })}
        >
          <div className={`absolute top-0.5 w-4 h-4 rounded-full bg-white transition-transform ${
            options.allow_base_material_match ? 'translate-x-5' : 'translate-x-0.5'
          }`} />
        </button>
      </label>

      <div aria-live="polite" className="space-y-3 text-xs">
        {loading && <p className="text-bambu-gray">{t('filamentRouting.loading')}</p>}
        {failed && <div className="text-amber-300">
          <p>{t('filamentRouting.failed')}</p>
          <button type="button" onClick={onRetry} className="underline mt-1">{t('filamentRouting.retry')}</button>
        </div>}
        {preview?.advisory_unavailable && <p className="text-amber-300">{t('filamentRouting.advisoryUnavailable')}</p>}
        {preview?.plates.map(plate => <section key={plate.requested_plate_id} className="border-t border-bambu-dark-tertiary pt-3 space-y-2">
          <p className="text-white font-medium">{t('filamentRouting.plate', { id: plate.plate_id ?? plate.requested_plate_id })}</p>
          {plate.reason && <p className="text-amber-300">{plate.reason.message}</p>}
          {plate.filaments.map(filament => {
            const override = overrides.find(value => value.slot_id === filament.slot_id);
            const update = (patch: Partial<AutoQueueFilamentOverride>) => onOverridesChange?.([
              ...overrides.filter(value => value.slot_id !== filament.slot_id),
              { ...override, slot_id: filament.slot_id, ...patch },
            ]);
            return <div key={filament.slot_id} className="rounded bg-bambu-dark-secondary p-2 space-y-2">
              <div className="flex gap-2 items-center text-white">
                <span>{t('filamentRouting.channel', { id: filament.slot_id })}</span>
                <span>{filament.type}</span>
                <span>{filament.color ?? '—'}</span>
                {filament.nozzle_id != null && <span className="text-bambu-gray">{t('filamentRouting.nozzle', { id: filament.nozzle_id })}</span>}
              </div>
              <div className="flex gap-3 flex-wrap items-center">
                <label className="text-bambu-gray">{t('filamentRouting.material')}
                  <input aria-label={t('filamentRouting.materialForChannel', { id: filament.slot_id })}
                    value={override?.type ?? filament.type} maxLength={64}
                    onChange={event => update({ type: event.target.value, tray_info_idx: null })}
                    className="ml-2 w-24 bg-bambu-dark rounded px-2 py-1 text-white" />
                </label>
                <label className="flex items-center gap-2 text-bambu-gray">{t('filamentRouting.color')}
                  <input type="color" aria-label={t('filamentRouting.colorForChannel', { id: filament.slot_id })}
                    value={`#${(override?.color ?? filament.color ?? 'FFFFFF').replace('#', '').slice(0, 6)}`}
                    onChange={event => update({ color: event.target.value })} className="w-7 h-6 bg-transparent" />
                </label>
                <label className="flex items-center gap-2 text-white">
                  <input type="checkbox" checked={override?.force_color_match ?? false}
                    onChange={event => update({ force_color_match: event.target.checked })} />
                  {t('filamentRouting.pinColor')}
                </label>
                {override && <button type="button" className="text-bambu-gray underline"
                  onClick={() => onOverridesChange?.(overrides.filter(value => value.slot_id !== filament.slot_id))}>
                  {t('filamentRouting.reset')}
                </button>}
              </div>
            </div>;
          })}
          {plate.status === 'ok' && <>
            <p className="text-bambu-gray">{t('filamentRouting.compatibilityHint')}</p>
            {plate.groups.length === 0 && <p className="text-amber-300">{t('filamentRouting.noPrinters')}</p>}
            {plate.groups.map(group => <div key={group.key} className="rounded border border-bambu-dark-tertiary p-2 space-y-1">
              <p className="text-white">{group.model} · {t('filamentRouting.nozzles', { count: group.nozzles })} · {t(`filamentRouting.ams_${group.ams}`)}</p>
              <p className={group.compatible ? 'text-bambu-green' : 'text-amber-300'}>
                {t('filamentRouting.counts', { compatible: group.compatible, total: group.total, ready: group.ready })}
              </p>
              {group.reasons.map(reason => <p key={reason.code} className="text-bambu-gray">{reason.message} ({reason.count})</p>)}
            </div>)}
          </>}
        </section>)}
        {overrides.length > 0 && <p className="text-bambu-gray">{t('filamentRouting.nextFileReview')}</p>}
      </div>
    </div>
  );
}
