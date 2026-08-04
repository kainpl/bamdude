import { describe, expect, it } from 'vitest';

import { iconRows } from '../../utils/iconRows';

/** Row sizes, which is the whole subject. */
function shape(count: number, maxPerRow?: number, minLastRow?: number): number[] {
  const items = Array.from({ length: count }, (_, i) => i);
  return iconRows(items, maxPerRow, minLastRow).map((row) => row.length);
}

describe('iconRows', () => {
  it('keeps a short list on one line', () => {
    expect(shape(0)).toEqual([]);
    expect(shape(1)).toEqual([1]);
    expect(shape(5)).toEqual([5]);
  });

  it('never strands one or two icons under a full row', () => {
    // The rule that CSS cannot express: flex-wrap breaks on width and would
    // happily leave 5 + 1.
    expect(shape(6)).toEqual([3, 3]);
    expect(shape(7)).toEqual([4, 3]);
  });

  it('leaves a healthy last row alone', () => {
    expect(shape(8)).toEqual([5, 3]);
    expect(shape(9)).toEqual([5, 4]);
    expect(shape(10)).toEqual([5, 5]);
  });

  it('applies the rule to the last row when there are three', () => {
    expect(shape(11)).toEqual([5, 3, 3]);
    expect(shape(12)).toEqual([5, 4, 3]);
  });

  it('loses nothing and keeps the order', () => {
    // Borrowing moves icons between rows; dropping or reversing one would be
    // invisible in the shape assertions above.
    const items = ['a', 'b', 'c', 'd', 'e', 'f', 'g'];
    expect(iconRows(items).flat()).toEqual(items);
  });

  it('takes its bounds as arguments', () => {
    expect(shape(6, 4, 2)).toEqual([4, 2]);
    expect(shape(5, 3, 2)).toEqual([3, 2]);
  });
});
