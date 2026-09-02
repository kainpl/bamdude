import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { X } from 'lucide-react';

import { api } from '../../api/client';
import type { DeviceSettings, DeviceSettingsTarget } from '../../api/client';
import { REPORTING_STATUS_KEY, reportingStatus } from '../../utils/reportingStatus';
import type { AppliedEntry } from '../../utils/reportingStatus';
import { Button } from '../Button';

interface Props {
  ieee: string;
  deviceName: string;
  onClose: () => void;
}

const FIELDS: { name: keyof DeviceSettingsTarget; labelKey: string }[] = [
  { name: 'min_interval', labelKey: 'settings.zigbee.reporting.minInterval' },
  { name: 'max_interval', labelKey: 'settings.zigbee.reporting.maxInterval' },
  { name: 'reportable_change', labelKey: 'settings.zigbee.reporting.change' },
];

/**
 * One dialog for both device classes, because there is one endpoint.
 *
 * It never knows in advance which targets a device has or which of the three
 * fields each allows: `editable` says so, and a relay says one. Hardcoding
 * either would be a second copy of knowledge the backend already holds.
 */
export function DeviceReportingModal({ ieee, deviceName, onClose }: Props) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<Record<string, DeviceSettingsTarget>>({});
  const [poll, setPoll] = useState<number | null>(null);
  const [stale, setStale] = useState<number | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  const { data, error, isLoading } = useQuery({
    queryKey: ['device-settings', ieee],
    queryFn: () => api.getDeviceSettings(ieee),
    retry: false,
  });

  useEffect(() => {
    if (!data) return;
    setDraft(data.desired);
    setPoll(data.poll_seconds);
    setStale(data.stale_after_seconds);
  }, [data]);

  const settled = (fresh: DeviceSettings) => {
    queryClient.setQueryData(['device-settings', ieee], fresh);
    queryClient.invalidateQueries({ queryKey: ['zigbee-sensors'] });
    // "Applied" and "saved, on its way" are different outcomes and the operator
    // is owed the difference: for a battery device the second is the ordinary
    // one, and calling it a failure would report normality as a fault.
    const reached = Object.values(fresh.applied).some((entry) => entry.state === 'ok');
    setNote(reached ? t('settings.zigbee.reporting.savedAwake') : t('settings.zigbee.reporting.savedAsleep'));
    setProblem(null);
  };

  const save = useMutation({
    mutationFn: () =>
      api.updateDeviceSettings(ieee, {
        reporting: draft,
        ...(data?.poll_supported && poll != null ? { poll_seconds: poll } : {}),
        ...(stale != null ? { stale_after_seconds: stale } : {}),
      }),
    onSuccess: settled,
    // Verbatim: these sentences were written to be acted on, and "validation
    // failed" throws exactly that away.
    onError: (e: Error) => setProblem(e.message),
  });

  const reset = useMutation({
    mutationFn: () => api.clearDeviceSettings(ieee),
    onSuccess: settled,
    onError: (e: Error) => setProblem(e.message),
  });

  const settings = data;

  return (
    <div className="fixed inset-0 bg-black/50 backdrop-blur-sm flex items-center justify-center z-50" onClick={onClose}>
      <div
        className="bg-bambu-dark-secondary rounded-xl w-full max-w-2xl max-h-[90vh] overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-4 py-4 border-b border-bambu-dark-tertiary">
          <div>
            <h2 className="text-lg font-semibold text-white">{t('settings.zigbee.reporting.title')}</h2>
            <p className="text-sm text-bambu-gray">{settings?.name || deviceName}</p>
          </div>
          <button onClick={onClose} className="p-2 text-bambu-gray" aria-label={t('common.close')}>
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="p-6 space-y-5 overflow-y-auto max-h-[calc(90vh-140px)]">
          {/* A device off the mesh is an ordinary state since sensors are listed
              from our rows -- so it gets the reason, not an empty form. */}
          {error ? (
            <p className="text-sm text-amber-600 dark:text-amber-400">{(error as Error).message}</p>
          ) : isLoading || !settings ? (
            <div className="h-32" />
          ) : (
            <>
              {Object.keys(settings.desired).map((key) => (
                <Target
                  key={key}
                  name={key}
                  unit={settings.units[key] ?? ''}
                  editable={settings.editable[key] ?? []}
                  value={draft[key] ?? settings.desired[key]}
                  applied={settings.applied[key]}
                  onChange={(next) => setDraft({ ...draft, [key]: next })}
                />
              ))}

              <div className="border-t border-bambu-dark-tertiary pt-4 space-y-3">
                {settings.poll_supported ? (
                  <Seconds
                    id="poll"
                    label={t('settings.zigbee.reporting.poll')}
                    value={poll ?? settings.poll_seconds}
                    onChange={setPoll}
                  />
                ) : (
                  // Explained, not hidden: a field that vanishes leaves the
                  // reader wondering where it went.
                  <p className="text-xs text-bambu-gray">{t('settings.zigbee.reporting.pollUnsupported')}</p>
                )}
                <Seconds
                  id="stale"
                  label={t('settings.zigbee.reporting.staleAfter')}
                  value={stale ?? settings.stale_after_seconds}
                  onChange={setStale}
                />
              </div>

              {note && <p className="text-sm text-bambu-green">{note}</p>}
              {problem && <p className="text-sm text-status-error">{problem}</p>}
            </>
          )}
        </div>

        {settings && !error && (
          <div className="flex items-center justify-between gap-2 px-4 py-4 border-t border-bambu-dark-tertiary">
            <Button variant="secondary" onClick={() => reset.mutate()} disabled={reset.isPending}>
              {t('settings.zigbee.reporting.reset')}
            </Button>
            <Button onClick={() => save.mutate()} disabled={save.isPending}>
              {t('common.save')}
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}

function Target({
  name,
  unit,
  editable,
  value,
  applied,
  onChange,
}: {
  name: string;
  unit: string;
  editable: string[];
  value: DeviceSettingsTarget;
  applied: AppliedEntry | undefined;
  onChange: (next: DeviceSettingsTarget) => void;
}) {
  const { t } = useTranslation();
  const status = applied ? reportingStatus(applied) : 'unknown';
  // Only the intervals are comparable: `actual` is in the device's raw units
  // and `values` is in the operator's, and seconds are seconds in both. For the
  // change, the mismatch is named without a number rather than printing a raw
  // count beside a label reading °C.
  const storedMax = applied?.actual?.max_interval;

  return (
    <div className="bg-bambu-dark rounded-lg p-3">
      <div className="flex items-center justify-between gap-3 mb-2">
        <span className="text-white text-sm">
          {t(`settings.zigbee.measurement.${name}`, { defaultValue: name })}
        </span>
        <span
          className={`text-xs ${status === 'mismatch' ? 'text-amber-600 dark:text-amber-400' : 'text-bambu-gray'}`}
        >
          {t(REPORTING_STATUS_KEY[status])}
          {status === 'mismatch' &&
            ' · ' +
              (storedMax != null
                ? t('settings.zigbee.reporting.storedInstead', { value: storedMax })
                : t('settings.zigbee.reporting.storedDifferent'))}
        </span>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
        {FIELDS.filter((field) => editable.includes(field.name)).map((field) => (
          <label key={field.name} className="text-xs text-bambu-gray">
            {t(field.labelKey)}
            <div className="flex items-center gap-1">
              <input
                type="number"
                aria-label={t(field.labelKey)}
                className="w-full px-2 py-1 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded text-white"
                value={value[field.name]}
                onChange={(e) => onChange({ ...value, [field.name]: Number(e.target.value) })}
              />
              <span className="text-xs text-bambu-gray shrink-0">
                {field.name === 'reportable_change' ? unit : t('settings.zigbee.reporting.seconds')}
              </span>
            </div>
          </label>
        ))}
      </div>
    </div>
  );
}

function Seconds({
  id,
  label,
  value,
  onChange,
}: {
  id: string;
  label: string;
  value: number;
  onChange: (next: number) => void;
}) {
  const { t } = useTranslation();
  return (
    <label className="flex items-center justify-between gap-3 text-sm text-bambu-gray" htmlFor={id}>
      {label}
      <span className="flex items-center gap-2">
        <input
          id={id}
          type="number"
          aria-label={label}
          className="w-28 px-2 py-1 bg-bambu-dark border border-bambu-dark-tertiary rounded text-white text-right"
          value={value}
          onChange={(e) => onChange(Number(e.target.value))}
        />
        <span className="text-xs">
          {t('settings.zigbee.reporting.seconds')}{' '}
          {t('settings.zigbee.reporting.minutesHint', { minutes: Math.round(value / 60) })}
        </span>
      </span>
    </label>
  );
}
