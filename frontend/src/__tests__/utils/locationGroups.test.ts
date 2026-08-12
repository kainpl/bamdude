import { describe, expect, it } from 'vitest';

import { groupByLocation } from '../../utils/locationGroups';

interface Row {
  name: string;
  location: { id: number; path: string } | null;
}

const ROWS: Row[] = [
  { name: 'p1', location: { id: 7, path: 'Workshop' } },
  { name: 'p2', location: { id: 2, path: 'Workshop / Shelf 1' } },
  { name: 'p3', location: { id: 7, path: 'Workshop' } },
  { name: 'p4', location: null },
];

describe('groupByLocation', () => {
  it('keeps the order the rows came in', () => {
    // THE reason this returns an array. An object keyed by a numeric id is
    // reordered by the engine — integer-like keys iterate in ascending numeric
    // order, whatever the insertion order — so the location-name sort the
    // caller just applied would be silently thrown away.
    const groups = groupByLocation(ROWS, (row) => row.location, 'Ungrouped');
    expect(groups.map((g) => g.locationId)).toEqual([7, 2, null]);
  });

  it('carries the id beside the label', () => {
    // The label is what the header prints; the id is what the sensors are
    // matched against. Neither can be derived from the other.
    const groups = groupByLocation(ROWS, (row) => row.location, 'Ungrouped');
    expect(groups[0]).toMatchObject({ locationId: 7, label: 'Workshop' });
    expect(groups[0].items).toHaveLength(2);
  });

  it('collects rows with no location under the given label', () => {
    const groups = groupByLocation(ROWS, (row) => row.location, 'Ungrouped');
    expect(groups[2]).toMatchObject({ locationId: null, label: 'Ungrouped' });
    expect(groups[2].items.map((r) => r.name)).toEqual(['p4']);
  });

  it('is an empty array for no rows', () => {
    expect(groupByLocation([], (row: Row) => row.location, 'Ungrouped')).toEqual([]);
  });
});
