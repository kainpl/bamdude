/**
 * The Printers page remembers its status, location and tag filters.
 *
 * Pick a location, navigate away, come back, and every printer was showing
 * again. Both filters were plain state, and the only preferences on that page
 * that were not remembered — sort order, card size, view mode, collapsed
 * sections and hide-disconnected all persist.
 *
 * ⚠️ A saved filter needs a way out. The location dropdown renders only while
 * at least one location exists, so a saved location that was later renamed or
 * deleted would match nothing and take its own dropdown off screen with it:
 * an empty page and no control to undo it. A value the dropdown does not offer
 * resets to "all", and the same for a status.
 *
 * ⚠️ That check waits for the locations query. The list is undefined while it
 * is in flight, and acting on the empty list that produces would throw the
 * saved filter away on every single page load.
 *
 * Search stays unpersisted: a box that silently refills itself on return is a
 * surprise rather than a convenience.
 */

import { describe, it, expect } from 'vitest';

import source from '../../pages/PrintersPage.tsx?raw';

describe('the filters persist', () => {
  it.each([
    ['status', 'printerStatusFilter'],
    ['location', 'printerLocationFilter'],
  ])('reads the saved %s filter on mount', (_label, key) => {
    expect(source).toContain(`localStorage.getItem('${key}')`);
  });

  it.each([
    ['status', 'printerStatusFilter'],
    ['location', 'printerLocationFilter'],
  ])('writes the %s filter when it changes', (_label, key) => {
    expect(source).toContain(`localStorage.setItem('${key}'`);
  });

  it('leaves the search box alone', () => {
    // Deliberate: a refilled dropdown reads as a memory, a refilled text box
    // reads as a bug.
    expect(source).not.toContain("localStorage.getItem('printerSearch')");
  });
});

describe('a saved filter cannot strand the page', () => {
  it('validates a saved status against the very list the dropdown offers', () => {
    // One list, so the two cannot drift: validating against a hand-copied set
    // is how a status the dropdown dropped survives in storage forever.
    expect(source).toContain('const STATUS_FILTER_OPTIONS = [');
    expect(source).toContain('isKnownStatusFilter(saved)');
    expect(source).toContain('STATUS_FILTER_OPTIONS.map((option) => ({ value: option.value');
  });

  it('validates a saved sort against the very list the dropdown offers', () => {
    // Same trap as the status filter, and the same cure: the sort was read out
    // of storage with a bare cast, so a value no option carries would leave the
    // grid in whatever order the sort switch falls through to with nothing
    // picked in the dropdown — and no way to see why.
    expect(source).toContain('const SORT_OPTIONS');
    expect(source).toContain('isKnownSortOption(saved)');
    expect(source).toContain('SORT_OPTIONS.map((option) => ({ value: option.value');
    expect(source).not.toContain("localStorage.getItem('printerSortBy') as SortOption");
  });

  it('resets a location that no longer exists', () => {
    const guard = source.slice(source.indexOf('A saved location can outlive'));
    expect(guard).toContain("setLocationFilter('all')");
    expect(guard).toContain("localStorage.setItem('printerLocationFilter', 'all')");
  });

  it('persists the tag filter under its own key and validates it against the tag list', () => {
    // Same trap as the location filter, and worse: a deleted tag leaves no
    // checkbox to untick, so the ids that no longer exist are dropped on read.
    expect(source).toContain("localStorage.getItem('printerTagFilter')");
    expect(source).toContain("localStorage.setItem('printerTagFilter'");
    expect(source).toMatch(/known\.has\(id\)/);
  });

  it('waits for the query before deciding a location is stale', () => {
    // ⚠️ Without this the saved filter is discarded on every page load, while
    // the list is still undefined.
    const guard = source.slice(source.indexOf('A saved location can outlive'));
    expect(guard).toContain('if (!locationRows || locationFilter === \'all\') return;');
  });
});
