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
// This list decides ORDER only, never membership: see `groups` below.
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

  // The groups we name come first, in our order; anything else the server sent
  // is appended in its own order. A group this file does not know about must
  // never be DROPPED — that would leave a user holding subscriptions they can
  // neither see nor edit, and the backend catalog grows without asking us.
  const sent = new Set(data.events.map((e) => e.group));
  const groups = [
    ...GROUP_ORDER.filter((g) => sent.has(g)),
    ...[...sent].filter((g) => !GROUP_ORDER.includes(g)),
  ];

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
            {/* An unnamed group falls back to its own key rather than to a
                rendered `notifications.center.groups.…` path. */}
            <h2 className="text-white font-semibold">
              {t(`notifications.center.groups.${group}`, { defaultValue: group })}
            </h2>
          </CardHeader>
          <CardContent className="grid gap-2 sm:grid-cols-2">
            {data.events
              .filter((e) => e.group === group)
              .map((e) => (
                <label key={e.event_type} className="flex items-center gap-2 text-sm text-bambu-gray-light">
                  {/* Disabled while a save is in flight, exactly as Reset is:
                      `useMutation` neither cancels nor serialises, so two quick
                      clicks race and whichever response lands LAST wins the
                      cache — silently reverting a toggle both requests
                      accepted. The optimistic update has already shown the
                      click landing, so the gap costs the user nothing. */}
                  <input
                    type="checkbox"
                    checked={e.subscribed}
                    disabled={save.isPending}
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
