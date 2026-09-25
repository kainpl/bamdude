import { useQuery } from '@tanstack/react-query';
import { api } from '../api/client';
import type { AutoQueuePendingSummary, PrintQueueItem, QueueSummary } from '../api/client';
import { farmPollInterval, farmQueryResumeOptions, farmRead, farmReadRetry, farmReadRetryDelay } from '../api/farmReadBudget';

/**
 * The farm-wide queue, as ONE query per status.
 *
 * ⚠️ **The key is the point.** The queue page and the order page both want
 * "everything pending" and "everything printing"; while they asked under two
 * different keys (`['queue', 'all', …]` and `['queue', …]`) TanStack had no way
 * to know it was the same question, and the same list was fetched twice on its
 * own timer. One key, one poll, one cache entry — a queue mutation anywhere
 * invalidates the `['queue']` prefix and both readers move together.
 *
 * Polling intervals live here rather than at each call site for the same
 * reason: a second opinion about how often the queue changes is how two panels
 * of the same screen end up disagreeing about what is on a printer.
 *
 * The sidebar now reads only compact summaries. Full pending/printing rows
 * belong to mounted fleet screens; an isolated printer card keeps a scoped
 * fallback. The 10 s interval is the screen's disconnected-WS safety poll,
 * not a reason for the shell to download all queued jobs.
 */
const PENDING_POLL_MS = 10_000;
const PRINTING_POLL_MS = 10_000;

/** Sidebar and closed issue headers need counts, not the complete queue rows. */
export function useQueueSummary(enabled = true, poll = true) {
  return useQuery<QueueSummary>({
    ...farmQueryResumeOptions,
    queryKey: ['queue', 'summary'],
    queryFn: ({ signal }) => farmRead('queue-summary', signal, owned => api.getQueueSummary({ signal: owned })),
    enabled,
    refetchInterval: poll ? query => farmPollInterval(10_000, query) : false,
    retry: farmReadRetry,
    retryDelay: farmReadRetryDelay,
  });
}

export function useAutoQueuePendingSummary(enabled = true) {
  return useQuery<AutoQueuePendingSummary>({
    ...farmQueryResumeOptions,
    queryKey: ['auto-queue', 'summary'],
    queryFn: ({ signal }) => farmRead('auto-queue-summary', signal, owned => api.getAutoQueuePendingSummary({ signal: owned })),
    enabled,
    refetchInterval: query => farmPollInterval(5_000, query),
    retry: farmReadRetry,
    retryDelay: farmReadRetryDelay,
  });
}

/** Everything waiting, farm-wide. */
export function usePendingQueueItems(enabled = true) {
  return useQuery<PrintQueueItem[]>({
    ...farmQueryResumeOptions,
    queryKey: ['queue', 'all', 'pending'],
    queryFn: ({ signal }) => farmRead('queue-all-pending', signal, owned => api.getQueue(undefined, 'pending', { signal: owned })),
    enabled,
    refetchInterval: query => farmPollInterval(PENDING_POLL_MS, query),
    retry: farmReadRetry,
    retryDelay: farmReadRetryDelay,
  });
}

/**
 * Everything running, farm-wide — real items plus the virtual rows the server
 * synthesises for external / direct prints, which is what lets a timeline (and
 * an order's queue panel) show a job nobody queued through BamDude.
 */
export function usePrintingQueueItems(enabled = true) {
  return useQuery<PrintQueueItem[]>({
    ...farmQueryResumeOptions,
    queryKey: ['queue', 'all', 'printing'],
    queryFn: ({ signal }) => farmRead('queue-all-printing', signal, owned => api.getQueue(undefined, 'printing', { signal: owned })),
    enabled,
    refetchInterval: query => farmPollInterval(PRINTING_POLL_MS, query),
    retry: farmReadRetry,
    retryDelay: farmReadRetryDelay,
  });
}
