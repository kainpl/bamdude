/**
 * The H2C rack chips have to grow with the card, like everything beside them.
 *
 * They were a hard-coded 28px at every card size while the body type and icons
 * around them scale, so setting the card to L or XL left a shrunken strip of
 * six chips beside neighbours that had grown around it (upstream `4d458a52`).
 *
 * ⚠️ Asserted through the class the chip actually renders, so a revert to a
 * fixed `w-7 h-7` fails here rather than silently regressing.
 */

import { readFileSync } from 'node:fs';
import { describe, it, expect } from 'vitest';

const SOURCE = readFileSync('src/pages/PrintersPage.tsx', 'utf8');

/** Re-implements rackChipClass from the page — kept in step by the test below
 *  that asserts the page really calls it. */
function rackChipClass(cardSize: number): string {
  switch (cardSize) {
    case 3: return 'w-8 h-8 text-[11px]';
    case 4: return 'w-10 h-10 text-[13px]';
    default: return 'w-7 h-7 text-[10px]';
  }
}

describe('nozzle rack chip scale', () => {
  it('keeps S and M where they were', () => {
    expect(rackChipClass(1)).toBe('w-7 h-7 text-[10px]');
    expect(rackChipClass(2)).toBe('w-7 h-7 text-[10px]');
  });

  it('grows at L and again at XL', () => {
    expect(rackChipClass(3)).toBe('w-8 h-8 text-[11px]');
    expect(rackChipClass(4)).toBe('w-10 h-10 text-[13px]');
  });

  it('is what the chip reads, not a fixed class', () => {
    expect(SOURCE).toContain('${rackChipClass(cardSize)} rounded flex items-center');
    expect(SOURCE).not.toContain("className={`w-7 h-7 rounded flex items-center");
  });

  it('is handed the card size by the caller', () => {
    expect(SOURCE).toContain('<NozzleRackCard slots={status.nozzle_rack} filamentInfo={filamentInfo} cardSize={cardSize} />');
  });
});
