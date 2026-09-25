import { useQuery } from '@tanstack/react-query';
import { Droplets, Split } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { api } from '../api/client';
import { farmPollInterval, farmQueryResumeOptions, farmRead, farmReadRetry, farmReadRetryDelay } from '../api/farmReadBudget';

interface Props {
  printerId: number;
  /** Gate the polling on the parent's live state — no requests for idle printers. */
  printing: boolean;
  /** Archive identity, not filename: repeat prints must not reuse old usage. */
  archiveId?: number | null;
}

/**
 * Live "filament so far" line for an active print. Display-only: the backend
 * computes it from the usage journal + per-layer G-code cumulative and writes
 * nothing — the books are written once, at completion.
 */
export function UsageProjection({ printerId, printing, archiveId }: Props) {
  const { t } = useTranslation();
  const { data } = useQuery({
    ...farmQueryResumeOptions,
    queryKey: ['usage-projection', printerId, archiveId ?? 'unbound'],
    queryFn: ({ signal }) => farmRead(`usage-${printerId}-${archiveId ?? 'unbound'}`, signal,
      owned => api.getUsageProjection(printerId, owned)),
    enabled: printing,
    refetchInterval: query => farmPollInterval(30_000, query),
    staleTime: 25_000,
    retry: farmReadRetry,
    retryDelay: farmReadRetryDelay,
  });

  if (!printing || !data?.active || !data.slots?.length
    || (archiveId !== undefined && (archiveId === null || data.archive_id !== archiveId))) return null;

  const total = data.slots.reduce((sum, s) => sum + s.consumed_g, 0);
  const estimate = data.slots.reduce((sum, s) => sum + s.estimate_g, 0);

  return (
    <div className="mt-1 flex flex-wrap items-center gap-x-2 text-xs text-bambu-gray" data-testid="usage-projection">
      <span className="inline-flex items-center gap-1">
        <Droplets className="w-3 h-3 shrink-0" />
        {t('printers.usageProjection.soFar', {
          consumed: total.toFixed(0),
          estimate: estimate.toFixed(0),
        })}
      </span>
      {data.slots.length > 1 && (
        <span>{data.slots.map((s) => `#${s.slot_id} ${s.consumed_g.toFixed(0)}g`).join(' · ')}</span>
      )}
      {data.slots.some(
        (s) => new Set((s.segments ?? []).map((seg) => `${seg.spool_id}:${seg.spoolman_spool_id}`)).size > 1,
      ) && (
        <span
          className="inline-flex items-center text-orange-400/80"
          title={`${t('printers.usageProjection.split')} — ${t('printers.usageProjection.splitHint')}`}
          aria-label={t('printers.usageProjection.split')}
        >
          <Split className="w-3 h-3 shrink-0 animate-pulse" />
        </span>
      )}
    </div>
  );
}
