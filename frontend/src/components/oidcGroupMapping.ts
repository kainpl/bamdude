/** N07: the rows of the OIDC group-mapping editor and the mapping a save sends. */

export type MappingRow = { idp: string; groupId: number | '' };

export function toRows(mapping: Record<string, number> | null | undefined): MappingRow[] {
  return Object.entries(mapping ?? {}).map(([idp, groupId]) => ({ idp, groupId }));
}

export type MappingResult = {
  /** Own properties only (Object.fromEntries), so a group literally named "__proto__" survives. */
  mapping: Record<string, number>;
  /** IdP group names two rows share after trimming; a save is refused while any exist. */
  duplicates: string[];
};

/**
 * The mapping a save sends: rows with a name and a group that still exists (spec §F).
 * ``existingGroupIds`` is null while the group list has not loaded: nothing is judged
 * deleted then, so a save before it arrives cannot strip the mapping.
 */
export function rowsToMapping(rows: MappingRow[], existingGroupIds: Set<number> | null): MappingResult {
  const seen = new Set<string>();
  const duplicates = new Set<string>();
  for (const row of rows) {
    const idp = row.idp.trim();
    if (!idp) continue;
    if (seen.has(idp)) duplicates.add(idp);
    seen.add(idp);
  }
  const pairs: [string, number][] = [];
  for (const row of rows) {
    const idp = row.idp.trim();
    if (!idp || duplicates.has(idp) || row.groupId === '') continue;
    if (existingGroupIds !== null && !existingGroupIds.has(row.groupId)) continue;
    pairs.push([idp, row.groupId]);
  }
  return { mapping: Object.fromEntries(pairs), duplicates: [...duplicates] };
}
