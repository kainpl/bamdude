/**
 * The print dialog must pick the same spool the scheduler would.
 *
 * Ported from upstream #2804 / #2823 with its #2804 follow-up folded in. Two
 * halves, and both matter for the same reason: **what this dialog shows is what
 * gets dispatched**, because the backend will not re-derive a mapping the
 * dialog already pinned. A badge that disagrees with the scheduler is not a
 * cosmetic bug — it is a promise about which spool will be used.
 *
 * ⚠️ The CIEDE2000 implementation here mirrors
 * `backend/app/utils/color_utils.py`. It is verified against the same published
 * reference values the backend test uses, so the two cannot drift silently.
 */

import { readFileSync } from 'node:fs';
import { describe, it, expect } from 'vitest';

import { colorDistance, nearerColour, autoMatchFilament, filamentTypesCompatible } from '../../utils/amsHelpers';

describe('colorDistance', () => {
  // Sharma, Wu & Dalal reference pairs, expressed as the sRGB hexes that
  // produce them — the backend test drives L*a*b* directly; this one has to go
  // through the conversion, so it checks the pair end to end.
  it('is zero for a colour against itself', () => {
    expect(colorDistance('1E4821', '1E4821')).toBeCloseTo(0, 9);
  });

  it('is symmetric', () => {
    expect(colorDistance('1E4821', '38202F')).toBeCloseTo(colorDistance('38202F', '1E4821')!, 12);
  });

  it('accepts a leading hash', () => {
    expect(colorDistance('#1E4821', '1E4821')).toBeCloseTo(0, 9);
  });

  it('ignores alpha — a transparent filament must still match itself', () => {
    expect(colorDistance('1E4821FF', '1E482100')).toBeCloseTo(0, 9);
  });

  it('returns null rather than a number for something unreadable', () => {
    for (const bad of ['', undefined, '12345', 'ZZZZZZ']) {
      expect(colorDistance(bad, '1E4821')).toBeNull();
      expect(colorDistance('1E4821', bad)).toBeNull();
    }
  });

  it('agrees with the backend to the digit the backend test pins', () => {
    // ⚠️ These are the backend's own measured outputs. If either side changes,
    // this fails — which is the point: the dialog must not promise a spool the
    // scheduler would not pick.
    expect(colorDistance('1E4821', '007040')).toBeCloseTo(13.421021813237324, 6);
    expect(colorDistance('1E4821', '301020')).toBeCloseTo(44.193208251361014, 6);
  });

  it('disagrees with RGB where RGB is wrong', () => {
    // Same measured pair as the backend test: a required dark green with two
    // candidates at the same RGB distance, one green and one dark purple.
    const green = colorDistance('1E4821', '007040')!;
    const purple = colorDistance('1E4821', '301020')!;
    expect(green).toBeLessThan(purple / 3);
  });
});

describe('nearerColour', () => {
  it('takes the first candidate when there is no incumbent', () => {
    const only = { color: '2FA04F' };
    expect(nearerColour(undefined, only, '30A050')).toBe(only);
  });

  it('prefers the closer of two', () => {
    const far = { color: '1E9040' };
    const near = { color: '2FA04F' };
    expect(nearerColour(far, near, '30A050')).toBe(near);
    expect(nearerColour(near, far, '30A050')).toBe(near);
  });

  it('keeps the incumbent when the candidate has no readable colour', () => {
    // Unreadable is not "far", but it is not evidence of nearness either.
    const incumbent = { color: '2FA04F' };
    expect(nearerColour(incumbent, { color: '' }, '30A050')).toBe(incumbent);
  });

  it('takes an unreadable incumbent over nothing, then loses to a readable one', () => {
    const unreadable = { color: '' };
    const readable = { color: '2FA04F' };
    expect(nearerColour(undefined, unreadable, '30A050')).toBe(unreadable);
    expect(nearerColour(unreadable, readable, '30A050')).toBe(readable);
  });
});

describe('autoMatchFilament', () => {
  const tray = (globalTrayId: number, color: string, type = 'PLA') => ({ globalTrayId, color, type });

  it('takes the nearest eligible spool, not the first', () => {
    const picked = autoMatchFilament(
      { type: 'PLA', color: '30A050' },
      [tray(1, '1E9040'), tray(2, '2FA04F')],
      new Set(),
    );
    expect(picked?.globalTrayId).toBe(2);
  });

  it('and the earlier slot when that one is nearer', () => {
    const picked = autoMatchFilament(
      { type: 'PLA', color: '30A050' },
      [tray(1, '2FA04F'), tray(2, '1E9040')],
      new Set(),
    );
    expect(picked?.globalTrayId).toBe(1);
  });

  it('never lets a near match overtake an exact one', () => {
    const picked = autoMatchFilament(
      { type: 'PLA', color: '30A050' },
      [tray(1, '2FA04F'), tray(2, '30A050')],
      new Set(),
    );
    expect(picked?.globalTrayId).toBe(2);
  });

  it('still prefers a near match over a type-only fallback', () => {
    const picked = autoMatchFilament(
      { type: 'PLA', color: '30A050' },
      [tray(1, 'FF0000'), tray(2, '2FA04F')],
      new Set(),
    );
    expect(picked?.globalTrayId).toBe(2);
  });

  it('does not match a different type however close the colour', () => {
    const picked = autoMatchFilament(
      { type: 'PETG', color: '30A050' },
      [tray(1, '30A050', 'PLA')],
      new Set(),
    );
    expect(picked).toBeUndefined();
  });
});

describe('type comparison agrees with the scheduler everywhere', () => {
  it('treats the nylon group as one material', () => {
    expect(filamentTypesCompatible('PA-CF', 'PA12-CF')).toBe(true);
    expect(filamentTypesCompatible('PAHT-CF', 'PA-CF')).toBe(true);
  });

  it('does not treat product variants as interchangeable', () => {
    expect(filamentTypesCompatible('PLA Basic', 'PLA')).toBe(false);
  });

  it('leaves no raw type comparison behind in the mapping code', () => {
    // ⚠️ The badge used to compare raw strings while the scheduler grouped the
    // nylons, so it called a pairing the printer accepts a mismatch — and the
    // manual override picker, which groups by canonical type, then offered the
    // very spool the badge had rejected.
    for (const path of [
      'src/hooks/useFilamentMapping.ts',
      'src/hooks/useMultiPrinterFilamentMapping.ts',
      'src/components/PrintModal/PrinterSelector.tsx',
    ]) {
      const source = readFileSync(path, 'utf8');
      expect(source, path).not.toMatch(/\.type\?\.toUpperCase\(\) === \w+\.type\?\.toUpperCase\(\)/);
    }
  });
});
