import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';

interface Props {
  values: Record<string, number | undefined>;
  onSave: (patch: Record<string, number>) => void;
  saving: boolean;
}

/**
 * How long each kind of measurement is kept.
 *
 * Two of these windows have been settable through the API since they were added
 * and have never had a control, so in practice they have been frozen at their
 * defaults. All four are here together because "how long do we keep
 * measurements" is one question, and answering it in four places is how two of
 * them got forgotten.
 */
const FIELDS = [
  { key: 'ams_history_retention_days', labelKey: 'settings.retention.ams' },
  { key: 'printer_sensor_history_retention_days', labelKey: 'settings.retention.printerSensors' },
  { key: 'plug_power_history_retention_days', labelKey: 'settings.retention.plugPower' },
  { key: 'sensor_history_retention_days', labelKey: 'settings.retention.sensors' },
] as const;

const DEFAULT_DAYS = 30;

export function RetentionCard({ values, onSave, saving }: Props) {
  const { t } = useTranslation();
  const [draft, setDraft] = useState<Record<string, number>>({});

  useEffect(() => {
    setDraft(
      Object.fromEntries(FIELDS.map((f) => [f.key, values[f.key] ?? DEFAULT_DAYS])) as Record<string, number>,
    );
  }, [values]);

  return (
    <div className="bg-bambu-dark-secondary rounded-xl">
      <h3 className="text-white mb-1">{t('settings.retention.title')}</h3>
      <p className="text-xs text-bambu-gray mb-3">{t('settings.retention.help')}</p>

      <div className="space-y-2">
        {FIELDS.map((field) => (
          <div key={field.key} className="flex items-center justify-between gap-3">
            <label className="text-sm text-bambu-gray" htmlFor={field.key}>
              {t(field.labelKey)}
            </label>
            <input
              id={field.key}
              type="number"
              min={1}
              max={365}
              className="w-24 px-2 py-1 bg-bambu-dark border border-bambu-dark-tertiary rounded text-white text-right"
              value={draft[field.key] ?? DEFAULT_DAYS}
              onChange={(e) => setDraft({ ...draft, [field.key]: Number(e.target.value) })}
            />
          </div>
        ))}
      </div>

      <button
        type="button"
        className="mt-3 px-3 py-1.5 bg-bambu-green rounded-lg text-white disabled:opacity-50"
        disabled={saving}
        /* Every field, not only the edited one: a patch carrying a single key
           would be fine today, but the whole point of this card is that these
           four are one answer, and sending them together keeps them that way. */
        onClick={() => onSave(draft)}
      >
        {t('common.save')}
      </button>
    </div>
  );
}
