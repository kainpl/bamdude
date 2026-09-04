import { useQuery } from '@tanstack/react-query';
import { api } from '../api/client';
import type { PrintQueueItem } from '../api/client';

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
 */
const PENDING_POLL_MS = 30_000;
const PRINTING_POLL_MS = 10_000;

/** Everything waiting, farm-wide. */
export function usePendingQueueItems() {
  return useQuery<PrintQueueItem[]>({
    queryKey: ['queue', 'all', 'pending'],
    queryFn: () => api.getQueue(undefined, 'pending'),
    refetchInterval: PENDING_POLL_MS,
  });
}

/**
 * Everything running, farm-wide — real items plus the virtual rows the server
 * synthesises for external / direct prints, which is what lets a timeline (and
 * an order's queue panel) show a job nobody queued through BamDude.
 */
export function usePrintingQueueItems() {
  return useQuery<PrintQueueItem[]>({
    queryKey: ['queue', 'all', 'printing'],
    queryFn: () => api.getQueue(undefined, 'printing'),
    refetchInterval: PRINTING_POLL_MS,
  });
}
