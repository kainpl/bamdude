/**
 * Shift-click range selection over a list as it is SHOWN.
 *
 * `order` is the visible, sorted and filtered list of selectable keys. The
 * range runs from the anchor — the last key clicked without Shift — to the
 * target, both included, in that order. An anchor that is no longer visible
 * (the filter changed, the folder was left) gives no range: the click is then
 * an ordinary one, never a guess about rows the operator cannot see.
 */
export function rangeBetween(order: readonly string[], anchor: string | null, target: string): string[] {
  if (anchor === null) return [];
  const from = order.indexOf(anchor);
  const to = order.indexOf(target);
  if (from < 0 || to < 0) return [];
  return order.slice(Math.min(from, to), Math.max(from, to) + 1);
}

/** The selection after a click on `target`: a Shift-click adds the range, a plain one toggles. */
export function clickSelection(
  selected: ReadonlySet<string>,
  order: readonly string[],
  anchor: string | null,
  target: string,
  shift: boolean,
): { selected: Set<string>; anchor: string } {
  const next = new Set(selected);
  const range = shift ? rangeBetween(order, anchor, target) : [];
  if (range.length > 0) {
    for (const key of range) next.add(key);
    // The anchor stays: a second Shift-click re-aims the same range.
    return { selected: next, anchor: anchor as string };
  }
  if (next.has(target)) next.delete(target);
  else next.add(target);
  return { selected: next, anchor: target };
}
