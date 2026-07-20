import type { Printer } from '../api/client';

/** Minimal shape needed to label/order a printer — the full `Printer` satisfies it. */
export interface PrinterLike {
  id: number;
  name: string;
  archived?: boolean;
}

/**
 * Display label for a printer id.
 *
 * Archived printers are intentionally shown as "Printer {id} (Archived)"
 * everywhere instead of their real name, so a retired printer reads the same
 * way in stats, archives and the calendar (its name may be reused or misleading
 * once it's out of service). Unknown/deleted ids fall back to "Printer {id}".
 */
export function printerLabel(
  printer: PrinterLike | undefined,
  id: number | string,
  t: (key: string) => string,
): string {
  const generic = `${t('common.printer')} ${id}`;
  if (!printer) return generic;
  return printer.archived ? `${generic} (${t('printers.archive.archivedSuffix')})` : printer.name;
}

/**
 * Comparator for printer objects: active printers first (A→Z by name), archived
 * sunk to the bottom (A→Z among themselves). Use for `Printer[]` arrays such as
 * filter dropdowns.
 */
export function comparePrinterLike(
  a: PrinterLike,
  b: PrinterLike,
  t: (key: string) => string,
): number {
  const aArchived = a.archived ? 1 : 0;
  const bArchived = b.archived ? 1 : 0;
  if (aArchived !== bArchived) return aArchived - bArchived;
  return printerLabel(a, a.id, t).localeCompare(printerLabel(b, b.id, t));
}

/**
 * Comparator that orders printer ids by display label, sinking archived (and
 * unknown/deleted) printers below the active ones. Both groups are sorted
 * alphabetically by their label within the group.
 */
export function comparePrinterByLabel(
  aId: number | string,
  bId: number | string,
  printerById: Map<string, Printer>,
  t: (key: string) => string,
): number {
  const a = printerById.get(String(aId));
  const b = printerById.get(String(bId));
  const aArchived = a?.archived ? 1 : 0;
  const bArchived = b?.archived ? 1 : 0;
  if (aArchived !== bArchived) return aArchived - bArchived;
  return printerLabel(a, aId, t).localeCompare(printerLabel(b, bId, t));
}
