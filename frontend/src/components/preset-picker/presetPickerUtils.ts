/**
 * Shared preset-picker types + pure helpers.
 *
 * Split out of ``PresetTripletPicker.tsx`` so that component file only
 * exports React components — ``react-refresh/only-export-components``
 * flags non-component exports (they break Fast Refresh / fail CI lint).
 */
import type { PresetRef, PresetSource, UnifiedPreset, UnifiedPresetsResponse } from '../../api/client';

// Slot is one of the three preset categories the slicer takes: machine,
// process, filament. Shared with SliceModal so both sites use the same
// vocabulary.
export type Slot = 'printer' | 'process' | 'filament';

// Manual mode tier display order — local imports first (operator's
// curated picks), then cloud (per-user), then standard bundled
// fallbacks. Identical to the SliceModal ordering.
export const TIER_ORDER = ['local', 'cloud', 'standard'] as const;

export type OwnerFilter = 'all' | 'custom' | 'builtin';

// Cloud setting_id prefixes Bambu uses for user-created presets — kept
// in sync with ``pages/ProfilesPage.tsx::isUserPreset`` and SliceModal.
// Local presets are user-imported by definition; standard presets are
// always built-in. Cloud splits by id prefix.
const _USER_CLOUD_PRESET_RE = /^(P[FPM]US|PF\d|PP\d)/;

export function isCustomPreset(p: UnifiedPreset): boolean {
  if (p.source === 'local') return true;
  if (p.source === 'standard') return false;
  return _USER_CLOUD_PRESET_RE.test(p.id);
}

export function matchesOwnerFilter(p: UnifiedPreset, filter: OwnerFilter): boolean {
  if (filter === 'all') return true;
  const custom = isCustomPreset(p);
  return filter === 'custom' ? custom : !custom;
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
  if (source !== 'cloud' && source !== 'local' && source !== 'standard') return null;
  return { source, id };
}
