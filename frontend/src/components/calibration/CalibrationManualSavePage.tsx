import { useState } from 'react';
import { useTranslation } from 'react-i18next';

import type { CalibrationSessionOut, ManualResultIn } from '../../api/client';

// Must match backend calibration_constants.PA_LINE_RANGE (0.0, 0.1, 0.002, 50).
const PA_LINE_RANGE = { start: 0.0, step: 0.002, count: 50 };

interface Props {
  session: CalibrationSessionOut;
  onSave: (body: ManualResultIn) => Promise<unknown>;
  onBack: () => void;
  isSubmitting: boolean;
}

export function CalibrationManualSavePage({ onSave, onBack, isSubmitting }: Props) {
  const { t } = useTranslation();
  const [lineIdx, setLineIdx] = useState<number>(Math.floor(PA_LINE_RANGE.count / 2));

  const k = PA_LINE_RANGE.start + lineIdx * PA_LINE_RANGE.step;

  return (
    <div className="space-y-4">
      <h3 className="text-base font-semibold text-white">{t('filamentCali.manualSave.heading')}</h3>
      <p className="text-sm text-bambu-gray">{t('filamentCali.manualSave.instruction')}</p>

      <label className="block">
        <span className="text-xs text-bambu-gray">{t('filamentCali.manualSave.lineIndex')}</span>
        <select
          value={lineIdx}
          onChange={(e) => setLineIdx(parseInt(e.target.value, 10))}
          className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
        >
          {Array.from({ length: PA_LINE_RANGE.count }, (_, i) => (
            <option key={i} value={i}>
              {i} (PA {(PA_LINE_RANGE.start + i * PA_LINE_RANGE.step).toFixed(4)})
            </option>
          ))}
        </select>
      </label>

      <div className="p-2 bg-bambu-dark rounded text-sm">
        <span className="text-bambu-gray">{t('filamentCali.manualSave.computedK')}: </span>
        <span className="text-white font-mono">{k.toFixed(4)}</span>
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
          onClick={() => onSave({ best_line_index: lineIdx })}
          disabled={isSubmitting}
          className="px-4 py-2 rounded bg-bambu-green text-white text-sm font-medium disabled:opacity-40"
        >
          {t('filamentCali.manualSave.save')}
        </button>
      </div>
    </div>
  );
}
