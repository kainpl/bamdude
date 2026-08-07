/**
 * Which server-side setting changes may be adopted into an open Settings page (#2716).
 *
 * The page keeps a local copy and saves it back, debounced. It used to decide
 * "has this field changed?" by diffing that copy against the LIVE settings
 * cache — which cannot tell a user's edit from a value the server moved. So
 * anything written server-side while the page sat open (another admin, another
 * tab, a backup restore, our own log-retention writer) was written back to the
 * old value a few hundred milliseconds later. No interaction was needed: the
 * query carries `refetchOnWindowFocus`, so returning to the tab was enough.
 *
 * The fix needs a third input — a *baseline*, the last server snapshot the page
 * reconciled with. That makes "did the user edit this?" answerable: a field
 * where the local copy still equals the baseline has not been touched, so the
 * server's newer value can be taken. A field the user did edit keeps their
 * value and is saved over the server's, so the newer write wins either way
 * rather than the page always winning.
 *
 * Extracted from the component so this rule is testable on its own — inline, it
 * could only be reached through a full page render plus a driven refetch, which
 * is exactly the kind of test that passes for the wrong reason.
 */
export function adoptUntouchedServerChanges<T extends object>(
  baseline: T,
  local: T,
  server: T,
): Partial<T> {
  const adopted: Partial<T> = {};
  for (const key of Object.keys(server) as (keyof T)[]) {
    // Changed on the server AND untouched locally since the last reconcile.
    if (server[key] !== baseline[key] && local[key] === baseline[key]) {
      adopted[key] = server[key];
    }
  }
  return adopted;
}
