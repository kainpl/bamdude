import type { InventorySpool } from '../api/client';
import { resolveSpoolColorName } from './colors';

/**
 * Return true when spool matches the search query across all searchable text fields.
 * Case-insensitive. Empty query always returns true.
 */
export function spoolMatchesQuery(spool: InventorySpool, query: string): boolean {
  if (!query) return true;
  const q = query.toLowerCase();
  return (
    // Numeric spool ID — typing the Spoolman ID into the Assign Spool
    // dialog or Inventory search used to return nothing (upstream
    // Bambuddy #1336).
    String(spool.id).includes(q) ||
    spool.material.toLowerCase().includes(q) ||
    (spool.brand?.toLowerCase().includes(q) ?? false) ||
    (spool.color_name?.toLowerCase().includes(q) ?? false) ||
    // The name on screen as well as the stored one (upstream e4a9ef45, #3090):
    // a tag may carry no name or an internal code, and Spoolman has no colour
    // name at all, so what the list shows is usually resolved from the hex.
    (resolveSpoolColorName(spool.color_name, spool.rgba, spool.color_name_is_synthesized)
      ?.toLowerCase()
      .includes(q) ??
      false) ||
    (spool.subtype?.toLowerCase().includes(q) ?? false) ||
    (spool.note?.toLowerCase().includes(q) ?? false) ||
    (spool.slicer_filament_name?.toLowerCase().includes(q) ?? false) ||
    (spool.storage_location?.toLowerCase().includes(q) ?? false)
  );
}

/** Filter a spool list by a free-text search query. */
export function filterSpoolsByQuery(spools: InventorySpool[], query: string): InventorySpool[] {
  if (!query) return spools;
  return spools.filter((spool) => spoolMatchesQuery(spool, query));
}
