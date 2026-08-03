export interface LocationNode {
  id: number;
  name: string;
  parent_id: number | null;
  path: string;
  depth: number;
}

export interface LocationIndex {
  /** The id and everything beneath it. Empty for an id that is not there. */
  descendantsOf: (id: number) => Set<number>;
  /** The resolved path, or an empty string for an id that is not there. */
  pathOf: (id: number) => string;
}

/**
 * The one tree question the client still has to answer.
 *
 * Paths come from the backend — building them here as well would be a second
 * copy of the rule, which is how the string locations drifted. What cannot come
 * from the backend is "does this printer's location belong to the subtree the
 * operator picked", because the filtering happens on rows already in hand.
 */
export function buildLocationIndex(rows: LocationNode[]): LocationIndex {
  const children = new Map<number, number[]>();
  const paths = new Map<number, string>();
  for (const row of rows) {
    paths.set(row.id, row.path);
    if (row.parent_id != null) {
      children.set(row.parent_id, [...(children.get(row.parent_id) ?? []), row.id]);
    }
  }

  return {
    descendantsOf(id: number) {
      if (!paths.has(id)) return new Set<number>();
      const found = new Set<number>([id]);
      const pending = [id];
      while (pending.length) {
        for (const child of children.get(pending.pop()!) ?? []) {
          if (!found.has(child)) {
            found.add(child);
            pending.push(child);
          }
        }
      }
      return found;
    },
    pathOf: (id: number) => paths.get(id) ?? '',
  };
}

/** A filter value that survived the move from names to ids.
 *
 * Before locations became a tree the stored filter was a NAME. Left alone it
 * parses as NaN, matches no subtree, and the page comes up empty after an
 * upgrade with nothing saying why — so anything that is not a number is read
 * as "all".
 */
export function readStoredLocationFilter(raw: string | null): string {
  if (!raw || raw === 'all') return 'all';
  return Number.isFinite(Number(raw)) ? raw : 'all';
}
