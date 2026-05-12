import { useState } from 'react';
import { useTranslation } from 'react-i18next';

import type { CalibrationSessionOut, ManualResultIn } from '../../api/client';

// Must match backend FLOW_RATE_COARSE_MODIFIERS.
const COARSE_MODS = [-20, -15, -10, -5, 0, 5, 10, 15, 20];

interface Props {
  session: CalibrationSessionOut;
  onSubmit: (body: ManualResultIn) => Promise<unknown>;
  isSubmitting: boolean;
}

export function CalibrationCoarseSavePage({ onSubmit, isSubmitting }: Props) {
  const { t } = useTranslation();
  const [mod, setMod] = useState<number>(0);
  const [skipFine, setSkipFine] = useState<boolean>(false);

  const coarseRatio = (100 + mod) / 100;

  return (
    <div className="space-y-4">
      <h3 className="text-base font-semibold text-white">{t('filamentCali.coarseSave.heading')}</h3>
      <p className="text-sm text-bambu-gray">{t('filamentCali.coarseSave.instruction')}</p>

      <label className="block">
        <span className="text-xs text-bambu-gray">{t('filamentCali.coarseSave.blockModifier')}</span>
        <select
          value={mod}
          onChange={(e) => setMod(parseInt(e.target.value, 10))}
          className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
        >
          {COARSE_MODS.map((m) => (
            <option key={m} value={m}>
              {m > 0 ? `+${m}%` : `${m}%`}
            </option>
          ))}
        </select>
      </label>

      <div className="p-2 bg-bambu-dark rounded text-sm">
        <span className="text-bambu-gray">{t('filamentCali.coarseSave.coarseRatio')}: </span>
        <span className="text-white font-mono">{coarseRatio.toFixed(4)}</span>
      </div>

      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={skipFine}
          onChange={(e) => setSkipFine(e.target.checked)}
          className="rounded"
        />
        <span className="text-bambu-gray">{t('filamentCali.coarseSave.skipFine')}</span>
      </label>

      <div className="flex justify-end pt-2 border-t border-bambu-dark-tertiary">
        <button
          type="button"
          onClick={() => onSubmit({ coarse_modifier: mod, skip_fine: skipFine })}
          disabled={isSubmitting}
          className="px-4 py-2 rounded bg-bambu-green text-white text-sm font-medium disabled:opacity-40"
        >
          {skipFine
            ? t('filamentCali.coarseSave.saveAndFinish')
            : t('filamentCali.coarseSave.continue')}
        </button>
      </div>
    </div>
  );
}
