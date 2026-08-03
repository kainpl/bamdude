import { describe, expect, it } from 'vitest';

import { buildLocationIndex } from '../../utils/locationTree';
import type { LocationNode } from '../../utils/locationTree';

// Workshop(1) -> Shelf 1(2) -> Box(4);  Workshop -> Shelf 2(3);  Hall(5)
const ROWS: LocationNode[] = [
  { id: 1, name: 'Workshop', parent_id: null, path: 'Workshop', depth: 1 },
  { id: 2, name: 'Shelf 1', parent_id: 1, path: 'Workshop / Shelf 1', depth: 2 },
  { id: 3, name: 'Shelf 2', parent_id: 1, path: 'Workshop / Shelf 2', depth: 2 },
  { id: 4, name: 'Box', parent_id: 2, path: 'Workshop / Shelf 1 / Box', depth: 3 },
  { id: 5, name: 'Hall', parent_id: null, path: 'Hall', depth: 1 },
];

describe('buildLocationIndex', () => {
  it('a subtree includes its own root', () => {
    // Filtering on the workshop has to keep a printer standing on the workshop
    // itself, not only the ones on its shelves.
    expect([...buildLocationIndex(ROWS).descendantsOf(1)].sort()).toEqual([1, 2, 3, 4]);
  });

  it('a leaf is its own subtree', () => {
    expect([...buildLocationIndex(ROWS).descendantsOf(4)]).toEqual([4]);
  });

  it('a sibling branch is not swept in', () => {
    // Widening this to the whole tree would make the filter meaningless, and
    // nothing on screen would say so.
    expect(buildLocationIndex(ROWS).descendantsOf(1).has(5)).toBe(false);
  });

  it('an unknown id is empty rather than an exception', () => {
    // A filter left over from before a location was deleted must narrow to
    // nothing, not break the page.
    expect(buildLocationIndex(ROWS).descendantsOf(999).size).toBe(0);
  });

  it('the path comes straight from the row', () => {
    expect(buildLocationIndex(ROWS).pathOf(4)).toBe('Workshop / Shelf 1 / Box');
    expect(buildLocationIndex(ROWS).pathOf(999)).toBe('');
  });
});

describe('ancestorsOf', () => {
  it('starts at the location itself', () => {
    // The workshop's own sensor has to show in the workshop's own group, not
    // only in its children's.
    expect(buildLocationIndex(ROWS).ancestorsOf(1)).toEqual([1]);
  });

  it('walks up to the root, nearest first', () => {
    // The order IS the display order: the nearest sensor reads leftmost.
    expect(buildLocationIndex(ROWS).ancestorsOf(4)).toEqual([4, 2, 1]);
  });

  it('does not reach into a sibling branch', () => {
    expect(buildLocationIndex(ROWS).ancestorsOf(2)).not.toContain(3);
  });

  it('an unknown id is empty rather than an exception', () => {
    // A group whose location was deleted mid-session must show no sensors, not
    // break the page.
    expect(buildLocationIndex(ROWS).ancestorsOf(999)).toEqual([]);
  });

  it('terminates on a cycle', () => {
    // The backend refuses cycles, but a corrupt row must not hang the browser.
    const looped: LocationNode[] = [
      { id: 1, name: 'A', parent_id: 2, path: 'A', depth: 1 },
      { id: 2, name: 'B', parent_id: 1, path: 'B', depth: 1 },
    ];
    expect(buildLocationIndex(looped).ancestorsOf(1).sort()).toEqual([1, 2]);
  });
});
