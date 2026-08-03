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
