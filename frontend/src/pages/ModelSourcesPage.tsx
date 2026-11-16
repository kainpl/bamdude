import { useSearchParams } from 'react-router';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Globe, Library } from 'lucide-react';

import { api } from '../api/client';
import { ManyfoldTab } from '../components/ManyfoldTab';
import { useAuth } from '../contexts/AuthContext';
import { MakerworldPage } from './MakerworldPage';

type SourceTab = 'makerworld' | 'manyfold';

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

  const canViewManyfold = hasPermission('manyfold:view');
  const manyfoldStatus = useQuery({
    queryKey: ['manyfold-status'],
    queryFn: () => api.getManyfoldStatus(),
    enabled: canViewManyfold,
  });
  const showManyfold = canViewManyfold && (manyfoldStatus.data?.usable === true || hasPermission('settings:update'));

  const tabs: { id: SourceTab; label: string; icon: typeof Globe }[] = [];
  if (hasPermission('makerworld:view')) tabs.push({ id: 'makerworld', label: t('modelSources.tabMakerworld'), icon: Globe });
  if (showManyfold) tabs.push({ id: 'manyfold', label: t('modelSources.tabManyfold'), icon: Library });

  const requested = searchParams.get('tab');
  // A Manyfold-only user, before Manyfold is connected, has no tab: they get the
  // Manyfold tab's "not connected" note instead of an empty page.
  const active = tabs.find((tab) => tab.id === requested)?.id ?? tabs[0]?.id ?? (canViewManyfold ? 'manyfold' : undefined);

  return (
    <div className="p-4 space-y-4">
      <div>
        <h1 className="text-2xl font-bold text-white flex items-center gap-3">
          <Globe className="w-6 h-6 text-bambu-green" />
          {t('modelSources.title')}
        </h1>
      </div>

      {tabs.length > 1 && (
        <div className="flex border-b border-bambu-dark-tertiary" role="tablist">
          {tabs.map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              type="button"
              role="tab"
              aria-selected={active === id}
              onClick={() => setSearchParams({ tab: id }, { replace: true })}
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

      {active === 'makerworld' && <MakerworldPage embedded />}
      {active === 'manyfold' && <ManyfoldTab />}
    </div>
  );
}
