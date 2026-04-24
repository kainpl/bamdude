import type { InventorySpool } from '../api/client';

/**
 * Synthesised spool display name, composed client-side from a user-configurable
 * template (Settings → Inventory → Spool display name template). The template
 * stays flexible without schema changes; search and sort on the Filaments page
 * interpolate it in-memory over the already-loaded inventory.
 *
 * Placeholder registry — register each entry with a stable key, UI label,
 * short description, and a formatter that reads the spool. Unknown placeholders
 * in a template are left verbatim so typos surface in the live preview instead
 * of silently collapsing to empty.
 */
export interface SpoolPlaceholder {
  key: string;
  label: string;
  description: string;
  example: string;
  format: (s: InventorySpool) => string;
}

const formatKg = (grams: number): string => {
  const kg = grams / 1000;
  // "1kg" for round values, "0.75" for fractional — keeps short labels short.
  return Number.isInteger(kg) ? String(kg) : kg.toFixed(2).replace(/\.?0+$/, '');
};

const remainingGrams = (s: InventorySpool): number =>
  Math.max(0, (s.label_weight ?? 0) - (s.weight_used ?? 0));

export const SPOOL_PLACEHOLDERS: SpoolPlaceholder[] = [
  {
    key: 'brand',
    label: 'Brand',
    description: 'Manufacturer name',
    example: 'Polymaker',
    format: (s) => s.brand ?? '',
  },
  {
    key: 'material',
    label: 'Material',
    description: 'PLA, PETG, ABS, …',
    example: 'PLA',
    format: (s) => s.material ?? '',
  },
  {
    key: 'subtype',
    label: 'Subtype',
    description: 'Basic, Matte, Silk, …',
    example: 'Matte',
    format: (s) => s.subtype ?? '',
  },
  {
    key: 'color_name',
    label: 'Color',
    description: 'Human-readable colour name',
    example: 'Jade White',
    format: (s) => s.color_name ?? '',
  },
  {
    key: 'slicer_filament_name',
    label: 'Slicer preset',
    description: 'Filament preset name as shown in the slicer',
    example: 'Polymaker PolyTerra PLA @Bambu Lab X1C',
    format: (s) => s.slicer_filament_name ?? '',
  },
  {
    key: 'note',
    label: 'Note',
    description: 'Free-form user note',
    example: 'Kitchen shelf',
    format: (s) => s.note ?? '',
  },
  {
    key: 'label_weight_g',
    label: 'Label weight (g)',
    description: 'Nominal weight of a full spool in grams',
    example: '1000',
    format: (s) => String(s.label_weight ?? 0),
  },
  {
    key: 'label_weight_kg',
    label: 'Label weight (kg)',
    description: 'Nominal weight of a full spool in kilograms',
    example: '1',
    format: (s) => formatKg(s.label_weight ?? 0),
  },
  {
    key: 'remaining_g',
    label: 'Remaining (g)',
    description: 'Label weight minus used, grams',
    example: '750',
    format: (s) => String(remainingGrams(s)),
  },
  {
    key: 'remaining_kg',
    label: 'Remaining (kg)',
    description: 'Label weight minus used, kilograms',
    example: '0.75',
    format: (s) => formatKg(remainingGrams(s)),
  },
  {
    key: 'remaining_pct',
    label: 'Remaining (%)',
    description: 'Remaining weight as a percentage of the label weight',
    example: '75%',
    format: (s) => {
      const lw = s.label_weight ?? 0;
      if (!lw) return '0%';
      return `${Math.round((remainingGrams(s) / lw) * 100)}%`;
    },
  },
  {
    key: 'color_hex',
    label: 'Color hex',
    description: '#RRGGBB (alpha dropped)',
    example: '#FF3300',
    format: (s) => (s.rgba ? `#${s.rgba.substring(0, 6).toUpperCase()}` : ''),
  },
  {
    key: 'cost_per_kg',
    label: 'Cost per kg',
    description: 'Cost per kilogram (bare number, no currency symbol)',
    example: '25',
    format: (s) => (s.cost_per_kg != null ? String(s.cost_per_kg) : ''),
  },
  {
    key: 'purchase_date',
    label: 'Purchase date',
    description: 'User-entered acquisition date (YYYY-MM-DD)',
    example: '2026-04-15',
    format: (s) => (s.purchase_date ? s.purchase_date.slice(0, 10) : ''),
  },
  {
    key: 'filament_diameter',
    label: 'Filament diameter',
    description: '1.75 or 2.85 (bare number, no unit)',
    example: '1.75',
    format: (s) => s.filament_diameter ?? '',
  },
  {
    key: 'lot',
    label: 'Lot',
    description: 'Position inside a purchase bundle / batch',
    example: '3',
    format: (s) => (s.lot != null ? String(s.lot) : ''),
  },
];

export const DEFAULT_SPOOL_DISPLAY_TEMPLATE = '{brand} {material} {color_name}';

const placeholderMap = new Map(SPOOL_PLACEHOLDERS.map((p) => [p.key, p]));

/**
 * Interpolate the template against a single spool.
 *
 * - Unknown placeholders (typos) are left verbatim so the Settings live preview
 *   flags them visibly rather than collapsing to a silent gap.
 * - Multiple adjacent whitespace characters are collapsed to one, and the
 *   result is trimmed — an empty placeholder at the start or end of the
 *   template doesn't leave a dangling space. This is the reason the front-end
 *   path is preferred over a SQL concat: SQL-side would need CASE / COALESCE
 *   gymnastics to get the same feel.
 */
export function formatSpoolDisplayName(
  spool: InventorySpool,
  template: string | undefined | null,
): string {
  const effective = template && template.trim() ? template : DEFAULT_SPOOL_DISPLAY_TEMPLATE;
  const replaced = effective.replace(/\{(\w+)\}/g, (match, key: string) => {
    const ph = placeholderMap.get(key);
    return ph ? ph.format(spool) : match;
  });
  return replaced.replace(/\s+/g, ' ').trim();
}

/**
 * Tokenised substring search. User input splits on whitespace; every token
 * must appear as a case-insensitive substring of the spool's display name.
 * Lets the operator type "SUN Bl" and match "SUNLU PETG Black" without
 * dragging in a fuzzy-match dependency.
 */
export function spoolDisplayNameMatches(displayName: string, query: string): boolean {
  const tokens = query
    .toLowerCase()
    .split(/\s+/)
    .filter((t) => t.length > 0);
  if (tokens.length === 0) return true;
  const hay = displayName.toLowerCase();
  return tokens.every((tok) => hay.includes(tok));
}
