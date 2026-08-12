export interface LocationGroup<T> {
  /** Null for rows with no location. What the sensors are matched against. */
  locationId: number | null;
  /** What the header prints — the resolved path, or the caller's word for
   *  "no location". Cannot be derived from the id, which is why both are here. */
  label: string;
  items: T[];
}

/**
 * Rows grouped by the place they stand in, in the order they arrived.
 *
 * An ARRAY, and that is the whole point. The obvious implementation is an
 * object keyed by location id — and JavaScript reorders integer-like keys into
 * ascending numeric order whatever the insertion order, so the location-name
 * sort the caller just applied would be thrown away silently. The three pages
 * that call this have each already sorted their rows.
 */
export function groupByLocation<T>(
  items: T[],
  locationOf: (item: T) => { id: number; path: string } | null | undefined,
  ungroupedLabel: string,
): LocationGroup<T>[] {
  const groups: LocationGroup<T>[] = [];
  const byId = new Map<number | null, LocationGroup<T>>();

  for (const item of items) {
    const location = locationOf(item) ?? null;
    const id = location?.id ?? null;
    let group = byId.get(id);
    if (!group) {
      group = { locationId: id, label: location?.path || ungroupedLabel, items: [] };
      byId.set(id, group);
      groups.push(group);
    }
    group.items.push(item);
  }

  return groups;
}
