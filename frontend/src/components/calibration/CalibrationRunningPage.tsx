import { useTranslation } from 'react-i18next';
import { useQuery } from '@tanstack/react-query';

import { api } from '../../api/client';
import type { CalibrationSessionOut, PrinterStatus } from '../../api/client';

interface Props {
  session: CalibrationSessionOut;
  onCancel: () => Promise<void> | void;
}

export function CalibrationRunningPage({ session, onCancel }: Props) {
  const { t } = useTranslation();

  const statusQuery = useQuery<PrinterStatus>({
    queryKey: ['printerStatus', session.printer_id],
    queryFn: () => api.getPrinterStatus(session.printer_id),
    refetchInterval: 3_000,
  });

  const progress = Math.max(0, Math.min(100, Math.round(statusQuery.data?.progress ?? 0)));
  const layer = statusQuery.data?.layer_num;
  const totalLayers = statusQuery.data?.total_layers;

  const handleCancel = async () => {
    if (window.confirm(t('filamentCali.running.cancelConfirm'))) {
      await onCancel();
    }
  };

  return (
    <div className="space-y-4">
      <h3 className="text-base font-semibold text-white">{t('filamentCali.running.heading')}</h3>

      <div className="p-3 bg-bambu-dark rounded border border-bambu-dark-tertiary space-y-2">
        <div className="flex justify-between text-sm">
          <span className="text-bambu-gray">{session.cali_mode}</span>
          <span className="text-white font-medium">{progress}%</span>
        </div>
        <div className="h-2 bg-bambu-dark-tertiary rounded overflow-hidden">
          <div
            className="h-full bg-bambu-green transition-all"
            style={{ width: `${progress}%` }}
          />
        </div>
        {layer != null && totalLayers != null && (
          <div className="text-xs text-bambu-gray">
            Layer {layer} / {totalLayers}
          </div>
        )}
        <div className="text-xs text-bambu-gray">Session #{session.id}</div>
      </div>

      <p className="text-sm text-bambu-gray">{t('filamentCali.running.inProgress')}</p>

      <div className="flex justify-end pt-2 border-t border-bambu-dark-tertiary">
        <button
          type="button"
          onClick={handleCancel}
          className="px-3 py-1.5 text-sm text-red-400 hover:text-red-300"
        >
          {t('filamentCali.running.cancel')}
        </button>
      </div>
    </div>
  );
}
