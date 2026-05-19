import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useMutation } from '@tanstack/react-query';
import { Check, Info } from 'lucide-react';
import { api } from '../../api/client';
import type { CalibrationSessionOut } from '../../api/client';

interface Props {
  session: CalibrationSessionOut;
  // Operator-entered sweep spec from the wizard's in-memory state —
  // CalibrationSessionOut does not carry start/step. Absent on a resumed
  // session; the calculator is then hidden and the page stays tip-only.
  spec?: Record<string, number | string | boolean>;
  onClose: () => void;
  onCalibrateAnother: () => void;
}

// Result unit per tower mode — implied by cali_mode (the DB column is a
// bare Float). Kept in sync with CalibrationHistoryModal's unit map.
const TOWER_UNITS: Record<string, string> = {
  vfa_tower: 'mm/s',
  vol_speed_tower: 'mm³/s',
  temp_tower: '°C',
  retraction_tower: 'mm',
};

export function CalibrationTowerFinishPage({ session, spec, onClose, onCalibrateAnother }: Props) {
  const { t } = useTranslation();
  const [heightMm, setHeightMm] = useState<number>(0);
  const [saved, setSaved] = useState(false);

  const start = typeof spec?.start === 'number' ? spec.start : undefined;
  const step = typeof spec?.step === 'number' ? spec.step : undefined;
  const mode = session.cali_mode;
  const unit = TOWER_UNITS[mode] ?? '';
  const isVfa = mode === 'vfa_tower';
  const isTemp = mode === 'temp_tower';
  // Temp Tower has no `step` — the band is fixed at 10 mm / 5 °C.
  const canCalc = start !== undefined && (isTemp || step !== undefined);

  // Per-mode height → result:
  // - Temp: descends, banded 10 mm / 5 °C → start − floor(h/10)·5
  // - VFA: banded every 5 mm → start + floor(h/5)·step
  // - Vol Speed: continuous → start + h·step
  const h = Math.max(0, heightMm);
  const result = !canCalc
    ? undefined
    : isTemp
      ? start! - Math.floor(h / 10) * 5
      : isVfa
        ? start! + Math.floor(h / 5) * step!
        : start! + h * step!;

  const saveMutation = useMutation({
    mutationFn: () => {
      if (result === undefined) throw new Error('no spec');
      return api.submitManualResult(session.id, { tower_result: result });
    },
    onSuccess: () => setSaved(true),
  });

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <Info className="h-6 w-6 text-bambu-green" />
        <h3 className="text-base font-semibold text-white">{t('filamentCali.towerFinish.heading')}</h3>
      </div>
      <p className="text-sm text-bambu-gray">{t('filamentCali.towerFinish.body')}</p>
      <p className="text-sm text-bambu-gray">
        {t(`filamentCali.towerFinish.tip.${mode}`, {
          defaultValue: t('filamentCali.towerFinish.tip.generic'),
        })}
      </p>

      {canCalc && (
        <div className="space-y-3 border border-bambu-dark-tertiary rounded p-3">
          <h4 className="text-sm font-medium text-bambu-gray">
            {t('filamentCali.towerFinish.calcHeading')}
          </h4>

          <label className="block">
            <span className="text-xs text-bambu-gray">
              {t('filamentCali.towerFinish.measuredHeight')}
            </span>
            <input
              type="number"
              min={0}
              step={1}
              value={heightMm}
              onChange={(e) => {
                setHeightMm(parseFloat(e.target.value) || 0);
                setSaved(false);
              }}
              className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
            />
          </label>

          <div className="p-2 bg-bambu-dark rounded text-sm space-y-1">
            <div>
              <span className="text-bambu-gray">{t('filamentCali.towerFinish.formula')}: </span>
              <span className="text-white font-mono">
                {isTemp
                  ? `${start} − ⌊${h} / 10⌋ × 5`
                  : isVfa
                    ? `${start} + ⌊${h} / 5⌋ × ${step}`
                    : `${start} + ${h} × ${step}`}
              </span>
            </div>
            <div>
              <span className="text-bambu-gray">{t('filamentCali.towerFinish.result')}: </span>
              <span className="text-white font-mono font-semibold">
                {result} {unit}
              </span>
            </div>
          </div>

          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={() => saveMutation.mutate()}
              disabled={saveMutation.isPending || saved}
              className="px-3 py-1.5 rounded bg-bambu-dark-tertiary text-white text-sm font-medium hover:bg-bambu-dark disabled:opacity-40"
            >
              {t('filamentCali.towerFinish.saveResult')}
            </button>
            {saved && (
              <span className="flex items-center gap-1 text-xs text-bambu-green">
                <Check className="h-4 w-4" />
                {t('filamentCali.towerFinish.savedConfirm')}
              </span>
            )}
          </div>
        </div>
      )}

      <div className="flex justify-between pt-2 border-t border-bambu-dark-tertiary">
        <button
          type="button"
          onClick={onCalibrateAnother}
          className="px-3 py-1.5 text-sm text-bambu-gray hover:text-white"
        >
          {t('filamentCali.finish.calibrateAnother')}
        </button>
        <button
          type="button"
          onClick={onClose}
          className="px-4 py-2 rounded bg-bambu-green text-white text-sm font-medium"
        >
          {t('filamentCali.finish.close')}
        </button>
      </div>
    </div>
  );
}
