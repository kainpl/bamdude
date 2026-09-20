import type { PrinterTag } from '../api/client';

export interface TagGroup<T> {
  /** Null for the untagged group. */
  tagId: number | null;
  label: string;
  color: string | null;
  items: T[];
}

/**
 * Rows grouped by the tags they wear — a row with three tags appears in three
 * groups, because "the printers of Phase 1" is the honest answer to a
 * per-tag view. Tag groups are ordered by name; the untagged group, if any,
 * comes last. An ARRAY, for the same reason `groupByLocation` is: an object
 * keyed by integer ids would be re-ordered by the engine.
 */
export function groupByTag<T>(
  items: T[],
  tagsOf: (item: T) => Pick<PrinterTag, 'id' | 'name' | 'color'>[] | undefined,
  ungroupedLabel: string,
): TagGroup<T>[] {
  const byId = new Map<number, TagGroup<T>>();
  const untagged: TagGroup<T> = { tagId: null, label: ungroupedLabel, color: null, items: [] };
  for (const item of items) {
    const tags = tagsOf(item) ?? [];
    if (tags.length === 0) {
      untagged.items.push(item);
      continue;
    }
    for (const tag of tags) {
      let group = byId.get(tag.id);
      if (!group) {
        group = { tagId: tag.id, label: tag.name, color: tag.color ?? null, items: [] };
        byId.set(tag.id, group);
      }
      group.items.push(item);
    }
  }
  const groups = [...byId.values()].sort((a, b) => a.label.localeCompare(b.label));
  if (untagged.items.length > 0) groups.push(untagged);
  return groups;
}
