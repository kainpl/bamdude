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
 * numbers stay fresh without requiring user interaction.  Tooltip lists
 * each printer currently holding a stagger slot with its state
 * (heating / interval_wait) and time to free.
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

  const occupied = data.slots.length;
  const capacity = data.concurrent;

  const tooltip = data.slots.length === 0
    ? t('queue.stagger.allFree')
    : data.slots
        .map(s => {
          const stateLabel = s.state === 'heating' ? t('queue.stagger.heating') : t('queue.stagger.intervalWait');
          return `${s.printer_name}: ${stateLabel}, ${formatDuration(s.seconds_to_free)}`;
        })
        .join('\n');

  return (
    <div
      className="mb-3 flex items-center gap-3 rounded-lg bg-bambu-dark-secondary border border-bambu-dark-tertiary px-3 py-2 text-sm"
      title={tooltip}
    >
      <Zap className="w-4 h-4 text-amber-400 shrink-0" />
      <span className="text-white">
        {t('queue.stagger.slots', { occupied, capacity })}
      </span>
      {data.next_free_in_seconds !== null && data.next_free_in_seconds > 0 && (
        <span className="text-bambu-gray">
          · {t('queue.stagger.nextFreeIn', { duration: formatDuration(data.next_free_in_seconds) })}
        </span>
      )}
      <Info className="w-3.5 h-3.5 text-bambu-gray ml-auto shrink-0" />
    </div>
  );
}
