/**
 * Shared preset-picker types + pure helpers.
 *
 * Split out of ``PresetTripletPicker.tsx`` so that component file only
 * exports React components — ``react-refresh/only-export-components``
 * flags non-component exports (they break Fast Refresh / fail CI lint).
 */
import type { PresetRef, PresetSource, UnifiedPreset, UnifiedPresetsResponse } from '../api/client';

// Slot is one of the three preset categories the slicer takes: machine,
// process, filament. Shared with SliceModal so both sites use the same
// vocabulary.
export type Slot = 'printer' | 'process' | 'filament';

// Tier display + lookup order — local first (the operator explicitly
// imported them for this install), then Orca Cloud (metadata inlined via
// sync_pull), then Bambu Cloud (per-user, no inline metadata), then
// standard bundled fallbacks. The backend does NOT dedup tiers, so a
// same-named preset appears in each group; this order only drives auto-pick
// and rendering (#1712). Identical to the SliceModal ordering.
export const TIER_ORDER = ['local', 'orca_cloud', 'cloud', 'standard'] as const;

// The source filter of the slicing-setup family: every preset, the user's
// own, or the built-in ones (spec slicing-setup R1 / R2).
export type PresetSourceFilter = 'all' | 'mine' | 'builtin';

// The setting_id prefixes Bambu Cloud gives a user-created preset. This is
// the one copy of the pattern (a guard test keeps it so).
const OWN_BAMBU_CLOUD_ID = /^(P[FPM]US|PF\d|PP\d)/;

/** A Bambu Cloud setting id the user created (not a system preset). */
export function isOwnBambuCloudId(id: string): boolean {
  return OWN_BAMBU_CLOUD_ID.test(id);
}

/**
 * Is this preset the user's own (R1)? Imported presets are; every Orca Cloud
 * preset is (the tier holds only the user's own synced profiles, under UUIDs
 * the Bambu prefix rule would read as built-in); a Bambu Cloud preset is when
 * its id carries the user prefix; the slicer's bundled presets never are.
 */
export function isOwnPreset(p: Pick<UnifiedPreset, 'source' | 'id'>): boolean {
  if (p.source === 'local' || p.source === 'orca_cloud') return true;
  if (p.source === 'standard') return false;
  return isOwnBambuCloudId(p.id);
}

export function matchesSourceFilter(p: Pick<UnifiedPreset, 'source' | 'id'>, filter: PresetSourceFilter): boolean {
  if (filter === 'all') return true;
  const own = isOwnPreset(p);
  return filter === 'mine' ? own : !own;
}

export function toRefValue(ref: PresetRef | null): string {
  return ref ? `${ref.source}:${ref.id}` : '';
}

/**
 * Resolve a {@link PresetRef} to the real preset *name* from the catalogue.
 *
 * The name is the exact string the slicer writes into a process / filament
 * preset's `compatible_printers`, so this (NOT a reconstructed "Bambu Lab
 * <model> <nozzle>" string) is what the printer-compatibility matcher must be
 * fed. The calibration wizard previously fabricated a name from the hardware
 * short code, whose casing ("A1 Mini") didn't match the real preset name
 * ("A1 mini") — so a printer's own profiles were classed as a mismatch and
 * hidden. Returns null when the ref is unset or no longer in the catalogue
 * (the matcher then answers 'unknown' and nothing is hidden).
 */
export function resolvePresetName(
  presets: UnifiedPresetsResponse | undefined,
  ref: PresetRef | null,
  slot: Slot,
): string | null {
  if (!presets || !ref) return null;
  return presets[ref.source]?.[slot].find((p) => p.id === ref.id)?.name ?? null;
}

export function fromRefValue(raw: string): PresetRef | null {
  if (!raw) return null;
  const idx = raw.indexOf(':');
  if (idx < 0) return null;
  const source = raw.slice(0, idx) as PresetSource;
  const id = raw.slice(idx + 1);
  if (source !== 'orca_cloud' && source !== 'cloud' && source !== 'local' && source !== 'standard') return null;
  return { source, id };
}
