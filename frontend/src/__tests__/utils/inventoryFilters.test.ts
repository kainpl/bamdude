import { describe, expect, it, beforeEach } from 'vitest';
import {
  DEFAULT_FILTERS,
  FILTERS_KEY,
  loadFilters,
  saveFilters,
  withCurrentId,
  withCurrentValue,
  type StoredFilters,
} from '../../utils/inventoryFilters';

/**
 * Remembering the filament manager's filters.
 *
 * The interesting cases are all about a saved value meeting data that has moved
 * on: a filter we no longer support, a brand whose last spool was deleted, a
 * hand-edited key. Each of those can end as "the list is empty and nothing on
 * screen says why", which is worse than not remembering at all.
 */

const filters = (over: Partial<StoredFilters> = {}): StoredFilters => ({ ...DEFAULT_FILTERS, ...over });

beforeEach(() => localStorage.clear());

describe('a fresh browser', () => {
  it('starts from the defaults', () => {
    expect(loadFilters()).toEqual(DEFAULT_FILTERS);
  });

  it('does not fall over on a key that is not JSON', () => {
    localStorage.setItem(FILTERS_KEY, 'not json{');

    expect(loadFilters()).toEqual(DEFAULT_FILTERS);
  });

  it('does not fall over on JSON that is not an object', () => {
    localStorage.setItem(FILTERS_KEY, '"a string"');

    expect(loadFilters()).toEqual(DEFAULT_FILTERS);
  });
});

describe('a round trip', () => {
  it('brings every filter back', () => {
    const saved = filters({
      archiveFilter: 'archived',
      usageFilter: 'lowstock',
      materialFilter: 'PETG',
      brandFilter: 'eSUN',
      colorFilter: 'Bambu Green',
      categoryFilter: 'prototypes',
      spoolFilter: '17',
      stockFilter: 'stock',
      search: 'matte',
      viewMode: 'cards',
    });
    saveFilters(saved);

    expect(loadFilters()).toEqual(saved);
  });

  it('keeps the view mode, which was forgotten alongside the filters', () => {
    saveFilters(filters({ viewMode: 'forecast' }));

    expect(loadFilters().viewMode).toBe('forecast');
  });
});

describe('clearing', () => {
  it('removes the key rather than storing "no filters"', () => {
    saveFilters(filters({ brandFilter: 'eSUN' }));
    saveFilters(DEFAULT_FILTERS);

    expect(localStorage.getItem(FILTERS_KEY)).toBeNull();
  });

  it('is what a later load sees', () => {
    saveFilters(filters({ search: 'matte', archiveFilter: 'archived' }));
    saveFilters(DEFAULT_FILTERS);

    expect(loadFilters()).toEqual(DEFAULT_FILTERS);
  });
});

describe('a value the app no longer supports', () => {
  it('falls back instead of filtering everything out', () => {
    // ⚠️ The failure this prevents: an unknown value passed through would match
    // no spool at all, on a page whose chips would all read "no filter".
    localStorage.setItem(FILTERS_KEY, JSON.stringify({ usageFilter: 'depleted' }));

    expect(loadFilters().usageFilter).toBe('all');
  });

  it('does the same for the archive tab, stock and view mode', () => {
    localStorage.setItem(
      FILTERS_KEY,
      JSON.stringify({ archiveFilter: 'deleted', stockFilter: 'weird', viewMode: 'kanban' }),
    );
    const got = loadFilters();

    expect([got.archiveFilter, got.stockFilter, got.viewMode]).toEqual(['active', 'all', 'table']);
  });

  it('does not let a non-string through where text is expected', () => {
    localStorage.setItem(FILTERS_KEY, JSON.stringify({ brandFilter: { evil: true }, search: 42 }));
    const got = loadFilters();

    expect([got.brandFilter, got.search]).toEqual(['', '']);
  });

  it('keeps the filters it CAN read when one is broken', () => {
    localStorage.setItem(FILTERS_KEY, JSON.stringify({ usageFilter: 'depleted', brandFilter: 'eSUN' }));

    expect(loadFilters().brandFilter).toBe('eSUN');
  });
});

describe('free text is kept, not corrected', () => {
  it('survives even though no such brand may exist any more', () => {
    // Dropping it would silently edit what the user saved. It is made visible
    // instead — see `withCurrentValue`.
    saveFilters(filters({ brandFilter: 'a brand nobody stocks' }));

    expect(loadFilters().brandFilter).toBe('a brand nobody stocks');
  });
});

describe('withCurrentValue', () => {
  it('leaves the options alone when the value is among them', () => {
    expect(withCurrentValue(['PLA', 'PETG'], 'PETG')).toEqual(['PLA', 'PETG']);
  });

  it('folds in a value the data no longer offers', () => {
    expect(withCurrentValue(['PLA'], 'PETG')).toEqual(['PLA', 'PETG']);
  });

  it('adds nothing when no filter is set', () => {
    expect(withCurrentValue(['PLA'], '')).toEqual(['PLA']);
  });

  it('does not duplicate on repeat calls', () => {
    expect(withCurrentValue(withCurrentValue(['PLA'], 'PETG'), 'PETG')).toEqual(['PLA', 'PETG']);
  });
});

describe('withCurrentId', () => {
  it('folds in a catalog id that is gone', () => {
    expect(withCurrentId([1, 2], '9')).toEqual([1, 2, 9]);
  });

  it('leaves the options alone when the id is present', () => {
    expect(withCurrentId([1, 2], '2')).toEqual([1, 2]);
  });

  it('ignores a value that is not a number', () => {
    expect(withCurrentId([1, 2], 'abc')).toEqual([1, 2]);
  });

  it('adds nothing when no filter is set', () => {
    expect(withCurrentId([1, 2], '')).toEqual([1, 2]);
  });
});
