import { useTranslation } from 'react-i18next';
import { CheckCircle2 } from 'lucide-react';

import type { FilamentCalibrationOut } from '../../api/client';

interface Props {
  savedRows: FilamentCalibrationOut[];
  onCalibrateAnother: () => void;
  onClose: () => void;
}

export function CalibrationFinishPage({ savedRows, onCalibrateAnother, onClose }: Props) {
  const { t } = useTranslation();

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <CheckCircle2 className="h-6 w-6 text-bambu-green" />
        <h3 className="text-base font-semibold text-white">{t('filamentCali.finish.heading')}</h3>
      </div>

      <p className="text-sm text-bambu-gray">{t('filamentCali.finish.body')}</p>

      {savedRows.length > 0 && (
        <div className="space-y-1">
          {savedRows.map((r) => (
            <div
              key={r.id}
              className="p-2 bg-bambu-dark rounded text-sm flex justify-between"
            >
              <span className="text-white">{r.name}</span>
              {r.pa_k_value != null && (
                <span className="text-bambu-gray font-mono">K = {r.pa_k_value.toFixed(4)}</span>
              )}
              {r.flow_ratio != null && (
                <span className="text-bambu-gray font-mono">flow = {r.flow_ratio.toFixed(4)}</span>
              )}
            </div>
          ))}
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
