import { useCallback, useEffect, useRef } from 'react';
import { useQueryClient } from '@tanstack/react-query';

/**
 * Drop one query from the cache once the page that owned it has UNMOUNTED.
 *
 * The case is a detail page deleting its own row. The list keys are
 * invalidated (`invalidateAfterDelete`) and the row's own key deliberately is
 * NOT — refetching a row that no longer exists lands a 404 in the query of a
 * page that is already leaving. But leaving the entry behind is wrong too: it
 * sits there for the whole `gcTime`, and a Back inside the 60 s `staleTime`
 * renders the deleted row from cache as if nothing had happened.
 *
 * ⚠️ **"After `navigate`" is not good enough, and this is why the hook
 * exists.** A `removeQueries` written on the line after `navigate(...)` runs
 * while the page is still mounted — React has only SCHEDULED the route change —
 * so the page's own observer immediately refetches the key that was just
 * removed and puts the row straight back. Measured, not assumed. Removing on
 * the unmount cleanup is the only point at which no observer is left to refill
 * it.
 *
 * Returns the arming function: call it in the delete's `onSuccess`, beside the
 * `navigate` that takes the page away.
 */
export function useForgetOnUnmount(queryKey: readonly unknown[]): () => void {
  const queryClient = useQueryClient();
  const armed = useRef(false);
  // The key can change with the route (`['project', id]`); the cleanup must use
  // whatever it was at the moment the page went away, not what it was at mount.
  const keyRef = useRef(queryKey);
  // ⚠️ Written in an EFFECT, never during render. A render can be thrown away
  // (StrictMode's double pass, a suspended or aborted concurrent render), and a
  // ref written on a render that never commits makes the cleanup fire against a
  // key the page never actually showed. After commit is exactly when "what the
  // page went away with" becomes a fact.
  useEffect(() => {
    keyRef.current = queryKey;
  });

  useEffect(
    () => () => {
      if (armed.current) queryClient.removeQueries({ queryKey: keyRef.current, exact: true });
    },
    [queryClient],
  );

  return useCallback(() => {
    armed.current = true;
  }, []);
}
