/** N07: the rows of the OIDC group-mapping editor and the mapping a save sends. */

export type MappingRow = { idp: string; groupId: number | '' };

export function toRows(mapping: Record<string, number> | null | undefined): MappingRow[] {
  return Object.entries(mapping ?? {}).map(([idp, groupId]) => ({ idp, groupId }));
}

/**
 * The mapping a save sends: rows with a name and a group that still exists (spec §F).
 * ``existingGroupIds`` is null while the group list has not loaded: nothing is judged
 * deleted then, so a save before it arrives cannot strip the mapping.
 */
export function rowsToMapping(rows: MappingRow[], existingGroupIds: Set<number> | null): Record<string, number> {
  const mapping: Record<string, number> = {};
  for (const row of rows) {
    const idp = row.idp.trim();
    if (!idp || row.groupId === '' || (existingGroupIds !== null && !existingGroupIds.has(row.groupId))) continue;
    mapping[idp] = row.groupId;
  }
  return mapping;
}
