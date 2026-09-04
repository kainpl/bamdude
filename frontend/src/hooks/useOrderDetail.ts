import { useQuery } from '@tanstack/react-query';
import { api } from '../api/client';
import type { Order } from '../api/client';

/**
 * One order, from the ONE definition of that query.
 *
 * ⚠️ **A query has a single `meta`, whoever asked last.** Two components watch
 * `['project', id]` — the order page and its queue panel — and TanStack keeps
 * one set of options per query key: the last observer to mount wins. When the
 * page declared `meta: { refreshToast: true }` and the panel declared nothing,
 * the panel silently wiped it, and a failed background refetch went unreported
 * on exactly the page the flag was added for. Measured, not assumed.
 *
 * So the options live here and neither caller gets to write them out again.
 */
export function useOrderDetail(id: number) {
  return useQuery<Order>({
    queryKey: ['project', id],
    queryFn: () => api.getOrder(id),
    enabled: Number.isFinite(id),
    // The page keeps its data when a REFETCH fails (it asks "do I have data?"
    // before "did the fetch fail?"), so the cache is what says the figures are
    // older than they look. See `utils/appQueryClient`.
    meta: { refreshToast: true },
  });
}
