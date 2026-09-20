import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import type { InfiniteData } from '@tanstack/react-query';
import { api, STOCK_JOURNAL_PAGE } from '../api/client';
import type { StockMovementsPage, StockMovementsParams, StockSummary, StockSummaryParams } from '../api/client';

/**
 * The Stock tab's two questions, each declared ONCE.
 *
 * TanStack keeps one set of options per query key and the last observer to
 * mount owns them (`hooks/detailQueryKeys.test.ts` polices the detail keys for
 * this reason), so the page and any future widget go through these two
 * functions rather than spelling the keys themselves.
 *
 * `retry: false` — both degrade to "could not load" and can act on nothing
 * else. `meta: { refreshToast: true }` — with data on screen a failed
 * background refetch keeps it and says so once, the rule the detail pages
 * follow.
 */
export function useStockSummary(params: StockSummaryParams) {
  return useQuery<StockSummary>({
    queryKey: ['stock-summary', params],
    queryFn: () => api.getStockSummary(params),
    retry: false,
    meta: { refreshToast: true },
  });
}

/** The journal filters are everything but the cursor and the page size. */
export type StockJournalFilters = Pick<StockMovementsParams, 'product_id' | 'part_id' | 'reason'>;

export function useStockMovements(filters: StockJournalFilters) {
  return useInfiniteQuery<
    StockMovementsPage,
    Error,
    InfiniteData<StockMovementsPage, number | null>,
    unknown[],
    number | null
  >({
    queryKey: ['stock-movements', filters],
    queryFn: ({ pageParam }) =>
      api.getStockMovements({ ...filters, before_id: pageParam, limit: STOCK_JOURNAL_PAGE }),
    initialPageParam: null,
    // A short page IS the end: the server sets `next_before_id` only on a
    // full one, and `undefined` tells TanStack there is no next page.
    getNextPageParam: (last) => last.next_before_id ?? undefined,
    retry: false,
    meta: { refreshToast: true },
  });
}
