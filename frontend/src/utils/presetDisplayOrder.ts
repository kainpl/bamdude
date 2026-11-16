// How the Slice dialog LISTS presets (upstream a9ffe5b7c, #3393) — display
// only. The auto-picks read the listing's own order (`slicePresetPicker`), so
// nothing here changes what is preselected.
//
// Two decisions live here:
//
//   1. Which group a preset is shown in. A group is a SOURCE, and the user's
//      own come first: Imported → Orca Cloud → the user's own Bambu Cloud
//      presets → Bambu Cloud's stock presets → Standard (the slicer's bundle).
//      ⚠️ A stock preset that Bambu Cloud and the bundle both carry under one
//      name stays TWO entries, in two groups: a name is not an identity, and
//      the two are different content (the bundle is the running sidecar's, the
//      cloud copy is Bambu Studio's). Upstream folds them into one; we do not.
//   2. The order inside a group: Bambu first, then Generic, then every other
//      brand; for filaments, support materials after the real ones; then by
//      name without case and with numbers by value, so "0.08mm" comes before
//      "0.20mm" and "PA6" before "PA12".

import type { PresetSource, UnifiedPreset } from '../api/client';
import { isCustomPreset, type Slot } from './presetPickerUtils';

/** The groups of a preset dropdown, in the order they are listed. */
export const PRESET_GROUP_ORDER = ['local', 'orca_cloud', 'cloud_own', 'cloud', 'standard'] as const;
export type PresetGroup = (typeof PRESET_GROUP_ORDER)[number];

/** The group a preset is listed in: its source, with Bambu Cloud split by owner. */
export function presetGroupOf(preset: Pick<UnifiedPreset, 'source' | 'id'>): PresetGroup {
  const source: PresetSource = preset.source;
  if (source === 'cloud') return isCustomPreset(preset as UnifiedPreset) ? 'cloud_own' : 'cloud';
  return source;
}

const NAME_TEXT_ORDER = new Intl.Collator(undefined, { sensitivity: 'base' });
// `split` with a capture group leaves the numbers at the odd indices.
const NAME_NUMBER = /(\d+(?:\.\d+)?)/;
const SUPPORT_MATERIAL = /\b(support|pva|bvoh|hips)\b/i;

function compareNameParts(a: string[], b: string[]): number {
  for (let i = 0; i < Math.min(a.length, b.length); i++) {
    const d = i % 2 ? Number(a[i]) - Number(b[i]) : NAME_TEXT_ORDER.compare(a[i], b[i]);
    if (d) return d;
  }
  return a.length - b.length;
}

// A GUI clone is listed where the preset it copies would be.
function displayName(name: string): string {
  return name.replace(/^#\s*/, '');
}

function brandRank(name: string, slot: Slot): number {
  const brand = /^bambu\b/i.test(name) ? 0 : /^generic\b/i.test(name) ? 1 : 2;
  return (slot === 'filament' && SUPPORT_MATERIAL.test(name) ? 3 : 0) + brand;
}

/** `presets` in display order. Returns a new array; the input is not touched. */
export function sortPresetsForDisplay<T extends Pick<UnifiedPreset, 'name'>>(presets: readonly T[], slot: Slot): T[] {
  return presets
    .map((preset) => {
      const name = displayName(preset.name);
      return { preset, rank: brandRank(name, slot), parts: name.split(NAME_NUMBER) };
    })
    .sort(
      (a, b) =>
        a.rank - b.rank ||
        compareNameParts(a.parts, b.parts) ||
        NAME_TEXT_ORDER.compare(a.preset.name, b.preset.name),
    )
    .map(({ preset }) => preset);
}

/**
 * Split one slot's presets into the dropdown's groups, each sorted, empty ones
 * left out. `presets` may mix sources (the "other printers" tail does).
 */
export function groupPresetsForDisplay(
  presets: readonly UnifiedPreset[],
  slot: Slot,
): { group: PresetGroup; entries: UnifiedPreset[] }[] {
  const byGroup = new Map<PresetGroup, UnifiedPreset[]>();
  for (const preset of presets) {
    const group = presetGroupOf(preset);
    const list = byGroup.get(group);
    if (list) list.push(preset);
    else byGroup.set(group, [preset]);
  }
  return PRESET_GROUP_ORDER.filter((group) => byGroup.has(group)).map((group) => ({
    group,
    entries: sortPresetsForDisplay(byGroup.get(group) as UnifiedPreset[], slot),
  }));
}
