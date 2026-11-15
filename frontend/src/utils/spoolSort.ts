import type { InventorySpool } from '../api/client';
import { formatSpoolDisplayName } from './spoolName';

/**
 * The operator's spool order (Settings → Filament): a chain of «field +
 * direction» levels stored as one setting, `material_asc,brand_asc,net_asc`.
 *
 * The server applies it to the local inventory (the slot-assignment picker and
 * the Filaments list while no column is chosen). A Spoolman list arrives whole
 * and unsorted, so the same chain is applied to it here — `compareByOrder`.
 * The field list mirrors `inventory_service.SPOOL_ORDER_KEYS`.
 */
export type SpoolSortDirection = 'asc' | 'desc';
export interface SpoolSortLevel {
  field: string;
  direction: SpoolSortDirection;
}

type Extractor = (spool: InventorySpool, template: string | undefined) => string | number;

const net = (s: InventorySpool) => Math.max(0, s.label_weight - s.weight_used);
const text = (value: string | null | undefined) => (value || '').toLowerCase();

const EXTRACTORS: Record<string, Extractor> = {
  display_name: (s, template) => formatSpoolDisplayName(s, template).toLowerCase(),
  material: (s) => text(s.material),
  subtype: (s) => text(s.subtype),
  brand: (s) => text(s.brand),
  color_name: (s) => text(s.color_name),
  slicer_filament: (s) => text(s.slicer_filament_name || s.slicer_filament),
  net,
  remaining: (s) => (s.label_weight > 0 ? net(s) / s.label_weight : 0),
  used: (s) => s.weight_used,
  label_weight: (s) => s.label_weight,
  added_time: (s) => s.created_at || '',
  purchase_date: (s) => s.purchase_date || '',
  last_used_time: (s) => s.last_used || '',
  storage_location: (s) => text(s.storage_location),
  cost_per_kg: (s) => s.cost_per_kg ?? 0,
  lot: (s) => s.lot ?? 0,
  id: (s) => s.id,
};

/** Every field a level may name, in the order the settings offer them. */
export const SPOOL_SORT_FIELDS: readonly string[] = Object.keys(EXTRACTORS);

/** Unknown fields, bad directions and repeats are dropped — as the server does. */
export function parseSpoolSortOrder(value: string | null | undefined): SpoolSortLevel[] {
  const levels: SpoolSortLevel[] = [];
  const seen = new Set<string>();
  for (const chunk of (value || '').split(',')) {
    const trimmed = chunk.trim();
    const cut = trimmed.lastIndexOf('_');
    if (cut <= 0) continue;
    const field = trimmed.slice(0, cut);
    const direction = trimmed.slice(cut + 1);
    if (!(field in EXTRACTORS) || seen.has(field)) continue;
    if (direction !== 'asc' && direction !== 'desc') continue;
    seen.add(field);
    levels.push({ field, direction });
  }
  return levels;
}

export function serializeSpoolSortOrder(levels: SpoolSortLevel[]): string {
  return levels.map((level) => `${level.field}_${level.direction}`).join(',');
}

/** A comparator for the chain; equal rows fall back to the newest spool first. */
export function compareByOrder(
  levels: SpoolSortLevel[],
  template?: string,
): (a: InventorySpool, b: InventorySpool) => number {
  return (a, b) => {
    for (const { field, direction } of levels) {
      const extract = EXTRACTORS[field];
      const va = extract(a, template);
      const vb = extract(b, template);
      const cmp = typeof va === 'string' && typeof vb === 'string'
        ? va.localeCompare(vb, undefined, { numeric: true })
        : va < vb ? -1 : va > vb ? 1 : 0;
      if (cmp !== 0) return direction === 'asc' ? cmp : -cmp;
    }
    return b.id - a.id;
  };
}
