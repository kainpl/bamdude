import { createContext, useContext, type ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, type PrintQueueItem, type QueueSummary } from '../api/client';
import { farmPollInterval, farmQueryResumeOptions, farmRead, farmReadRetry, farmReadRetryDelay } from '../api/farmReadBudget';

type FarmQueueRows = {
  pending: PrintQueueItem[] | undefined;
  printing: PrintQueueItem[] | undefined;
  summary?: QueueSummary | undefined;
  summaryError?: boolean;
  summaryPending?: boolean;
};

const FarmQueueContext = createContext<FarmQueueRows | null>(null);

/** Mounted fleet screens own the full reads; cards only select their rows. */
export function FarmQueueScope({ rows, children }: { rows: FarmQueueRows | null; children: ReactNode }) {
  return <FarmQueueContext.Provider value={rows}>{children}</FarmQueueContext.Provider>;
}

export function usePrinterQueueRows(printerId: number, status: 'pending' | 'printing', enabled = true) {
  const farm = useContext(FarmQueueContext);
  const scoped = useQuery({
    ...farmQueryResumeOptions,
    queryKey: ['queue', printerId, status],
    queryFn: ({ signal }) => farmRead(`queue-${printerId}-${status}`, signal, owned => api.getQueue(printerId, status, { signal: owned })),
    enabled: farm === null && enabled,
    refetchInterval: farm === null && enabled
      ? query => farmPollInterval(status === 'printing' ? 10_000 : 30_000, query) : false,
    retry: farmReadRetry,
    retryDelay: farmReadRetryDelay,
  });
  return {
    data: farm === null ? scoped.data : farm[status]?.filter(item => (item.printer_id ?? item.queue_id) === printerId),
  };
}

export function useQueueSummarySnapshot() {
  const farm = useContext(FarmQueueContext);
  const scoped = useQuery({
    ...farmQueryResumeOptions,
    queryKey: ['queue', 'summary'],
    queryFn: ({ signal }) => farmRead('queue-summary', signal, owned => api.getQueueSummary({ signal: owned })),
    enabled: farm === null,
    retry: farmReadRetry,
    retryDelay: farmReadRetryDelay,
  });
  return farm === null
    ? { data: scoped.data, isError: scoped.isError, isPending: scoped.isPending }
    : { data: farm.summary, isError: farm.summaryError ?? false, isPending: farm.summaryPending ?? false };
}
