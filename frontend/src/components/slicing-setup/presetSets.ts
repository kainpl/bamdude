// The sets of one preset field (spec slicing-setup r5, R2 / R3).
//
// Three sets come out of one listing:
//   * the PICK OFFER — what an automatic pick may choose from: the bound, the
//     presets that fit the selected printer, every filter that steers picks.
//     No relaxation, no pinned ref, no "other printers". It may be empty.
//   * the DISPLAY SET — what the list shows: the fitting presets after the
//     filters, relaxed (last filter first) when they would hide every one; in
//     group mode the presets for other printers trail in a group of their own;
//     when nothing fits at all, the bound in its source groups (#2982).
//   * the PINNED ref — the chosen preset, joined after the display set is
//     built, so a filter never hides or replaces a choice.
// Pure: no React, no I/O.
import type { PresetRef, UnifiedPreset, UnifiedPresetsResponse } from '../../api/client';
import { groupPresetsForDisplay, sortPresetsForDisplay, type PresetGroup } from '../../utils/presetDisplayOrder';
import { TIER_ORDER, type Slot } from '../../utils/presetPickerUtils';
import { presetCompatibility, type PrinterCompatibilityIndex } from '../../utils/slicerPrinterMatch';

export type SoftFilterId = 'source' | 'farm' | 'loaded';

export interface SoftFilter {
  id: SoftFilterId;
  /** What the list shows. */
  keep: (p: UnifiedPreset) => boolean;
  /** What picks may use; `null` = this filter is skipped for picks (a stale farm answer). */
  pickKeep: ((p: UnifiedPreset) => boolean) | null;
}

export interface PresetFieldInput {
  listing: UnifiedPresetsResponse;
  slot: Slot;
  /** What the place may ever offer (calibration's proven printers; the filament-family dialog's tiers). */
  bound?: { keep: (p: UnifiedPreset) => boolean; whenEmpty: 'disable' | 'showAll' };
  fit?: { printerName: string | null; compatIndex: PrinterCompatibilityIndex; mode: 'group' | 'hide' };
  /** In spec order: source, then farm or loaded. */
  filters: SoftFilter[];
  pinned: PresetRef | null;
}

export interface PresetField {
  pickKeep: (p: UnifiedPreset) => boolean;
  sections: { group: PresetGroup; entries: UnifiedPreset[] }[];
  others: UnifiedPreset[];
  relaxed: SoftFilterId[];
  noFit: boolean;
  boundEmpty: boolean;
  boundFallback: boolean;
  pinnedOutside: 'otherPrinter' | 'filtered' | 'outsideBound' | null;
}

const same = (a: UnifiedPreset, ref: PresetRef | null) => ref !== null && a.source === ref.source && a.id === ref.id;

/** Apply `filters`, dropping them from the end until `accept` holds; returns what stayed and what was dropped. */
function relax(
  entries: UnifiedPreset[],
  filters: SoftFilter[],
  accept: (shown: UnifiedPreset[]) => boolean,
): { active: SoftFilter[]; relaxed: SoftFilterId[] } {
  const active = [...filters];
  const relaxed: SoftFilterId[] = [];
  const apply = () => entries.filter((e) => active.every((f) => f.keep(e)));
  while (active.length > 0 && !accept(apply())) {
    relaxed.push((active.pop() as SoftFilter).id);
  }
  return { active, relaxed };
}

export function buildPresetField(input: PresetFieldInput): PresetField {
  const { listing, slot, bound, fit, filters, pinned } = input;
  const all = TIER_ORDER.flatMap((tier) => listing[tier][slot]);
  const inBound = (p: UnifiedPreset) => (bound ? bound.keep(p) : true);
  const boundSet = all.filter(inBound);
  const boundEmpty = boundSet.length === 0;
  const boundFallback = boundEmpty && bound?.whenEmpty === 'showAll';
  const base = boundFallback ? all : boundSet;

  const judged = Boolean(fit) && slot !== 'printer';
  const mismatch = (p: UnifiedPreset) =>
    judged &&
    presetCompatibility(p, slot as 'process' | 'filament', fit?.printerName ?? null, fit!.compatIndex) === 'mismatch';

  const pickKeep = (p: UnifiedPreset) =>
    inBound(p) && !mismatch(p) && filters.every((f) => f.pickKeep === null || f.pickKeep(p));

  const fitting = base.filter((p) => !mismatch(p));
  const other = base.filter(mismatch);
  const noFit = judged && fitting.length === 0 && other.length > 0;

  let main: UnifiedPreset[];
  let trailing: UnifiedPreset[] = [];
  let relaxed: SoftFilterId[] = [];
  if (noFit) {
    const r = relax(base, filters, (shown) => shown.length > 0);
    relaxed = r.relaxed;
    main = base.filter((e) => r.active.every((f) => f.keep(e)));
  } else {
    const r = relax(fitting, filters, (shown) => shown.length > 0 || fitting.length === 0);
    relaxed = r.relaxed;
    main = fitting.filter((e) => r.active.every((f) => f.keep(e)));
    if (fit?.mode === 'group') trailing = other.filter((e) => r.active.every((f) => f.keep(e)));
  }

  const pinnedPreset = pinned ? all.find((p) => same(p, pinned)) ?? null : null;
  let pinnedOutside: PresetField['pinnedOutside'] = null;
  if (pinnedPreset) {
    const shown = main.some((p) => same(p, pinned)) || trailing.some((p) => same(p, pinned));
    if (mismatch(pinnedPreset)) pinnedOutside = 'otherPrinter';
    else if (bound && !bound.keep(pinnedPreset)) pinnedOutside = 'outsideBound';
    else if (!shown) pinnedOutside = 'filtered';
    if (!shown) {
      if (mismatch(pinnedPreset) && !noFit) trailing = [...trailing, pinnedPreset];
      else main = [...main, pinnedPreset];
    }
  }

  return {
    pickKeep,
    sections: groupPresetsForDisplay(main, slot),
    others: sortPresetsForDisplay(trailing, slot),
    relaxed,
    noFit,
    boundEmpty,
    boundFallback,
    pinnedOutside,
  };
}
