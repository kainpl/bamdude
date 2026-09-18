import { useTranslation } from 'react-i18next';
import { Loader2 } from 'lucide-react';
import { Toggle } from './Toggle';
import { DESCRIPTION_KEYS, useEventLabel, useProviderEvents } from './providerEvents';

/**
 * Every event a notification provider can subscribe to, as switches.
 *
 * ⚠️ The LIST is never written here. `GET /notifications/events` returns one row
 * per flag in `PROVIDER_EVENT_DEFAULTS`, grouped as the notification centre
 * groups them, and this component renders whatever arrives. That is the whole
 * point: the dialog and the card each used to keep their own list, and they
 * drifted — the dialog offered 18 of the 34 flags, and six of the sixteen it
 * hid default to ON, so a new provider sent events nobody had been shown. The
 * wording resolves through three tiers in `providerEvents.ts`, so a flag this
 * codebase has never heard of still renders under a readable label rather than
 * being dropped.
 */
interface ProviderEventTogglesProps {
  /** Flag → subscribed. A flag missing from this map renders as off. */
  value: Record<string, boolean>;
  onChange: (flag: string, on: boolean) => void;
  /** Extra control rendered under a specific flag, e.g. the progress duration floor. */
  extras?: Record<string, React.ReactNode>;
  /** Two columns in the dialog (it is wide), one on the card. */
  columns?: 1 | 2;
}

export function ProviderEventToggles({ value, onChange, extras, columns = 1 }: ProviderEventTogglesProps) {
  const { t } = useTranslation();
  const { data: events, isLoading, isError } = useProviderEvents();
  const label = useEventLabel();

  if (isLoading) {
    return (
      <div className="flex items-center gap-2 p-3 text-sm text-bambu-gray">
        <Loader2 className="w-4 h-4 animate-spin" />
        {t('common.loading')}
      </div>
    );
  }

  if (isError || !events) {
    return (
      <div className="p-3 rounded-lg bg-red-100 dark:bg-red-500/20 border border-red-300 dark:border-red-500/50 text-sm text-red-700 dark:text-red-400">
        {t('notifications.eventsLoadFailed')}
      </div>
    );
  }

  // Groups in the order the API sent them — it is the catalog's display order,
  // and the backend test pins that a group is never split across the list.
  const groups: string[] = [];
  for (const event of events) {
    if (!groups.includes(event.group)) groups.push(event.group);
  }

  return (
    <div className="space-y-3">
      {groups.map((group) => (
        <div key={group} className="space-y-2 p-3 bg-bambu-dark rounded-lg">
          <p className="text-xs text-bambu-gray uppercase tracking-wide">
            {t(`notifications.center.groups.${group}`, { defaultValue: group })}
          </p>
          <div className={columns === 2 ? 'grid grid-cols-1 sm:grid-cols-2 gap-x-4 gap-y-2' : 'space-y-2'}>
            {events
              .filter((event) => event.group === group)
              .map((event) => {
                const description = DESCRIPTION_KEYS[event.flag];
                return (
                  <div key={event.flag}>
                    <div className="flex items-center justify-between gap-2">
                      <div className="min-w-0">
                        <p className="text-sm text-white">{label(event)}</p>
                        {description && (
                          <p className="text-xs text-bambu-gray">{t(`notifications.${description}`)}</p>
                        )}
                      </div>
                      <Toggle
                        checked={value[event.flag] ?? false}
                        onChange={(checked) => onChange(event.flag, checked)}
                        label={label(event)}
                      />
                    </div>
                    {extras?.[event.flag]}
                  </div>
                );
              })}
          </div>
        </div>
      ))}
    </div>
  );
}
