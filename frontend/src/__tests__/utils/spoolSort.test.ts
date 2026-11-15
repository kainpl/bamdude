import { describe, expect, it } from 'vitest';
import type { InventorySpool } from '../../api/client';
import { compareByOrder, parseSpoolSortOrder, serializeSpoolSortOrder } from '../../utils/spoolSort';

const spool = (id: number, over: Partial<InventorySpool>): InventorySpool => ({
  id, material: 'PLA', subtype: null, brand: 'Alpha', color_name: 'Black',
  label_weight: 1000, core_weight: 250, weight_used: 0, ...over,
} as InventorySpool);

describe('the operator\'s spool order', () => {
  it('drops unknown fields, bad directions and repeats, as the server does', () => {
    const levels = parseSpoolSortOrder(' material_asc, net_desc ,bogus_asc,brand_up,material_desc,,');
    expect(levels).toEqual([
      { field: 'material', direction: 'asc' },
      { field: 'net', direction: 'desc' },
    ]);
    expect(serializeSpoolSortOrder(levels)).toBe('material_asc,net_desc');
    expect(parseSpoolSortOrder(undefined)).toEqual([]);
  });

  it('orders level by level, and equal rows newest first', () => {
    const rows = [
      spool(1, { material: 'PETG' }),
      spool(2, { weight_used: 500 }),
      spool(3, { weight_used: 900 }),
      spool(4, { weight_used: 900 }),
    ];
    const sorted = [...rows].sort(compareByOrder(parseSpoolSortOrder('material_desc,net_asc')));
    expect(sorted.map((row) => row.id)).toEqual([4, 3, 2, 1]);
  });

  it('takes a last-dried level, a never-dried spool first when ascending', () => {
    const levels = parseSpoolSortOrder('last_dried_asc');
    expect(levels).toEqual([{ field: 'last_dried', direction: 'asc' }]);
    const rows = [
      spool(1, { last_dried_at: '2026-06-01T00:00:00' }),
      spool(2, { last_dried_at: null }),
      spool(3, { last_dried_at: '2026-01-01T00:00:00' }),
    ];
    expect([...rows].sort(compareByOrder(levels)).map((row) => row.id)).toEqual([2, 3, 1]);
    const newestFirst = compareByOrder(parseSpoolSortOrder('last_dried_desc'));
    expect([...rows].sort(newestFirst).map((row) => row.id)).toEqual([1, 3, 2]);
  });

  it('sorts by the name the template composes', () => {
    const rows = [spool(1, { color_name: 'White' }), spool(2, { color_name: 'Azure', brand: 'Zeta' })];
    const sorted = [...rows].sort(compareByOrder(parseSpoolSortOrder('display_name_asc'), '{color_name} {brand}'));
    expect(sorted.map((row) => row.id)).toEqual([2, 1]);
  });
});
