import { useQuery } from '@tanstack/react-query';
import { api } from '../api/client';
import type { Order } from '../api/client';

/**
 * One order, from the ONE definition of that query.
 *
 * ⚠️ **A query has a single `meta`, whoever asked last.** Several components
 * watch `['project', id]` — the order page, its queue panel, the line picker
 * inside a dialog, the "add to order" menu on an archive card — and TanStack
 * keeps one set of options per query key: the last observer to mount wins.
 * When the page declared `meta: { refreshToast: true }` and another observer
 * declared nothing, that observer silently wiped it, and a failed background
 * refetch went unreported on exactly the page the flag was added for.
 * Measured, not assumed.
 *
 * So the options live here and no caller gets to write them out again. A
 * grep-gate test fails the build if a `queryKey: ['project', …]` literal
 * appears anywhere but this file and `utils/queryInvalidation.ts`.
 *
 * `id` accepts `null` for the callers that watch an order the user has not
 * chosen yet: the query simply stays disabled, which is the same thing those
 * call sites used to spell as `enabled: orderId != null` — with their own
 * (empty) `meta` attached.
 */
export function useOrderDetail(id: number | null) {
  return useQuery<Order>({
    queryKey: ['project', id],
    queryFn: () => api.getOrder(id as number),
    enabled: Number.isFinite(id),
    // The page keeps its data when a REFETCH fails (it asks "do I have data?"
    // before "did the fetch fail?"), so the cache is what says the figures are
    // older than they look. See `utils/appQueryClient`.
    meta: { refreshToast: true },
  });
}
