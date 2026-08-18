import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { PrinterLocationSelect } from '../PrinterLocationSelect';
import { Sparkles } from 'lucide-react';
import type { Printer } from '../../api/client';
import type { AutoModeOptionsState } from './types';

interface AutoModeOptionsProps {
  options: AutoModeOptionsState;
  onChange: (next: AutoModeOptionsState) => void;
  printers: Printer[] | undefined;
  /** Suggested model from the sliced 3MF — pre-selects when target_model is null. */
  slicedForModel?: string | null;
  /** The target came from the file, not the operator — show it, do not offer it. */
  locked?: boolean;
}

/**
 * Auto-distribute mode controls: target model + location filter +
 * force-color-match toggle. The auto-queue scheduler fans the item out
 * to any matching idle printer; backend auto-extracts target_model and
 * required filaments from the 3MF when target_model is left empty.
 */
export function AutoModeOptions({ options, onChange, printers, slicedForModel, locked = false }: AutoModeOptionsProps) {
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

      <label className="flex items-center justify-between gap-3 cursor-pointer">
        <div className="min-w-0 flex-1">
          <span className="text-sm text-white">{t('printModal.autoMode.forceColorMatch')}</span>
          <p className="text-xs text-bambu-gray">{t('printModal.autoMode.forceColorMatchDesc')}</p>
        </div>
        <div
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
        </div>
      </label>
    </div>
  );
}
