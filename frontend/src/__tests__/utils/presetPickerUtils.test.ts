import { describe, it, expect } from 'vitest';
import {
  isOwnBambuCloudId,
  isOwnPreset,
  matchesSourceFilter,
  resolvePresetName,
} from '../../utils/presetPickerUtils';

// R1 (spec slicing-setup r5): one rule for "my preset". Orca Cloud lists only
// the user's own synced profiles; its ids are UUIDs, which the Bambu prefix
// rule used to read as built-in.
describe('isOwnPreset (R1)', () => {
  it('local is mine', () => expect(isOwnPreset({ source: 'local', id: '12' })).toBe(true));
  it('every Orca Cloud preset is mine', () =>
    expect(isOwnPreset({ source: 'orca_cloud', id: '3f2a-uuid' })).toBe(true));
  it('Bambu Cloud by the user prefix', () => {
    expect(isOwnPreset({ source: 'cloud', id: 'PFUS1a2b' })).toBe(true);
    expect(isOwnPreset({ source: 'cloud', id: 'GFSA00_02' })).toBe(false);
  });
  it('standard is built-in', () =>
    expect(isOwnPreset({ source: 'standard', id: 'Bambu PLA Basic' })).toBe(false));
  it('the Bambu id rule on its own', () => {
    expect(isOwnBambuCloudId('PPUS9')).toBe(true);
    expect(isOwnBambuCloudId('PF001')).toBe(true);
    expect(isOwnBambuCloudId('GFSA00')).toBe(false);
  });
  it('the three filter values', () => {
    const orca = { source: 'orca_cloud', id: 'u' } as const;
    expect([
      matchesSourceFilter(orca, 'mine'),
      matchesSourceFilter(orca, 'builtin'),
      matchesSourceFilter(orca, 'all'),
    ]).toEqual([true, false, true]);
  });
});
import { presetCompatibility, EMPTY_COMPATIBILITY_INDEX } from '../../utils/slicerPrinterMatch';
import type { UnifiedPresetsResponse } from '../../api/client';

// Minimal A1-mini catalogue. The real cloud printer preset name is
// "Bambu Lab A1 mini" (lowercase "mini") — the exact string the slicer writes
// into a process preset's compatible_printers.
const presets = {
  cloud: {
    printer: [{ id: 'PRINTER-A1M', name: 'Bambu Lab A1 mini', source: 'cloud' }],
    process: [
      { id: 'PP1', name: '0.20mm fast', source: 'cloud', compatible_printers: ['Bambu Lab A1 mini'] },
      { id: 'PP2', name: '0.20mm P1S thing', source: 'cloud', compatible_printers: ['Bambu Lab P1S'] },
    ],
    filament: [],
  },
  local: { printer: [], process: [], filament: [] },
  standard: { printer: [], process: [], filament: [] },
} as unknown as UnifiedPresetsResponse;

describe('resolvePresetName', () => {
  it('resolves a ref to the real catalogue preset name', () => {
    expect(resolvePresetName(presets, { source: 'cloud', id: 'PRINTER-A1M' }, 'printer')).toBe(
      'Bambu Lab A1 mini',
    );
  });

  it('returns null for an unset ref or missing catalogue', () => {
    expect(resolvePresetName(presets, null, 'printer')).toBeNull();
    expect(resolvePresetName(undefined, { source: 'cloud', id: 'x' }, 'printer')).toBeNull();
    expect(resolvePresetName(presets, { source: 'cloud', id: 'nope' }, 'printer')).toBeNull();
  });

  // Regression for the calibration profile-filtering bug: feeding the matcher
  // the *resolved* printer name (not a fabricated "Bambu Lab A1 Mini …") keeps
  // the printer's own process profiles and only drops genuinely foreign ones.
  it('keeps the printer\'s own process profiles when fed the resolved name (calibration bug)', () => {
    const printerName = resolvePresetName(presets, { source: 'cloud', id: 'PRINTER-A1M' }, 'printer');
    const [own, foreign] = presets.cloud.process;
    expect(presetCompatibility(own, 'process', printerName, EMPTY_COMPATIBILITY_INDEX)).toBe('match');
    expect(presetCompatibility(foreign, 'process', printerName, EMPTY_COMPATIBILITY_INDEX)).toBe(
      'mismatch',
    );
  });
});
