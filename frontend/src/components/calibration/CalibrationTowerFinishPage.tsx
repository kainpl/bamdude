import { useTranslation } from 'react-i18next';
import { Info } from 'lucide-react';
import type { CalibrationSessionOut } from '../../api/client';

interface Props {
  session: CalibrationSessionOut;
  onClose: () => void;
  onCalibrateAnother: () => void;
}

export function CalibrationTowerFinishPage({ session, onClose, onCalibrateAnother }: Props) {
  const { t } = useTranslation();
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <Info className="h-6 w-6 text-bambu-green" />
        <h3 className="text-base font-semibold text-white">{t('filamentCali.towerFinish.heading')}</h3>
      </div>
      <p className="text-sm text-bambu-gray">{t('filamentCali.towerFinish.body')}</p>
      <p className="text-sm text-bambu-gray">
        {t(`filamentCali.towerFinish.tip.${session.cali_mode}`, {
          defaultValue: t('filamentCali.towerFinish.tip.generic'),
        })}
      </p>

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
