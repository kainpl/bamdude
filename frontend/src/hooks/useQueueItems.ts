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
 *
 * ⚠️ **Pending polls at the badge's cadence, not the Queue page's.** The
 * sidebar badge is mounted on EVERY screen and is the only sign of waiting work
 * a page that shows no queue gives — it used to have its own 5 s query, and
 * folding it into this one at 30 s would have made every other screen a
 * half-minute slower to notice a print was queued. Ten seconds is the
 * compromise the whole app now shares; `printing` was already there.
 *
 * What it costs, said plainly: every open tab now asks for the pending list
 * three times as often as the Queue page's old 30 s — one request per tab per
 * ten seconds, against a list that is usually short. What it buys is one cache
 * entry instead of two and one answer instead of two disagreeing ones. The
 * badge's focus refetch is suppressed too, and by the app's own settings rather
 * than by anything removed here: `utils/appQueryClient` gives every query a
 * 60 s `staleTime`, and TanStack refetches on focus only what is STALE — a list
 * this hook re-polls every ten seconds never is. So the first refresh after
 * coming back to a tab is up to ten seconds away rather than immediate — the
 * same ten seconds, from the other end.
 */
const PENDING_POLL_MS = 10_000;
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
