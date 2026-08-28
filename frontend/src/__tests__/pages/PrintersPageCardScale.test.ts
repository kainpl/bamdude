/**
 * The printer card's body type and icons scale with the card size.
 *
 * Switching a card from M to XL made it wider, enlarged the printer name and
 * the thumbnail, and left everything else where it was: AMS slot labels,
 * temperatures, filament names, status text and every small button stayed
 * pinned between 8 and 11 pixels — below the smallest size used anywhere else
 * in the app. Browser zoom is not an answer, because it enlarges the whole
 * page and so preserves the very disparity being complained about.
 *
 * ⚠️ S and M stay at 1.0 on purpose. S is the dense fleet view where density
 * IS the point, and M is the default — so an existing install looks identical
 * until the operator reaches for a size that is already asking for more room.
 */

import { describe, it, expect } from 'vitest';

import source from '../../pages/PrintersPage.tsx?raw';

/** Evaluate the scale table straight out of the source. */
function scaleFor(cardSize: number): number {
  const table = source.match(/const CARD_BODY_SCALE: Record<number, number> = (\{[^}]+\});/);
  if (!table) throw new Error('CARD_BODY_SCALE not found');
  return (JSON.parse(table[1].replace(/(\d+):/g, '"$1":')) as Record<string, number>)[String(cardSize)];
}

describe('printer card body scale', () => {
  it('leaves the two dense sizes exactly as they were', () => {
    expect(scaleFor(1)).toBe(1);
    expect(scaleFor(2)).toBe(1);
  });

  it('grows the body at the two large sizes', () => {
    expect(scaleFor(3)).toBe(1.2);
    expect(scaleFor(4)).toBe(1.4);
  });

  it('scales icons alongside text, so controls do not stay fiddly to hit', () => {
    // The icon variables are derived from the same scale as the type ones;
    // a table with only text entries would enlarge the labels and leave the
    // buttons beside them at 12px.
    const builder = source.slice(source.indexOf('function buildCardScaleStyle'));
    expect(builder).toContain("'--pc-t10'");
    expect(builder).toContain("'--pc-i3'");
    expect(builder).toContain("'--pc-i4'");
  });
});

describe('the converted sizes keep their old value as a fallback', () => {
  // ⚠️ This is what makes the conversion safe to apply across the whole card
  // subtree: a component that ALSO renders outside a card root — a portalled
  // popover — sees no variable and falls back to exactly the size it had.
  it.each([
    ['text-[length:var(--pc-t8,8px)]'],
    ['text-[length:var(--pc-t10,10px)]'],
    ['w-[var(--pc-i3,0.75rem)]'],
    ['w-[var(--pc-i4,1rem)]'],
  ])('%s', (needle) => {
    expect(source).toContain(needle);
  });

  it('never emits a variable reference without one', () => {
    const bare = source.match(/var\(--pc-[a-z0-9]+\)/g);
    expect(bare, `these would collapse to nothing outside a card: ${bare}`).toBeNull();
  });

  it('applies the variables at the card root', () => {
    expect(source).toContain('style={buildCardScaleStyle(cardSize)}');
  });
});
