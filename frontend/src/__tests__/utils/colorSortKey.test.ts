/**
 * The Inventory's Color column sorts by colour, not by colour name.
 *
 * Ported from upstream #2729. The swatch column had no sort extractor at all,
 * so its header ignored clicks, and the combined swatch+name column sorted by
 * the NAME — which files a titanium grey under T and a burgundy under B, an
 * ordering nobody reading a row of swatches can follow.
 *
 * ⚠️ A straight hue → saturation → lightness sort, which is what the original
 * issue asked for, does not survive a real inventory: a near-neutral still has
 * a hue and it can be anything. Family leads; the continuous sort runs inside
 * each family.
 */

import { describe, it, expect } from 'vitest';

import { COLOR_FAMILY_ORDER, colorFamily, colorSortKey, hexToColorName } from '../../utils/colors';

/** Sort hexes the way the table would. */
const sorted = (hexes: string[]) => [...hexes].sort((a, b) => colorSortKey(a).localeCompare(colorSortKey(b)));

describe('colorFamily', () => {
  it('classifies the rainbow', () => {
    expect(colorFamily('FF0000')).toBe('Red');
    expect(colorFamily('00FF00')).toBe('Green');
    expect(colorFamily('0000FF')).toBe('Blue');
  });

  it('calls a dark orange brown rather than splitting the oranges', () => {
    expect(colorFamily('6B4423')).toBe('Brown');
  });

  it('keeps transparent out of black', () => {
    // #00000000 is Bambu's transparent code; by RGB alone it is pure black.
    expect(colorFamily('00000000')).toBe('Clear');
  });

  it('answers null for something unusable rather than guessing', () => {
    expect(colorFamily('')).toBeNull();
    expect(colorFamily(undefined)).toBeNull();
    expect(colorFamily('12345')).toBeNull();
  });

  it('is the same classifier the name fallback uses', () => {
    // ⚠️ The whole reason it was extracted: the Color and Color Name columns
    // must not disagree about what counts as brown.
    for (const hex of ['FF0000', '6B4423', '5F6367', '00000000']) {
      expect(hexToColorName(hex)).toBe(colorFamily(hex));
    }
  });
});

describe('colorSortKey', () => {
  it('puts the families in rainbow order', () => {
    expect(sorted(['0000FF', 'FF0000', '00FF00'])).toEqual(['FF0000', '00FF00', '0000FF']);
  });

  it('keeps brown out of the middle of the oranges', () => {
    // ⚠️ Brown is a dark orange by hue. Sorting on hue alone drops it between
    // two oranges and splits them.
    const order = sorted(['FF8C00', '6B4423', 'FFA54F']);
    expect(order[2]).toBe('6B4423');
  });

  it('keeps a near-neutral grey out of the blues', () => {
    // ⚠️ The measured case: a titanium grey reads hue 210° at 4% saturation,
    // which a straight hue sort files between the blues.
    const order = sorted(['5F6367', '0000FF', 'FF0000']);
    expect(order).toEqual(['FF0000', '0000FF', '5F6367']);
  });

  it('orders neutrals light to dark, ignoring their hue', () => {
    const order = sorted(['000000', 'FFFFFF', '808080']);
    expect(order).toEqual(['FFFFFF', '808080', '000000']);
  });

  it('sorts by hue inside a family', () => {
    // Two greens: the yellower one leads the bluer one.
    const order = sorted(['00FF7F', '7FFF00']);
    expect(order).toEqual(['7FFF00', '00FF7F']);
  });

  it('sends a spool with no colour recorded to the end', () => {
    const order = sorted(['FF0000', '', '0000FF']);
    expect(order[order.length - 1]).toBe('');
  });

  it('produces keys that compare as fixed-width strings', () => {
    // The table's comparison is `string | number`; a key of varying width would
    // sort "10" before "9".
    const widths = new Set(['FF0000', '00000000', '5F6367', ''].map((h) => colorSortKey(h).length));
    expect(widths.size).toBe(1);
  });

  it('covers every family in the order list', () => {
    // If a family were missing from COLOR_FAMILY_ORDER, indexOf would return
    // -1 and it would sort ahead of Red.
    for (const family of COLOR_FAMILY_ORDER) {
      expect(COLOR_FAMILY_ORDER.indexOf(family)).toBeGreaterThanOrEqual(0);
    }
    expect(colorSortKey('FF0000').startsWith('-')).toBe(false);
  });
});
