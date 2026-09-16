/**
 * The notification centre: the in-app inbox, what lands in it, and — when the
 * install has them — the per-user email switches that used to be this whole
 * page.
 *
 * The open tab lives in `?tab=`, and the inbox is the default with NO query
 * parameter at all: a bare `/notifications` is always the inbox and stays
 * clean. A `?tab=` naming something this install does not offer (an `email`
 * link sent to an instance without Advanced Authentication) falls back to the
 * inbox rather than rendering an empty shell.
 *
 * ⚠️ Nothing below the heading is drawn until BOTH gate queries have answered.
 * Which tabs exist decides which tab a `?tab=` may select, so a strip drawn
 * over an unanswered gate is a strip that is about to change under the user:
 * a `?tab=email` deep link would mount the inbox for a tick — firing its own
 * two requests — and only then swap to the Email card. The old page held the
 * same wait behind the same loading block. The `<h1>` stays up through it, so
 * the title does not flash in.
 */

import { useQuery } from '@tanstack/react-query';
import { useSearchParams } from 'react-router';
import { useTranslation } from 'react-i18next';
import { Bell } from 'lucide-react';
import { api } from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import { LoadingBlock } from '../components/LoadingBlock';
import { InboxTab } from '../components/notifications/InboxTab';
import { SubscriptionsTab } from '../components/notifications/SubscriptionsTab';
import { EmailDeliveryCard } from '../components/notifications/EmailDeliveryCard';

type Tab = 'inbox' | 'subscriptions' | 'email';

export function NotificationCenterPage() {
  const { t } = useTranslation();
  const { authEnabled, hasPermission } = useAuth();
  const [searchParams, setSearchParams] = useSearchParams();

  const { data: advancedAuthStatus, isLoading: isAdvancedAuthLoading } = useQuery({
    queryKey: ['advancedAuthStatus'],
    queryFn: api.getAdvancedAuthStatus,
    staleTime: 5 * 60 * 1000,
  });
  const { data: settings, isLoading: isSettingsLoading } = useQuery({
    queryKey: ['settings'],
    queryFn: api.getSettings,
    staleTime: 5 * 60 * 1000,
  });

  // The email card keeps the gate the old page enforced by redirecting: SMTP
  // lives in Advanced Authentication, and the per-user emails have their own
  // permission. Everything else on this page needs only notifications:inbox.
  const emailTabVisible =
    Boolean(advancedAuthStatus?.advanced_auth_enabled) &&
    settings?.user_notifications_enabled !== false &&
    (!authEnabled || hasPermission('notifications:user_email'));

  const gateSettled = !isAdvancedAuthLoading && !isSettingsLoading;
  const tabs: Tab[] = emailTabVisible ? ['inbox', 'subscriptions', 'email'] : ['inbox', 'subscriptions'];
  const requested = searchParams.get('tab') as Tab | null;
  const tab: Tab = requested && tabs.includes(requested) ? requested : 'inbox';
  const setTab = (next: Tab) => setSearchParams(next === 'inbox' ? {} : { tab: next }, { replace: true });

  return (
    <div className="p-4 space-y-4">
      <h1 className="text-2xl font-bold text-white flex items-center gap-3">
        <Bell className="w-6 h-6 text-bambu-green" />
        {t('notifications.center.title')}
      </h1>
      {!gateSettled ? (
        <LoadingBlock label={t('common.loading')} className="h-64 text-bambu-gray" />
      ) : (
        <>
          <div role="tablist" className="flex gap-1 border-b border-bambu-dark-tertiary">
            {tabs.map((key) => (
              <button
                key={key}
                id={`notification-tab-${key}`}
                type="button"
                role="tab"
                aria-selected={tab === key}
                aria-controls={`notification-panel-${key}`}
                // One tab stop for the whole strip: Tab reaches the selected
                // tab, Tab again leaves for the panel. Deliberately no
                // arrow-key roving — three buttons do not need the machinery.
                tabIndex={tab === key ? 0 : -1}
                onClick={() => setTab(key)}
                className={`px-4 py-2 text-sm border-b-2 -mb-px transition-colors ${
                  tab === key ? 'border-bambu-green text-white' : 'border-transparent text-bambu-gray hover:text-white'
                }`}
              >
                {t(`notifications.center.tabs.${key}`)}
              </button>
            ))}
          </div>
          <div role="tabpanel" id={`notification-panel-${tab}`} aria-labelledby={`notification-tab-${tab}`}>
            {tab === 'inbox' && <InboxTab />}
            {tab === 'subscriptions' && <SubscriptionsTab />}
            {tab === 'email' && <EmailDeliveryCard />}
          </div>
        </>
      )}
    </div>
  );
}
