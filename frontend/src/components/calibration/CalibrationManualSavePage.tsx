import { useState } from 'react';
import { useTranslation } from 'react-i18next';

import type { CalibrationSessionOut, ManualResultIn } from '../../api/client';

// Defaults mirror BS / Orca PA Tower wizard (Plater.cpp::_calib_pa_tower).
// `compute_pa_k(idx)` on the backend returns `start + idx * step`, which
// matches Orca's documented Tower formula
// `K = Start + (Step × measured_height_mm)` — so passing the measured
// height in mm as `best_line_index` produces the right K.
// Source: https://www.orcaslicer.com/wiki/calibration/pressure_advance_calib.html#tower-method
const PA_TOWER_RANGE = { start: 0.0, step: 0.002, maxHeightMm: 50 };

// PA Line wizard ships a fixed 50-line sweep at K=0..0.098 (step 0.002).
const PA_LINE_RANGE = { start: 0.0, step: 0.002, count: 50 };

interface Props {
  session: CalibrationSessionOut;
  onSave: (body: ManualResultIn) => Promise<unknown>;
  onBack: () => void;
  isSubmitting: boolean;
}

export function CalibrationManualSavePage({ session, onSave, onBack, isSubmitting }: Props) {
  if (session.cali_mode === 'pa_tower') {
    return <PATowerSave onSave={onSave} onBack={onBack} isSubmitting={isSubmitting} />;
  }

  return <PALineSave onSave={onSave} onBack={onBack} isSubmitting={isSubmitting} />;
}

function PATowerSave({ onSave, onBack, isSubmitting }: Omit<Props, 'session'>) {
  const { t } = useTranslation();
  const [heightMm, setHeightMm] = useState<number>(8);

  const clamped = Math.max(0, Math.min(PA_TOWER_RANGE.maxHeightMm, Math.floor(heightMm)));
  const k = PA_TOWER_RANGE.start + clamped * PA_TOWER_RANGE.step;

  return (
    <div className="space-y-4">
      <h3 className="text-base font-semibold text-white">{t('filamentCali.paTowerSave.heading')}</h3>
      <p className="text-sm text-bambu-gray whitespace-pre-line">
        {t('filamentCali.paTowerSave.instruction')}
      </p>

      <label className="block">
        <span className="text-xs text-bambu-gray">{t('filamentCali.paTowerSave.measuredHeight')}</span>
        <input
          type="number"
          min={0}
          max={PA_TOWER_RANGE.maxHeightMm}
          step={1}
          value={heightMm}
          onChange={(e) => setHeightMm(parseInt(e.target.value, 10) || 0)}
          className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
        />
      </label>

      <div className="p-2 bg-bambu-dark rounded text-sm space-y-1">
        <div>
          <span className="text-bambu-gray">{t('filamentCali.paTowerSave.formula')}: </span>
          <span className="text-white font-mono">
            {PA_TOWER_RANGE.start} + {PA_TOWER_RANGE.step} × {clamped} ={' '}
            <span className="font-semibold">{k.toFixed(4)}</span>
          </span>
        </div>
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
          onClick={() => onSave({ best_line_index: clamped })}
          disabled={isSubmitting}
          className="px-4 py-2 rounded bg-bambu-green text-white text-sm font-medium disabled:opacity-40"
        >
          {t('filamentCali.paTowerSave.save')}
        </button>
      </div>
    </div>
  );
}

function PALineSave({ onSave, onBack, isSubmitting }: Omit<Props, 'session'>) {
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
