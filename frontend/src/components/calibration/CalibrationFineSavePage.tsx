import { useState } from 'react';
import { useTranslation } from 'react-i18next';

import type { CalibrationSessionOut, ManualResultIn } from '../../api/client';

// Must match backend FLOW_RATE_FINE_MODIFIERS and the actual 10 blocks
// inside flowrate-test-pass2.3mf (flowrate_m9..m1 + flowrate_0). BS's fine
// pass refines downward only — see CalibrationWizardSavePage.cpp:1847-1851.
const FINE_MODS = [-9, -8, -7, -6, -5, -4, -3, -2, -1, 0];

interface Props {
  session: CalibrationSessionOut;
  onSubmit: (body: ManualResultIn) => Promise<unknown>;
  isSubmitting: boolean;
}

export function CalibrationFineSavePage({ session, onSubmit, isSubmitting }: Props) {
  const { t } = useTranslation();
  const [mod, setMod] = useState<number>(0);

  const coarse = session.coarse_ratio ?? 1.0;
  const fine = (coarse * (100 + mod)) / 100;

  return (
    <div className="space-y-4">
      <h3 className="text-base font-semibold text-white">{t('filamentCali.fineSave.heading')}</h3>
      <p className="text-sm text-bambu-gray">{t('filamentCali.fineSave.instruction')}</p>

      <label className="block">
        <span className="text-xs text-bambu-gray">{t('filamentCali.fineSave.fineModifier')}</span>
        <select
          value={mod}
          onChange={(e) => setMod(parseInt(e.target.value, 10))}
          className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
        >
          {FINE_MODS.map((m) => (
            <option key={m} value={m}>
              {m > 0 ? `+${m}%` : `${m}%`}
            </option>
          ))}
        </select>
      </label>

      <div className="p-2 bg-bambu-dark rounded text-sm">
        <span className="text-bambu-gray">{t('filamentCali.fineSave.fineRatio')}: </span>
        <span className="text-white font-mono">{fine.toFixed(4)}</span>
      </div>

      <div className="flex justify-end pt-2 border-t border-bambu-dark-tertiary">
        <button
          type="button"
          onClick={() => onSubmit({ fine_modifier: mod })}
          disabled={isSubmitting}
          className="px-4 py-2 rounded bg-bambu-green text-white text-sm font-medium disabled:opacity-40"
        >
          {t('filamentCali.fineSave.save')}
        </button>
      </div>
    </div>
  );
}
