import { useTranslation } from 'react-i18next';
import type { MonitorJob } from './types';

export function useJobLabel() {
  const { t } = useTranslation();
  return (job: MonitorJob | null | undefined, next = false) => !job ? '—' :
    job.visibility === 'restricted' ? t(next ? 'monitor.nextJob' : 'monitor.currentJob') : job.name || t('monitor.unnamedJob');
}
