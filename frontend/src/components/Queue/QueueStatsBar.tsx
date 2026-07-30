import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { Printer as PrinterIcon, ListTodo, AlertTriangle, Timer, Shuffle } from 'lucide-react';
import type { PrinterQueue, PrintQueueItem, AutoQueueItem } from '../../api/client';
import { estimateWallClockSeconds } from '../../utils/queueEstimate';

interface Props {
  queues: PrinterQueue[] | undefined;
  pendingItems: PrintQueueItem[] | undefined;
  /** Auto-queue items still waiting to be routed to a printer.
   *
   * Counted separately from ``pending``, which is per-printer queues only.
   * Auto-Queue holds at most one placed item per printer at a time, so the rest
   * of a batch legitimately sits here — reporting "Pending 0" while eight jobs
   * waited is what made a working Auto-Queue look dead. */
  unassignedCount: number;
  /** Auto-queue items still awaiting routing — needed for the wall-clock
   * estimate, which distributes them across the printers that can take them. */
  stagedItems: AutoQueueItem[] | undefined;
  /** Items currently printing, so "remaining" includes work in progress. */
  printingItems: PrintQueueItem[] | undefined;
}

function formatDuration(totalSeconds: number): string {
  if (!Number.isFinite(totalSeconds) || totalSeconds <= 0) return '0m';
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.round((totalSeconds % 3600) / 60);
  if (hours > 0 && minutes > 0) return `${hours}h ${minutes}m`;
  if (hours > 0) return `${hours}h`;
  return `${minutes}m`;
}

export function QueueStatsBar({ queues, pendingItems, unassignedCount, stagedItems, printingItems }: Props) {
  const { t } = useTranslation();

  const stats = useMemo(() => {
    const printing = queues?.filter(q => q.status === 'printing').length ?? 0;
    const error = queues?.filter(q => q.status === 'error').length ?? 0;
    const pending = queues?.reduce((sum, q) => sum + q.pending_count, 0) ?? 0;
    // Wall-clock, not a sum: printers run in parallel, so adding every queued
    // duration together reported 88 minutes for work four machines finish in
    // 22. Counts the prints in progress and the staging area too — both were
    // missing, and both are unambiguously part of "what is left".
    const estimatedSeconds = estimateWallClockSeconds({
      queues,
      pendingItems,
      printingItems,
      stagedItems,
      now: Date.now(),
    });
    return { printing, pending, error, estimatedSeconds };
  }, [queues, pendingItems, printingItems, stagedItems]);

  const tiles = [
    {
      key: 'printing',
      icon: PrinterIcon,
      label: t('queue.stats.printing'),
      value: stats.printing,
      tone: 'text-blue-700 dark:text-blue-400',
    },
    {
      key: 'pending',
      icon: ListTodo,
      label: t('queue.stats.pending'),
      value: stats.pending,
      tone: 'text-white',
    },
    // Always shown, zero included. Auto-Queue is a permanent fixture of this
    // page, so its backlog should be a permanent reading too — a tile that
    // appears only when non-zero teaches nobody where staged work lives, and
    // "no tile" reads the same as "nothing waiting" only if you already knew
    // the tile could appear at all.
    {
      key: 'unassigned',
      icon: Shuffle,
      label: t('queue.stats.awaitingRouting'),
      value: unassignedCount,
      tone: unassignedCount > 0 ? 'text-amber-600 dark:text-amber-400' : 'text-bambu-gray',
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
    <div className="grid grid-cols-2 md:grid-cols-5 gap-2 mb-4">
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
