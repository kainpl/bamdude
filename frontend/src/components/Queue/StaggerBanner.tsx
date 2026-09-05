import { useQuery } from '@tanstack/react-query';
import { Zap, Info } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { api } from '../../api/client';

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return s > 0 ? `${m}m ${s}s` : `${m}m`;
}

/**
 * Electrical-load diagnostic strip shown at the top of QueuePage.
 *
 * Hidden when stagger is disabled.  Refreshes every 10 s so countdown
 * numbers stay fresh without requiring user interaction.
 *
 * One segment per group: with no split the backend sends a single unlabelled
 * group and the line reads farm-wide, exactly as it always did; with a split
 * on, each phase / room gets its own `label: occupied/capacity`.  The tooltip
 * lists every printer holding a slot with its state (heating / interval_wait)
 * and time to free, grouped under its label, and marks a wildcard printer —
 * one with no chosen tag or location, which therefore counts in every group.
 */
export function StaggerBanner() {
  const { t } = useTranslation();
  const { data } = useQuery({
    queryKey: ['stagger-state'],
    queryFn: () => api.getStaggerState(),
    refetchInterval: 10_000,
    refetchOnWindowFocus: false,
  });

  if (!data || !data.enabled) return null;

  const capacity = data.concurrent;
  const line = data.groups.length === 1 && data.groups[0].label === null
    ? t('queue.stagger.slots', { occupied: data.groups[0].occupied, capacity })
    : data.groups.map((g) => t('queue.stagger.group', { label: g.label ?? '—', occupied: g.occupied, capacity })).join(' · ');

  const nextFree = data.groups
    .map((g) => g.next_free_in_seconds)
    .filter((s): s is number => s !== null && s > 0);
  const soonest = nextFree.length ? Math.min(...nextFree) : null;

  const tooltip = data.groups.every((g) => g.slots.length === 0)
    ? t('queue.stagger.allFree')
    : data.groups
        .filter((g) => g.slots.length > 0)
        .map((g) => {
          const rows = g.slots.map((s) => {
            const stateLabel = s.state === 'heating' ? t('queue.stagger.heating') : t('queue.stagger.intervalWait');
            const wild = s.wildcard ? ` (${t('queue.stagger.wildcard')})` : '';
            return `  ${s.printer_name}${wild}: ${stateLabel}, ${formatDuration(s.seconds_to_free)}`;
          });
          return g.label ? [g.label, ...rows].join('\n') : rows.join('\n');
        })
        .join('\n');

  return (
    <div
      className="mb-3 flex items-center gap-3 rounded-lg bg-bambu-dark-secondary border border-bambu-dark-tertiary px-3 py-2 text-sm"
      title={tooltip}
    >
      <Zap className="w-4 h-4 text-amber-600 dark:text-amber-400 shrink-0" />
      <span className="text-white">{line}</span>
      {soonest !== null && (
        <span className="text-bambu-gray">· {t('queue.stagger.nextFreeIn', { duration: formatDuration(soonest) })}</span>
      )}
      <Info className="w-3.5 h-3.5 text-bambu-gray ml-auto shrink-0" />
    </div>
  );
}
