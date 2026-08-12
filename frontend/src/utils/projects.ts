/**
 * Which projects may be OFFERED for binding.
 *
 * Archiving a project is the explicit "I am done with this, stop showing it to
 * me" action — the same meaning it has for printers, where an archived one
 * disappears from every place that offers a choice rather than being labelled
 * in small print. A list that only ever grows punishes exactly the people who
 * tidy up after themselves.
 *
 * ⚠️ **A project already bound stays on the list even when archived.** Hiding it
 * would erase the binding from the screen: the row would render as "nothing
 * chosen", and the next save would write that emptiness back. This is the way
 * this kind of filter usually breaks, so the current ids are a parameter rather
 * than an afterthought.
 *
 * ⚠️ **`completed` is deliberately still offered.** Three statuses exist —
 * `active`, `completed`, `archived` — and only the last one means "put away". A
 * finished project can still take a reprint, and treating "done" as "gone"
 * would hide somewhere people legitimately file work.
 *
 * Not applied where the list is not a choice: the archives page reads it to look
 * up a project's colour, and an archive bound to an archived project still has
 * to be drawn.
 */
export function selectableProjects<T extends { id: number; status?: string | null }>(
  projects: T[] | undefined | null,
  keepIds?: Iterable<number> | null,
): T[] {
  if (!projects) return [];
  const keep = keepIds ? new Set(keepIds) : null;
  return projects.filter((p) => p.status !== 'archived' || keep?.has(p.id));
}
