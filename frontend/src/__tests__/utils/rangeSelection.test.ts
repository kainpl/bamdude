import { describe, expect, it } from 'vitest';
import { clickSelection, rangeBetween } from '../../utils/rangeSelection';

const ORDER = ['a', 'b', 'c', 'd', 'e'];

describe('rangeBetween', () => {
  it('is inclusive in the visible order, whichever end was clicked first', () => {
    expect(rangeBetween(ORDER, 'b', 'd')).toEqual(['b', 'c', 'd']);
    expect(rangeBetween(ORDER, 'd', 'b')).toEqual(['b', 'c', 'd']);
    expect(rangeBetween(ORDER, 'c', 'c')).toEqual(['c']);
  });

  it('gives nothing when either end is not shown', () => {
    expect(rangeBetween(ORDER, null, 'c')).toEqual([]);
    expect(rangeBetween(ORDER, 'gone', 'c')).toEqual([]);
    expect(rangeBetween(ORDER, 'a', 'gone')).toEqual([]);
  });
});

describe('clickSelection', () => {
  it('toggles on a plain click and moves the anchor', () => {
    const first = clickSelection(new Set(), ORDER, null, 'b', false);
    expect([...first.selected]).toEqual(['b']);
    expect(first.anchor).toBe('b');
    const second = clickSelection(first.selected, ORDER, first.anchor, 'b', false);
    expect(second.selected.size).toBe(0);
  });

  it('adds the whole range on a Shift-click and keeps the anchor', () => {
    const next = clickSelection(new Set(['a', 'b']), ORDER, 'b', 'e', true);
    expect([...next.selected].sort()).toEqual(['a', 'b', 'c', 'd', 'e']);
    expect(next.anchor).toBe('b');
  });

  it('never reaches rows that are not shown', () => {
    // The filter hides c: the range from b to d covers only what is on screen.
    const shown = ['a', 'b', 'd', 'e'];
    const next = clickSelection(new Set(['b']), shown, 'b', 'd', true);
    expect([...next.selected].sort()).toEqual(['b', 'd']);
  });

  it('treats a Shift-click without a visible anchor as a plain click', () => {
    const next = clickSelection(new Set(), ORDER, 'gone', 'c', true);
    expect([...next.selected]).toEqual(['c']);
    expect(next.anchor).toBe('c');
  });
});
