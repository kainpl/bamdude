// The preset list's status of the slicing-setup family (spec slicing-setup r5,
// R10): the Bambu Cloud and Orca Cloud banners (only the states a signed-in
// user needs to see — expired, unreachable) and "Refresh presets", which asks
// the server to bypass its caches and writes the answer into the current
// catalogue context. The markup is the Slice dialog's.
import { Cloud, CloudOff, RefreshCw } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import type { SlicerCloudStatus } from '../../api/client';
import type { PresetCatalogue } from './usePresetCatalogue';

interface PresetListStatusProps {
  catalogue: PresetCatalogue;
  disabled?: boolean;
}

export function PresetListStatus({ catalogue, disabled }: PresetListStatusProps) {
  const { t } = useTranslation();
  const data = catalogue.data;
  return (
    <div className="flex items-start justify-between gap-2">
      <div className="flex-1 space-y-2">
        {data && <CloudStatusBanner status={data.cloud_status} cloudName="bambu" />}
        {data && <CloudStatusBanner status={data.orca_cloud_status} cloudName="orca" />}
      </div>
      <button
        type="button"
        onClick={() => void catalogue.refresh()}
        disabled={disabled || catalogue.refreshing}
        className="flex-shrink-0 inline-flex items-center gap-1 px-2 py-1 rounded-md text-xs text-bambu-gray hover:text-white hover:bg-bambu-dark-tertiary/40 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
        title={t('slice.refreshPresetsTitle')}
        aria-label={t('slice.refreshPresets')}
      >
        <RefreshCw className={`w-3.5 h-3.5 ${catalogue.refreshing ? 'animate-spin' : ''}`} />
        {t('slice.refreshPresets')}
      </button>
    </div>
  );
}

function CloudStatusBanner({ status, cloudName }: { status: SlicerCloudStatus; cloudName: 'bambu' | 'orca' }) {
  const { t } = useTranslation();
  // `ok` is the happy path; `not_authenticated` is silenced too — a person who
  // never signed in needs no permanent nag. Only real breakage surfaces.
  if (status === 'ok' || status === 'not_authenticated') return null;
  const messages =
    cloudName === 'orca'
      ? {
          expired: { key: 'slice.orcaCloud.expired', fallback: 'Orca Cloud session expired — sign in again to refresh your Orca presets.' },
          unreachable: { key: 'slice.orcaCloud.unreachable', fallback: 'Orca Cloud is unreachable right now. Other presets still work.' },
        }
      : {
          expired: { key: 'slice.cloud.expired', fallback: 'Bambu Cloud session expired — sign in again to refresh your cloud presets.' },
          unreachable: {
            key: 'slice.cloud.unreachable',
            fallback: 'Bambu Cloud is unreachable right now. Local and standard presets still work.',
          },
        };
  const tones: Record<'expired' | 'unreachable', { tone: string; icon: typeof Cloud }> = {
    expired: {
      tone: 'border-amber-300 dark:border-amber-700/40 bg-amber-50 dark:bg-amber-900/20 text-amber-800 dark:text-amber-200',
      icon: CloudOff,
    },
    unreachable: { tone: 'border-bambu-dark-tertiary/40 bg-bambu-dark text-bambu-gray', icon: CloudOff },
  };
  const { tone, icon: Icon } = tones[status];
  const { key, fallback } = messages[status];
  return (
    <div className={`flex items-start gap-2 text-xs rounded-md border p-2 ${tone}`} role="status">
      <Icon className="w-4 h-4 flex-shrink-0 mt-0.5" />
      <span>{t(key, fallback)}</span>
    </div>
  );
}
