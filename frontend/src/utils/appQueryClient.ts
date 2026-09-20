import { QueryCache, QueryClient, type Query } from '@tanstack/react-query';
import i18n from '../i18n';
import { notifyOutsideReact } from './toastBridge';

declare module '@tanstack/react-query' {
  interface Register {
    queryMeta: {
      /** Say so, once, when a BACKGROUND refetch of this query fails. See
       *  `refreshFailureToast` — opt-in, for queries whose page keeps the
       *  stale data on screen instead of falling back to an error state. */
      refreshToast?: boolean;
    };
  }
}

/**
 * The one thing a page that survives a failed refetch cannot say for itself.
 *
 * The three detail pages (order, product, customer) deliberately ask "do I
 * have data?" BEFORE "did the fetch fail?", so a background refetch that 500s
 * leaves the rendered page exactly as it was. That is the right call — a proxy
 * hiccup must not throw away an order somebody is reading — but it is also
 * completely silent: the figures on screen are now older than they look and
 * nothing says so.
 *
 * ⚠️ **The `data !== undefined` guard is the whole rule.** A FIRST fetch that
 * fails has its own error state on the page and needs no toast; only a query
 * that already held data has something stale to warn about. `QueryCache`'s
 * `onError` fires once per failed query (after retries), so one failure is one
 * toast however many components watch the key.
 */
function refreshFailureToast(_error: unknown, query: Query<unknown, unknown>): void {
  if (!query.meta?.refreshToast) return;
  if (query.state.data === undefined) return;
  // `i18n.t`, not `useTranslation` — there is no component here. The language
  // is whatever the app has already switched to.
  notifyOutsideReact(i18n.t('common.toast.refreshFailed'), 'warning');
}

/**
 * The app's `QueryClient`, as a factory so a test can build the same one.
 *
 * ⚠️ **`staleTime` is a minute and `retry` is 1 for every query in the app.**
 * Changing either here changes how the whole UI behaves; both were chosen
 * against a farm that pushes most of its updates over the websocket.
 */
export function createAppQueryClient(): QueryClient {
  return new QueryClient({
    queryCache: new QueryCache({ onError: refreshFailureToast }),
    defaultOptions: {
      queries: {
        staleTime: 1000 * 60,
        retry: 1,
      },
    },
  });
}
