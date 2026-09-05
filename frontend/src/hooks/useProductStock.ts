import { useQuery } from '@tanstack/react-query';
import { api } from '../api/client';
import type { ProductStock } from '../api/client';

/**
 * One product's free stock, from the ONE definition of that query.
 *
 * ⚠️ **Three screens ask this question and none of them may declare it twice.**
 * The product page's stock section, the add-line row and the line editor all
 * need `kits_available` for the same product, and TanStack keeps a SINGLE set
 * of options per query key — the last observer to mount owns them. Two hand-
 * written `useQuery({ queryKey: ['product-stock', id] })` calls with different
 * `enabled` / `retry` are therefore not two watchers of one key but a race for
 * whose options apply; the same trap `useProductDetail` and `useOrderDetail`
 * exist to close, and the one `hooks/detailQueryKeys.test.ts` polices.
 *
 * `id` accepts `null` so a caller with no product picked yet disables the query
 * by passing nothing to fetch, rather than attaching its own `enabled`. ⚠️ In
 * TanStack v5 a DISABLED query is not "loading" — it is `pending` and not
 * fetching — so a caller must never read `isPending` as "wait for it".
 *
 * `retry: false` because every consumer degrades to "no stock" on a failure and
 * none of them can act on the error: the line dialog simply offers no kits, and
 * the section says the shelf is empty. Three silent retries would only delay
 * that by seconds while the operator waits on a spinner.
 */
export function useProductStock(id: number | null) {
  return useQuery<ProductStock>({
    queryKey: ['product-stock', id],
    queryFn: () => api.getProductStock(id as number),
    enabled: Number.isFinite(id),
    retry: false,
  });
}
