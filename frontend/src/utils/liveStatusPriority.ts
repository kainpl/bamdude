/**
 * The active fleet surface tells live feeds which printer cards currently
 * cost a paint. A virtual grid already keeps this set to the viewport plus a
 * small overscan; ordinary grids contribute their mounted cards.
 */
const scopes = new Map<string, Set<number>>();

export function setLiveStatusPriority(scope: string, ids: Iterable<number>) {
  scopes.set(scope, new Set(ids));
}

export function clearLiveStatusPriority(scope: string) {
  scopes.delete(scope);
}

export function liveStatusPriorityIds(): ReadonlySet<number> {
  const ids = new Set<number>();
  for (const scopeIds of scopes.values()) {
    for (const id of scopeIds) ids.add(id);
  }
  return ids;
}

/** Stable partition: a current card never waits behind an offscreen update. */
export function prioritizeLiveStatusEntries<T>(entries: readonly [number, T][], priorityIds = liveStatusPriorityIds()): [number, T][] {
  if (priorityIds.size === 0) return [...entries];
  return [
    ...entries.filter(([id]) => priorityIds.has(id)),
    ...entries.filter(([id]) => !priorityIds.has(id)),
  ];
}
