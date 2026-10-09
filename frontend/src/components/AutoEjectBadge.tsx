import { useTranslation } from 'react-i18next';
import type { AutoEjectSettings } from '../api/client';

export function AutoEjectBadge({ mode, settings }: { mode?: unknown; settings?: AutoEjectSettings | null }) {
  const { t } = useTranslation();
  return mode === true ? <span className={`inline-flex rounded px-1.5 py-0.5 text-xs ${settings?.skip_check
    ? 'bg-red-500/10 text-red-700 dark:text-red-400' : 'bg-bambu-green/10 text-bambu-green'}`}
    title={settings?.skip_check ? t('autoEject.riskTitle') : t('autoEject.requirements')}>
    {t(settings?.skip_check ? 'autoEject.badgeUnchecked' : 'autoEject.badge')}
  </span> : null;
}
