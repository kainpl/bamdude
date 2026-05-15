import { useTranslation } from 'react-i18next';
import { AlertCircle } from 'lucide-react';

import type { CalibrationSessionOut } from '../../api/client';

interface Props {
  session: CalibrationSessionOut;
  onResume: () => void;
  onDiscard: () => Promise<void> | void;
}

export function ResumeBanner({ session, onResume, onDiscard }: Props) {
  const { t } = useTranslation();
  const date = new Date(session.created_at).toLocaleString();

  const handleDiscard = async () => {
    if (window.confirm(t('filamentCali.resume.discardConfirm'))) {
      await onDiscard();
    }
  };

  const body =
    session.cali_mode === 'flow_rate'
      ? t('filamentCali.resume.bodyFlow', {
          filament: '',
          stage: session.stage === 2 ? 'fine' : 'coarse',
          date,
        })
      : t('filamentCali.resume.body', {
          filament: '',
          mode: t(`filamentCali.modeShort.${session.cali_mode}`, { defaultValue: session.cali_mode }),
          date,
        });

  return (
    <div className="p-3 bg-bambu-dark-tertiary border border-yellow-700/50 rounded-lg flex items-start gap-3">
      <AlertCircle className="h-5 w-5 text-yellow-500 mt-0.5 shrink-0" />
      <div className="flex-1">
        <div className="text-sm font-medium text-white">{t('filamentCali.resume.title')}</div>
        <div className="text-xs text-bambu-gray mt-0.5">{body}</div>
        <div className="mt-2 flex gap-2">
          <button
            onClick={onResume}
            className="px-2 py-1 text-xs rounded bg-bambu-green text-white"
          >
            {t('filamentCali.resume.resume')}
          </button>
          <button
            onClick={handleDiscard}
            className="px-2 py-1 text-xs rounded border border-bambu-dark-tertiary text-bambu-gray hover:text-white"
          >
            {t('filamentCali.resume.discard')}
          </button>
        </div>
      </div>
    </div>
  );
}
