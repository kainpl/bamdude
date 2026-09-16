/**
 * "What lands in MY inbox."
 *
 * The catalog comes from the server already ordered (group, then severity,
 * then key) and this tab does not re-sort it — it only cuts it into cards.
 * `GROUP_ORDER` decides which card comes first and silently skips a group the
 * server sent nothing for, so a backend that stops emitting a whole family of
 * events does not leave an empty card behind.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { RotateCcw } from 'lucide-react';
import { api, type InboxSubscriptions } from '../../api/client';
import { useToast } from '../../contexts/ToastContext';
import { Button } from '../Button';
import { Card, CardContent, CardHeader } from '../Card';
import { LoadingBlock } from '../LoadingBlock';
import { INBOX_QUERY_KEY } from '../../hooks/useInboxUnreadCount';
import { SeverityIcon } from './SeverityIcon';

// Under the `['inbox']` prefix on purpose: the `inbox_item` socket handler
// invalidates that prefix, and this tab must not drift out of it.
const KEY = [...INBOX_QUERY_KEY, 'subscriptions'] as const;
// Display order of the groups — the server already orders events inside a group.
const GROUP_ORDER = ['print', 'printer', 'filament', 'ams', 'queue', 'inventory', 'sensors'];

export function SubscriptionsTab() {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();
  const { data, isLoading } = useQuery({ queryKey: KEY, queryFn: api.getInboxSubscriptions });

  const save = useMutation({
    mutationFn: api.updateInboxSubscriptions,
    onSuccess: (next) => queryClient.setQueryData(KEY, next),
    onError: () => {
      showToast(t('notifications.center.subscriptions.saveError'), 'error');
      queryClient.invalidateQueries({ queryKey: KEY });
    },
  });

  if (isLoading || !data) {
    return <LoadingBlock label={t('common.loading')} className="h-40 text-bambu-gray" />;
  }

  const toggle = (eventType: string, on: boolean) => {
    // The wire always carries the FULL explicit list, never a delta — the
    // server stores what it is given verbatim.
    const current = data.events.filter((e) => e.subscribed).map((e) => e.event_type);
    const next = on ? [...current, eventType] : current.filter((e) => e !== eventType);
    // Optimistic: the checkbox answers immediately; a refusal re-reads the truth.
    queryClient.setQueryData<InboxSubscriptions>(KEY, {
      is_default: false,
      events: data.events.map((e) => (e.event_type === eventType ? { ...e, subscribed: on } : e)),
    });
    save.mutate(next);
  };

  const groups = GROUP_ORDER.filter((g) => data.events.some((e) => e.group === g));

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <p className="text-sm text-bambu-gray flex-1 min-w-[16rem]">{t('notifications.center.subscriptions.intro')}</p>
        <span className="text-sm text-white">
          {data.is_default
            ? t('notifications.center.subscriptions.usingDefaults')
            : t('notifications.center.subscriptions.custom')}
        </span>
        <Button
          variant="outline"
          size="sm"
          onClick={() => save.mutate(null)}
          disabled={data.is_default || save.isPending}
        >
          <RotateCcw className="w-4 h-4" />
          {t('notifications.center.subscriptions.reset')}
        </Button>
      </div>

      {groups.map((group) => (
        <Card key={group}>
          <CardHeader>
            <h2 className="text-white font-semibold">{t(`notifications.center.groups.${group}`)}</h2>
          </CardHeader>
          <CardContent className="grid gap-2 sm:grid-cols-2">
            {data.events
              .filter((e) => e.group === group)
              .map((e) => (
                <label key={e.event_type} className="flex items-center gap-2 text-sm text-bambu-gray-light">
                  <input
                    type="checkbox"
                    checked={e.subscribed}
                    onChange={(ev) => toggle(e.event_type, ev.target.checked)}
                  />
                  <SeverityIcon severity={e.severity} className="w-4 h-4" />
                  <span>{t(`notifications.center.events.${e.event_type}`)}</span>
                </label>
              ))}
          </CardContent>
        </Card>
      ))}
    </div>
  );
}
