/**
 * Which orders may be OFFERED for binding.
 *
 * An order has three statuses and only one of them is open work: `active`.
 * `completed` and `cancelled` are closed ledgers — a reprint for a finished
 * order is filed by reopening it, not by quietly appending to books somebody
 * has already settled. Offering a closed order in a picker is how a print
 * ends up counted against an invoice that was sent last month.
 *
 * ⚠️ **An order already bound stays on the list even when closed.** Hiding it
 * would erase the binding from the screen: the row would render as "nothing
 * chosen", and the next save would write that emptiness back. This is the way
 * this kind of filter usually breaks, so the current ids are a parameter
 * rather than an afterthought.
 *
 * Not applied where the list is not a choice: the archives page reads it to
 * look up an order's colour, and a print bound to a completed order still has
 * to be drawn.
 */
export function selectableProjects<T extends { id: number; status?: string | null }>(
  projects: T[] | undefined | null,
  keepIds?: Iterable<number> | null,
): T[] {
  if (!projects) return [];
  const keep = keepIds ? new Set(keepIds) : null;
  return projects.filter((p) => p.status === 'active' || keep?.has(p.id));
}

/**
 * Which products may be OFFERED when adding an order line or linking a file.
 *
 * A product leaves the catalog (`is_active === false`) as the explicit "stop
 * offering me this" action — the same meaning archiving carries for printers,
 * where a retired one disappears from every place that offers a choice rather
 * than being labelled in small print.
 *
 * ⚠️ **A product already bound stays on the list**, for the same reason a
 * bound order does: hiding it renders the field empty and the next save
 * commits that emptiness.
 *
 * ⚠️ **A missing flag counts as in the catalog.** Rows arrive from several
 * shapes (list item, detail, an embedded `ProductRef`), and an absent field is
 * "not told" rather than "retired" — defaulting the other way would empty a
 * picker whenever a lighter payload is what happens to be in hand.
 */
export function selectableProducts<T extends { id: number; is_active?: boolean }>(
  products: T[] | undefined | null,
  keepIds?: Iterable<number> | null,
): T[] {
  if (!products) return [];
  const keep = keepIds ? new Set(keepIds) : null;
  return products.filter((p) => p.is_active !== false || keep?.has(p.id));
}
