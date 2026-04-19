import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { Printer as PrinterIcon, ListTodo, AlertTriangle, Timer } from 'lucide-react';
import type { PrinterQueue, PrintQueueItem } from '../../api/client';

interface Props {
  queues: PrinterQueue[] | undefined;
  pendingItems: PrintQueueItem[] | undefined;
}

function formatDuration(totalSeconds: number): string {
  if (!Number.isFinite(totalSeconds) || totalSeconds <= 0) return '0m';
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.round((totalSeconds % 3600) / 60);
  if (hours > 0 && minutes > 0) return `${hours}h ${minutes}m`;
  if (hours > 0) return `${hours}h`;
  return `${minutes}m`;
}

export function QueueStatsBar({ queues, pendingItems }: Props) {
  const { t } = useTranslation();

  const stats = useMemo(() => {
    const printing = queues?.filter(q => q.status === 'printing').length ?? 0;
    const error = queues?.filter(q => q.status === 'error').length ?? 0;
    const pending = queues?.reduce((sum, q) => sum + q.pending_count, 0) ?? 0;
    const estimatedSeconds = pendingItems?.reduce(
      (sum, it) => sum + (it.print_time_seconds ?? 0),
      0,
    ) ?? 0;
    return { printing, pending, error, estimatedSeconds };
  }, [queues, pendingItems]);

  const tiles = [
    {
      key: 'printing',
      icon: PrinterIcon,
      label: t('queue.stats.printing'),
      value: stats.printing,
      tone: 'text-blue-400',
    },
    {
      key: 'pending',
      icon: ListTodo,
      label: t('queue.stats.pending'),
      value: stats.pending,
      tone: 'text-white',
    },
    {
      key: 'remaining',
      icon: Timer,
      label: t('queue.stats.estimatedRemaining'),
      value: formatDuration(stats.estimatedSeconds),
      tone: 'text-bambu-green',
    },
    {
      key: 'errors',
      icon: AlertTriangle,
      label: t('queue.stats.errors'),
      value: stats.error,
      tone: stats.error > 0 ? 'text-red-400' : 'text-bambu-gray',
    },
  ];

  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-2 mb-4">
      {tiles.map(({ key, icon: Icon, label, value, tone }) => (
        <div
          key={key}
          className="flex items-center gap-3 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg px-3 py-2"
        >
          <Icon className={`w-5 h-5 ${tone}`} />
          <div className="min-w-0">
            <div className="text-[10px] uppercase tracking-wide text-bambu-gray truncate">{label}</div>
            <div className={`text-lg font-semibold ${tone} leading-tight`}>{value}</div>
          </div>
        </div>
      ))}
    </div>
  );
}
