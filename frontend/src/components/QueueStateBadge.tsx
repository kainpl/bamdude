import { List } from 'lucide-react';
import { Link } from 'react-router';
import { useTranslation } from 'react-i18next';
import { useQueueStatus } from '../hooks/useQueueStatus';

export function QueueStateBadge({ printerId, compact = false, bordered = false }: { printerId: number; compact?: boolean; bordered?: boolean }) {
  const { t } = useTranslation();
  const { data, isError, isEnabled } = useQueueStatus();
  if (!isEnabled) return null;
  const state = data?.queues.find(row => row.printer_id === printerId)?.effective_state ?? 'unknown';
  const title = `${t('nav.queue')}: ${t(`queueOperator.state.${state}`)}${isError ? ` · ${t('queueOperator.stale')}` : ''}`;
  const color = state === 'error' ? 'text-red-500' : state === 'paused' ? 'text-yellow-500'
    : state === 'printing' ? 'text-blue-400' : 'text-bambu-gray';
  return <div className={bordered ? "pt-2 mt-2 border-t border-bambu-dark-tertiary" : undefined}><Link to="/queue" title={title} aria-label={title} className={`inline-flex min-w-0 shrink-0 items-center gap-1 text-xs ${color}`}>
    <List className="h-3.5 w-3.5 shrink-0" />{!compact && <span>{t(`queueOperator.state.${state}`)}</span>}
  </Link></div>;
}
