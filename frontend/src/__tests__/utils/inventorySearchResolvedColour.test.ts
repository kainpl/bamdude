/**
 * The colour name on screen is a name you can search for (upstream e4a9ef45,
 * #3090).
 *
 * The list shows a name resolved from the swatch's hex whenever a spool carries
 * none of its own — a Bambu tag often carries no name or an internal code, and
 * Spoolman has no such field at all. Searching only the stored column meant
 * typing what was plainly on screen found nothing.
 */
import { beforeEach, describe, expect, it } from 'vitest';

import type { InventorySpool } from '../../api/client';
import { __resetColorCatalogForTests, setColorCatalog } from '../../utils/colors';
import { filterSpoolsByQuery } from '../../utils/inventorySearch';

function makeSpool(overrides: Partial<InventorySpool>): InventorySpool {
  return {
    id: 1,
    material: 'PLA',
    brand: null,
    subtype: null,
    color_name: null,
    rgba: null,
    note: null,
    slicer_filament_name: null,
    storage_location: null,
    ...overrides,
  } as InventorySpool;
}

describe('filterSpoolsByQuery — the resolved colour name', () => {
  beforeEach(() => {
    __resetColorCatalogForTests();
    setColorCatalog({ d02727: 'Candy Red' });
  });

  it('matches a catalogue name the spool does not store', () => {
    const spools = [
      makeSpool({ id: 1, rgba: 'D02727FF' }),
      makeSpool({ id: 2, rgba: '123456FF' }),
    ];
    expect(filterSpoolsByQuery(spools, 'candy').map((s) => s.id)).toEqual([1]);
  });

  it('matches a catalogue name over a subtype Spoolman put in its place', () => {
    const spools = [makeSpool({ id: 1, color_name: 'Silk+', color_name_is_synthesized: true, rgba: 'D02727FF' })];
    expect(filterSpoolsByQuery(spools, 'candy red').map((s) => s.id)).toEqual([1]);
  });

  it('still matches the stored value, for anyone who knows the tag codes', () => {
    const spools = [makeSpool({ id: 1, color_name: 'A06-D0', rgba: 'D02727FF' })];
    expect(filterSpoolsByQuery(spools, 'a06-d0').map((s) => s.id)).toEqual([1]);
  });
});
