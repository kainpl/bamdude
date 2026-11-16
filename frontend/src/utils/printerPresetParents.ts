// "Printer preset → the preset it was saved from" (upstream 3acc52d93, #3250).
//
// A printer preset saved under a name of its own ("Bambu Lab H2D 0.4 nozzle -
// Skew") is listed in no process's or filament's `compatible_printers`, and
// its name may not state a model or a nozzle at all. The slicer checks against
// the preset it was saved from, and the backend reports that parent's NAME on
// the listing (`UnifiedPreset.inherits`, printer slot, never for a system
// preset). This module turns the listing into a lookup; who reads it — the
// compatibility matcher today, the online-printers filter later — is theirs.
//
// The parent is always taken from data, never guessed out of a name.

import type { UnifiedPresetsResponse } from '../api/client';
import { TIER_ORDER } from './presetPickerUtils';

/** Printer preset name → the name of the preset it was saved from. */
export type PrinterParents = Record<string, string>;

// A copy of a copy is the deepest chain a slicer writes; the bound only keeps a
// malformed listing from looping.
const MAX_HOPS = 8;

/**
 * Build the lookup from a preset listing. Sources are walked in the dialog's
 * tier order, and the first one that names a parent for a preset name keeps it.
 */
export function buildPrinterParents(data: UnifiedPresetsResponse | null | undefined): PrinterParents {
  const out: PrinterParents = {};
  if (!data) return out;
  for (const tier of TIER_ORDER) {
    for (const preset of data[tier].printer) {
      const parent = typeof preset.inherits === 'string' ? preset.inherits.trim() : '';
      if (!parent || parent === preset.name || Object.hasOwn(out, preset.name)) continue;
      out[preset.name] = parent;
    }
  }
  return out;
}

/**
 * The preset's own name, then every preset it was saved from, nearest first.
 * Stops at a name already seen, so presets that name each other end the walk.
 */
export function printerPresetLineage(name: string, parents: PrinterParents | null | undefined): string[] {
  const lineage = [name];
  if (!parents) return lineage;
  let current = name;
  for (let hop = 0; hop < MAX_HOPS; hop++) {
    const parent = Object.hasOwn(parents, current) ? parents[current] : null;
    if (typeof parent !== 'string' || !parent || lineage.includes(parent)) break;
    lineage.push(parent);
    current = parent;
  }
  return lineage;
}
