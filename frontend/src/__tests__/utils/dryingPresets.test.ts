/**
 * A spool's material must resolve to a row the preset table actually has.
 *
 * Ported from upstream #2774. The drying popover prefilled its material from
 * the loaded spool without checking the table had that material — and the
 * dropdown silently displays its FIRST option when handed a value outside its
 * list. So an AMS-HT holding Support for PLA/PETG (`tray_type` "PLA-S") read
 * "PLA" on screen while "PLA-S" was what the start command carried, and it fell
 * back to PLA's temperature for every composite: PETG-CF dried at PLA's 45 °C.
 *
 * ⚠️ Two values are seeded from this, not one — the temperature AND the filament
 * name sent to the printer. That is why "close enough" is not good enough here.
 */

import { readFileSync } from 'node:fs';
import { describe, it, expect } from 'vitest';

import { resolveDryingPresetKey, type DryingPreset } from '../../utils/dryingPresets';

const p = (n3f: number, n3s: number): DryingPreset => ({ n3f, n3s, n3f_hours: 12, n3s_hours: 12 });

/** The rows the shipped table has, as far as this test is concerned. */
const PRESETS: Record<string, DryingPreset> = {
  PLA: p(45, 45),
  PETG: p(65, 65),
  TPU: p(65, 75),
  ABS: p(80, 80),
  ASA: p(80, 80),
  PC: p(80, 80),
  PA: p(80, 85),
  PVA: p(65, 65),
};

describe('resolving a tray material', () => {
  it('takes an exact match', () => {
    expect(resolveDryingPresetKey('PETG', PRESETS)).toBe('PETG');
  });

  it('resolves a support material to its base', () => {
    // The reported case: PLA-S showed PLA and sent PLA-S.
    expect(resolveDryingPresetKey('PLA-S', PRESETS)).toBe('PLA');
    expect(resolveDryingPresetKey('PETG-S', PRESETS)).toBe('PETG');
  });

  it('resolves a composite to its base', () => {
    expect(resolveDryingPresetKey('PETG-CF', PRESETS)).toBe('PETG');
    expect(resolveDryingPresetKey('PLA-CF', PRESETS)).toBe('PLA');
    expect(resolveDryingPresetKey('ABS-GF', PRESETS)).toBe('ABS');
  });

  it('aliases nylon under its several spellings', () => {
    // Bambu labels the family "PA" in the table but spells it out on the spool.
    expect(resolveDryingPresetKey('PA6', PRESETS)).toBe('PA');
    expect(resolveDryingPresetKey('PAHT', PRESETS)).toBe('PA');
    expect(resolveDryingPresetKey('PAHT-CF', PRESETS)).toBe('PA');
    expect(resolveDryingPresetKey('Nylon', PRESETS)).toBe('PA');
  });

  it('takes the first word, as the AMS spells it', () => {
    expect(resolveDryingPresetKey('PETG Basic', PRESETS)).toBe('PETG');
    expect(resolveDryingPresetKey('pla basic', PRESETS)).toBe('PLA');
  });

  it('falls back to PLA — deliberately the coolest row', () => {
    // ⚠️ Under-drying an exotic filament wastes a cycle. Defaulting to PA's
    // 85 °C would deform a PLA spool.
    expect(resolveDryingPresetKey('SOMETHING-NEW', PRESETS)).toBe('PLA');
    expect(PRESETS.PLA.n3f).toBe(Math.min(...Object.values(PRESETS).map((v) => v.n3f)));
  });

  it('handles an empty slot without throwing', () => {
    expect(resolveDryingPresetKey(undefined, PRESETS)).toBe('PLA');
    expect(resolveDryingPresetKey(null, PRESETS)).toBe('PLA');
    expect(resolveDryingPresetKey('', PRESETS)).toBe('PLA');
  });

  it('never answers with a key the table does not have', () => {
    // The property that matters: whatever comes back can be handed to the
    // dropdown AND to the printer without the two disagreeing.
    for (const trayType of ['PLA-S', 'PETG-CF', 'PAHT-CF', 'Nylon', 'UNOBTANIUM', '', 'PC FR']) {
      expect(Object.keys(PRESETS)).toContain(resolveDryingPresetKey(trayType, PRESETS));
    }
  });
});

describe('wiring', () => {
  const PAGE = readFileSync('src/pages/PrintersPage.tsx', 'utf8');

  it('both drying popovers seed through the resolver', () => {
    const uses = PAGE.split('resolveDryingPresetKey(firstTray?.tray_type, dryingPresets)').length - 1;
    expect(uses).toBe(2);
  });

  it('no popover still upper-cases the raw tray type by hand', () => {
    expect(PAGE).not.toContain("(firstTray?.tray_type || 'PLA').split(' ')[0].toUpperCase()");
  });
});
