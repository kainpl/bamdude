/**
 * A placeholder that keeps the inventory's shape while a cold list query runs.
 *
 * ⚠️ It replaces a centred spinner that swapped out the whole table, so every
 * cold load reflowed the page twice — and so did the first flip of "Group
 * similar", where the newly enabled query has no previous data of its own to
 * stand in and `placeholderData` cannot help it (the two are separate
 * observers, and a placeholder only ever sees its own observer's last result).
 *
 * Deliberately not solved by merging the two queries into one keyed on
 * `group_similar`: the placeholder would then be the OTHER shape — flat items
 * where the renderer expects grouped rows — and the render branches on state,
 * not on the shape of the data it was handed.
 */
export function SpoolTableSkeleton({
  rows,
  view,
  columns,
}: {
  rows: number;
  view: 'table' | 'cards';
  columns: number;
}) {
  if (view === 'cards') {
    return (
      <div
        data-testid="spool-table-skeleton"
        className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3"
      >
        {Array.from({ length: rows }, (_, i) => (
          <div key={i} className="h-40 rounded-lg bg-bambu-dark-secondary animate-pulse" />
        ))}
      </div>
    );
  }

  return (
    <div data-testid="spool-table-skeleton" className="space-y-2">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="flex gap-3">
          {Array.from({ length: Math.max(columns, 1) }, (_, c) => (
            <div key={c} className="h-6 flex-1 rounded bg-bambu-dark-secondary animate-pulse" />
          ))}
        </div>
      ))}
    </div>
  );
}
