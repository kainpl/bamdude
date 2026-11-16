/**
 * The name of the spool assigned to each AMS slot, keyed by global tray id.
 *
 * The print dialog described a slot from telemetry alone, and a printer cannot
 * describe a spool it did not sell: a tray record has no brand, no subtype for a
 * non-Bambu spool, and a bare hex that resolves against Bambu's own catalogue —
 * a Devil Design PLA Basic Orange read "PLA (Sunflower Yellow)" while the
 * printer card named it correctly (upstream d5c70477). The assignment is the
 * user's word for what is in the slot, so it names the slot, through the same
 * spool-name template every other surface uses. Matching is untouched: routing
 * still reads type, colour and tray_info_idx off telemetry.
 */
import type { InventorySpool } from '../api/client';
import { getGlobalTrayId } from './amsHelpers';
import { formatSpoolDisplayName } from './spoolName';

interface SlotRow {
  ams_id: number;
  tray_id: number;
}

export function slotSpoolNames(input: {
  spoolmanMode: boolean;
  template: string | null | undefined;
  /** Internal inventory: `/inventory/assignments?printer_id=…`. */
  assignments?: readonly (SlotRow & { spool?: InventorySpool | null })[];
  /** Spoolman: `/spoolman/inventory/slot-assignments/all?printer_id=…`. */
  spoolmanAssignments?: readonly (SlotRow & { spoolman_spool_id: number })[];
  spoolmanSpools?: readonly InventorySpool[];
}): Map<number, string> {
  const names = new Map<number, string>();
  const put = (row: SlotRow, spool: InventorySpool | null | undefined) => {
    if (!spool) return;
    const name = formatSpoolDisplayName(spool, input.template).trim();
    if (name) names.set(getGlobalTrayId(row.ams_id, row.tray_id, row.ams_id === 255), name);
  };
  if (input.spoolmanMode) {
    const byId = new Map((input.spoolmanSpools ?? []).map((s) => [s.id, s]));
    for (const row of input.spoolmanAssignments ?? []) put(row, byId.get(row.spoolman_spool_id));
  } else {
    for (const row of input.assignments ?? []) put(row, row.spool);
  }
  return names;
}

/** How the spool bound to a slot is drawn: its own colours and effect. */
export interface SlotSpoolSwatch {
  rgba: string | null;
  extraColors: string | null;
  effectType: string | null;
  subtype: string | null;
}

/**
 * The colours of the spool bound to each slot, keyed by global tray id
 * (upstream #3159). A tray record carries one colour; a two-tone or glitter
 * spool is only itself in the inventory, so a bound spool is drawn from there
 * and an unbound slot from what the printer reports. Display only.
 */
export function slotSpoolSwatches(input: {
  spoolmanMode: boolean;
  assignments?: readonly (SlotRow & { spool?: InventorySpool | null })[];
  spoolmanAssignments?: readonly (SlotRow & { spoolman_spool_id: number })[];
  spoolmanSpools?: readonly InventorySpool[];
}): Map<number, SlotSpoolSwatch> {
  const swatches = new Map<number, SlotSpoolSwatch>();
  const put = (row: SlotRow, spool: InventorySpool | null | undefined) => {
    if (!spool?.rgba) return;
    swatches.set(getGlobalTrayId(row.ams_id, row.tray_id, row.ams_id === 255), {
      rgba: spool.rgba,
      extraColors: spool.extra_colors ?? null,
      effectType: spool.effect_type ?? null,
      subtype: spool.subtype ?? null,
    });
  };
  if (input.spoolmanMode) {
    const byId = new Map((input.spoolmanSpools ?? []).map((s) => [s.id, s]));
    for (const row of input.spoolmanAssignments ?? []) put(row, byId.get(row.spoolman_spool_id));
  } else {
    for (const row of input.assignments ?? []) put(row, row.spool);
  }
  return swatches;
}

/** What the print dialog shows about the spool bound to a slot. */
export interface SlotSpoolFacts {
  /** The per-kg rate a print off the spool is charged; null → the farm rate. */
  costPerKg: number | null;
  /** Whole grams left on the spool. */
  remainingGrams: number;
}

/**
 * The charged rate and the grams left of the spool bound to each slot, keyed
 * by global tray id (upstream 5649f836d).
 *
 * The dialog read both off the built-in assignment rows in both inventory
 * modes. In Spoolman mode those hold nothing, so a print off a priced Spoolman
 * spool was estimated at the farm rate and then archived at the Spoolman one.
 * The binding is the active inventory's: in Spoolman mode the Spoolman spool
 * the slot is bound to, priced by `effective_cost_per_kg` — the charge's own
 * rule (spool price, else its filament's; unpriced grams go at the farm rate).
 * Built-in mode keeps its rule: the spool's rate as stored, 0 included.
 */
export function slotSpoolFacts(input: {
  spoolmanMode: boolean;
  assignments?: readonly (SlotRow & { spool?: InventorySpool | null })[];
  spoolmanAssignments?: readonly (SlotRow & { spoolman_spool_id: number })[];
  spoolmanSpools?: readonly InventorySpool[];
}): Map<number, SlotSpoolFacts> {
  const facts = new Map<number, SlotSpoolFacts>();
  const put = (row: SlotRow, spool: InventorySpool | null | undefined, costPerKg: number | null) => {
    if (!spool) return;
    facts.set(getGlobalTrayId(row.ams_id, row.tray_id, row.ams_id === 255), {
      costPerKg,
      remainingGrams: Math.max(0, Math.round((spool.label_weight ?? 0) - (spool.weight_used ?? 0))),
    });
  };
  if (input.spoolmanMode) {
    const byId = new Map((input.spoolmanSpools ?? []).map((s) => [s.id, s]));
    for (const row of input.spoolmanAssignments ?? []) {
      const spool = byId.get(row.spoolman_spool_id);
      put(row, spool, spool?.effective_cost_per_kg ?? null);
    }
  } else {
    for (const row of input.assignments ?? []) put(row, row.spool, row.spool?.cost_per_kg ?? null);
  }
  return facts;
}
