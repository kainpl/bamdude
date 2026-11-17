import { useRef, type KeyboardEvent } from 'react';
import { useSearchParams } from 'react-router';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Globe, Library } from 'lucide-react';

import { api } from '../api/client';
import { LoadingBlock } from '../components/LoadingBlock';
import { ManyfoldTab } from '../components/ManyfoldTab';
import { MANYFOLD_TRANSPORT_UNAVAILABLE, manyfoldUnusableQuery } from '../components/manyfoldRefusal';
import { useAuth } from '../contexts/AuthContext';
import { MakerworldPage } from './MakerworldPage';

type SourceTab = 'makerworld' | 'manyfold';

const tabId = (id: SourceTab) => `model-sources-tab-${id}`;
const panelId = (id: SourceTab) => `model-sources-panel-${id}`;

/**
 * Model sources: the places models come into the library from, one tab each.
 * MakerWorld takes a pasted link; Manyfold is a self-hosted library that is
 * browsed. A tab shows only to a user with that source's own permission, and
 * Manyfold only once it is connected — except to someone who can connect it.
 */
export function ModelSourcesPage() {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const [searchParams, setSearchParams] = useSearchParams();
  const tabButtons = useRef(new Map<SourceTab, HTMLButtonElement>());

  const canViewManyfold = hasPermission('manyfold:view');
  const manyfoldStatus = useQuery({
    queryKey: ['manyfold-status'],
    queryFn: () => api.getManyfoldStatus(),
    enabled: canViewManyfold,
  });
  // A status that could not be read is not "not connected": the tab stays, and says so itself.
  // Read off the error COUNT, not `isError` — a retry of a read that never succeeded goes back
  // to "pending", and a tab that vanished for the length of every retry would unmount the very
  // component that is retrying.
  const statusFailed = manyfoldStatus.data === undefined && manyfoldStatus.errorUpdateCount > 0;
  // A connection that is set up and cannot be used: the tab's own read of why (same key, one
  // request). Switched off stays hidden; a failed safety check of the connection is said to
  // everyone who could use Manyfold, on its tab.
  const unusableReason = useQuery({
    ...manyfoldUnusableQuery,
    enabled: canViewManyfold && manyfoldStatus.data?.usable === false && manyfoldStatus.data.configured === true,
  });
  const transportGone = unusableReason.data === MANYFOLD_TRANSPORT_UNAVAILABLE;
  const showManyfold =
    canViewManyfold &&
    (manyfoldStatus.data?.usable === true || transportGone || statusFailed || hasPermission('settings:update'));

  const tabs: { id: SourceTab; label: string; icon: typeof Globe }[] = [];
  if (hasPermission('makerworld:view')) tabs.push({ id: 'makerworld', label: t('modelSources.tabMakerworld'), icon: Globe });
  if (showManyfold) tabs.push({ id: 'manyfold', label: t('modelSources.tabManyfold'), icon: Library });

  const requested = searchParams.get('tab');
  // An address that names Manyfold, while it is not yet known whether Manyfold has a tab:
  // wait for the answer rather than mount MakerWorld (and send its requests) for a moment.
  const deciding = requested === 'manyfold' && canViewManyfold && !showManyfold && manyfoldStatus.isLoading;
  // A Manyfold-only user, before Manyfold is connected, has no tab: they get the
  // Manyfold tab's "not connected" note instead of an empty page.
  const active = tabs.find((tab) => tab.id === requested)?.id ?? tabs[0]?.id ?? (canViewManyfold ? 'manyfold' : undefined);

  // A tab switch replaces the address (tabs do not fill the history) and drops the other
  // tab's own parameters — the Manyfold tab keeps its place in the catalogue there.
  const select = (id: SourceTab) => setSearchParams({ tab: id }, { replace: true });
  // The tab pattern's keys: the arrows, Home and End move between tabs and select at once;
  // Tab itself leaves the strip (only the selected tab is a tab stop).
  const onTabKey = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    const last = tabs.length - 1;
    const next =
      event.key === 'ArrowRight' ? (index === last ? 0 : index + 1)
      : event.key === 'ArrowLeft' ? (index === 0 ? last : index - 1)
      : event.key === 'Home' ? 0
      : event.key === 'End' ? last
      : null;
    if (next === null) return;
    event.preventDefault();
    select(tabs[next].id);
    tabButtons.current.get(tabs[next].id)?.focus();
  };

  const content = deciding ? (
    <LoadingBlock label={t('common.loading')} />
  ) : (
    <>
      {active === 'makerworld' && <MakerworldPage embedded />}
      {active === 'manyfold' && <ManyfoldTab />}
    </>
  );

  return (
    <div className="p-4 space-y-4">
      <div>
        <h1 className="text-2xl font-bold text-white flex items-center gap-3">
          <Globe className="w-6 h-6 text-bambu-green" />
          {t('modelSources.title')}
        </h1>
      </div>

      {tabs.length > 1 && (
        <div className="flex border-b border-bambu-dark-tertiary" role="tablist" aria-label={t('modelSources.title')}>
          {tabs.map(({ id, label, icon: Icon }, index) => (
            <button
              key={id}
              ref={(node) => {
                if (node) tabButtons.current.set(id, node);
                else tabButtons.current.delete(id);
              }}
              type="button"
              role="tab"
              id={tabId(id)}
              aria-selected={active === id}
              aria-controls={panelId(id)}
              tabIndex={active === id ? 0 : -1}
              onClick={() => select(id)}
              onKeyDown={(event) => onTabKey(event, index)}
              className={`flex items-center gap-2 px-4 py-3 text-sm font-medium transition-colors border-b-2 -mb-px ${
                active === id ? 'text-bambu-green border-bambu-green' : 'text-bambu-gray hover:text-white border-transparent'
              }`}
            >
              <Icon className="w-4 h-4" />
              {label}
            </button>
          ))}
        </div>
      )}

      {tabs.length > 1 && active ? (
        <div role="tabpanel" id={panelId(active)} aria-labelledby={tabId(active)}>
          {content}
        </div>
      ) : (
        content
      )}
    </div>
  );
}
