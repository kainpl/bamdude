import { useTranslation } from 'react-i18next';
import { Loader2 } from 'lucide-react';

import type { PrinterSettingsGetResponse, PrinterSettingsPostBody } from '../api/client';

interface Props {
  data: PrinterSettingsGetResponse;
  onSubmit: (body: PrinterSettingsPostBody) => Promise<void>;
  isPending: (flag: string) => boolean;
}

// Mirrors BS's Safety Options dialog: Open Door Detection + Idle Heating
// Protection. Shown only for safety-capable models (X2D / P2S).
export function PrinterSafetyTab({ data, onSubmit, isPending }: Props) {
  const { t } = useTranslation();
  const sup = data.supports;
  const safety = data.safety;
  const idleUnavailable = safety.idle_heating === 2;
  const doorPending = isPending('open_door');
  const idlePending = isPending('idle_heating');

  const doorOptions = [
    { v: 0, label: t('printerSettings.openDoorMode.off') },
    { v: 1, label: t('printerSettings.openDoorMode.notification') },
    { v: 2, label: t('printerSettings.openDoorMode.pause') },
  ];

  return (
    <div className="space-y-5">
      <div>
        <div className="text-white text-sm mb-1 flex items-center gap-2">
          {t('printerSettings.safety.openDoor')}
          {doorPending && <Loader2 className="w-3.5 h-3.5 animate-spin text-bambu-gray" />}
        </div>
        <div className="text-xs text-bambu-gray mb-2">{t('printerSettings.safety.openDoorDesc')}</div>
        <div className={`inline-flex gap-1 rounded-lg p-1 bg-bambu-dark ${doorPending ? 'opacity-70' : ''}`}>
          {doorOptions.map((o) => (
            <button
              key={o.v}
              type="button"
              disabled={doorPending}
              className={`px-3 py-1 text-sm rounded-md transition-colors ${doorPending ? 'cursor-wait' : ''} ${
                (safety.open_door ?? 0) === o.v ? 'bg-bambu-green text-white' : 'text-bambu-gray hover:text-white'
              }`}
              onClick={() => onSubmit({ action: 'safety_open_door', value: o.v })}
            >
              {o.label}
            </button>
          ))}
        </div>
      </div>

      {sup.idle_heating && (
        <div className="border-t border-bambu-dark-tertiary pt-4">
          <label
            className={`flex items-start gap-3 ${
              idleUnavailable || idlePending ? 'cursor-wait opacity-60' : 'cursor-pointer'
            }`}
          >
            <input
              type="checkbox"
              className="mt-0.5 h-4 w-4 accent-bambu-green"
              checked={safety.idle_heating === 1}
              disabled={idleUnavailable || idlePending}
              onChange={(e) => onSubmit({ action: 'safety_idle_heating', enabled: e.target.checked })}
              aria-label={t('printerSettings.safety.idleHeating')}
            />
            <div className="flex-1">
              <div className="text-white flex items-center gap-2">
                {t('printerSettings.safety.idleHeating')}
                {idlePending && <Loader2 className="w-3.5 h-3.5 animate-spin text-bambu-gray" />}
              </div>
              <div className="text-xs text-bambu-gray">
                {idleUnavailable
                  ? t('printerSettings.safety.idleHeatingUnavailable')
                  : t('printerSettings.safety.idleHeatingDesc')}
              </div>
            </div>
          </label>
        </div>
      )}
    </div>
  );
}
