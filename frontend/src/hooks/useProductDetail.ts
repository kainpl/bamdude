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
 * A grep-gate test fails the build if a `queryKey: ['product', …]` literal
 * appears in an observer outside this file. (Invalidation call sites name the
 * key too; the test only guards `queryKey:` declarations.)
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
