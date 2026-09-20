/**
 * The round-robin deal behind the print dialog's «Total» quantity mode
 * (spec 2026-09-11 §3.1): one copy at a time over the picked printers, the
 * cursor carrying from plate to plate so several plates' tails do not all
 * land on the first printer.
 */
import { describe, it, expect } from 'vitest';
import { splitRoundRobin } from '../../lib/quantitySplit';

describe('splitRoundRobin', () => {
  it('deals 13 over 3 as 5 / 4 / 4', () => {
    expect(splitRoundRobin([13], 3)).toEqual([[5, 4, 4]]);
  });

  it('carries the cursor from one plate to the next', () => {
    // Plate 1 leaves the cursor after target 0 (13 = 4 rounds + 1), so plate 2's
    // two copies go to targets 1 and 2 — not back onto target 0.
    expect(splitRoundRobin([13, 2], 3)).toEqual([[5, 4, 4], [0, 1, 1]]);
  });

  it('keeps every row summing to its total and no two targets more than one apart', () => {
    const rows = splitRoundRobin([7, 1, 9, 0], 4);
    rows.forEach((row, i) => {
      expect(row.reduce((a, b) => a + b, 0)).toBe([7, 1, 9, 0][i]);
      expect(Math.max(...row) - Math.min(...row)).toBeLessThanOrEqual(1);
    });
  });

  it('is the identity for one target', () => {
    expect(splitRoundRobin([4, 2], 1)).toEqual([[4], [2]]);
  });

  it('gives rows of zeros for a zero total and empty rows for no targets', () => {
    expect(splitRoundRobin([0], 3)).toEqual([[0, 0, 0]]);
    expect(splitRoundRobin([5], 0)).toEqual([[]]);
  });
});
