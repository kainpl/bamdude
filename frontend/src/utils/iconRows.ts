/**
 * How the sidebar footer's icons are laid out across lines.
 *
 * Two rules, and the second is why this is code rather than a CSS class:
 * at most `maxPerRow` on a line, and **never a stub last line** — one or two
 * icons stranded under a full row reads as a mistake rather than a wrap.
 * `flex-wrap` can do the first and cannot do the second, because it breaks on
 * width and knows nothing about how many are left.
 *
 * The last line borrows from the one above it until it is respectable. That is
 * always possible: a donor row holds at most `maxPerRow` and stops giving at
 * `minLastRow`, so with the defaults it can spare two.
 */
export function iconRows<T>(items: T[], maxPerRow = 5, minLastRow = 3): T[][] {
  if (items.length === 0) return [];
  if (items.length <= maxPerRow) return [items];

  const rows: T[][] = [];
  for (let i = 0; i < items.length; i += maxPerRow) {
    rows.push(items.slice(i, i + maxPerRow));
  }

  const last = rows[rows.length - 1];
  const previous = rows[rows.length - 2];
  while (last.length < minLastRow && previous.length > minLastRow) {
    last.unshift(previous.pop()!);
  }

  return rows;
}
