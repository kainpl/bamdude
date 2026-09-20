import { useQuery } from '@tanstack/react-query';
import { api } from '../api/client';
import type { Product } from '../api/client';

/**
 * One product, from the ONE definition of that query — `useOrderDetail`'s twin.
 *
 * ⚠️ **A query has a single `meta`, whoever asked last.** `['product', id]` is
 * watched by the product page AND by the card dialog that opens over it, and
 * TanStack keeps one set of options per query key: the last observer to mount
 * wins. The page declared `meta: { refreshToast: true }`; the dialog declared
 * nothing, so opening it wiped the flag and a failed background refetch went
 * unreported for as long as the dialog stayed open — the same bug the order
 * side had, on the key nobody had checked.
 *
 * `__tests__/hooks/detailQueryKeys.test.ts` greps the source for that, on the
 * same terms as the order side. ⚠️ ONE LINE containing `queryKey:` followed by
 * `['product',` in SINGLE quotes, outside `src/__tests__`, on a line that does
 * not read as an invalidation call — those name the key without observing it and
 * are allowed everywhere. A declaration spelled some other way (double quotes, a
 * constant, a line break after `queryKey:`) is not seen: the gate catches the
 * copy-paste that caused the bug, not every possible spelling.
 *
 * `id` accepts `null` so a caller that already HAS the record — the dialog
 * opened on a full product rather than on a list row — disables the query by
 * passing nothing to fetch, instead of attaching its own `enabled` and, with
 * it, its own empty `meta`.
 */
export function useProductDetail(id: number | null) {
  return useQuery<Product>({
    queryKey: ['product', id],
    queryFn: () => api.getProduct(id as number),
    enabled: Number.isFinite(id),
    // The page keeps its data when a REFETCH fails, so the cache is what says
    // the figures are older than they look. See `utils/appQueryClient`.
    meta: { refreshToast: true },
  });
}
