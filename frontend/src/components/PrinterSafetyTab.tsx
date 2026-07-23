import { useTranslation } from 'react-i18next';

import type { PrinterSettingsGetResponse, PrinterSettingsPostBody } from '../api/client';

interface Props {
  data: PrinterSettingsGetResponse;
  onSubmit: (body: PrinterSettingsPostBody) => Promise<void>;
}

// Mirrors BS's Safety Options dialog: Open Door Detection + Idle Heating
// Protection. Shown only for safety-capable models (X2D / P2S).
export function PrinterSafetyTab({ data, onSubmit }: Props) {
  const { t } = useTranslation();
  const sup = data.supports;
  const safety = data.safety;
  const idleUnavailable = safety.idle_heating === 2;

  const doorOptions = [
    { v: 0, label: t('printerSettings.openDoorMode.off') },
    { v: 1, label: t('printerSettings.openDoorMode.notification') },
    { v: 2, label: t('printerSettings.openDoorMode.pause') },
  ];

  return (
    <div className="space-y-5">
      <div>
        <div className="text-white text-sm mb-1">{t('printerSettings.safety.openDoor')}</div>
        <div className="text-xs text-bambu-gray mb-2">{t('printerSettings.safety.openDoorDesc')}</div>
        <div className="inline-flex gap-1 rounded-lg p-1 bg-bambu-dark">
          {doorOptions.map((o) => (
            <button
              key={o.v}
              type="button"
              className={`px-3 py-1 text-sm rounded-md transition-colors ${
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
            className={`flex items-start gap-3 ${idleUnavailable ? 'cursor-not-allowed opacity-60' : 'cursor-pointer'}`}
          >
            <input
              type="checkbox"
              className="mt-0.5 h-4 w-4 accent-bambu-green"
              checked={safety.idle_heating === 1}
              disabled={idleUnavailable}
              onChange={(e) => onSubmit({ action: 'safety_idle_heating', enabled: e.target.checked })}
              aria-label={t('printerSettings.safety.idleHeating')}
            />
            <div>
              <div className="text-white">{t('printerSettings.safety.idleHeating')}</div>
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
