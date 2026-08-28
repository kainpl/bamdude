/**
 * Per-printer card view preferences.
 *
 * ⚠️ Against jsdom's real localStorage, not a vi.fn() stub — that is this
 * project's setup (see `__tests__/setup.ts`), and it is the right shape here
 * anyway: the store's whole job is to round-trip through a real one.
 *
 * The awkward cases are the point. A malformed entry, a value another tab
 * mangled, or a browser that refuses storage entirely must all read as
 * "nothing hidden": showing the external spool is the safe default, and
 * throwing out of a render is not an option.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

import { isExternalSpoolHidden, setExternalSpoolHidden } from '../../utils/printerCardPrefs';

const KEY = 'printerHiddenExternalSpools';

function stored(): unknown {
  return JSON.parse(localStorage.getItem(KEY) ?? 'null');
}

describe('printerCardPrefs — external spool visibility', () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => vi.restoreAllMocks());

  it('defaults to visible for a printer that was never toggled', () => {
    expect(isExternalSpoolHidden(1)).toBe(false);
  });

  it('round-trips the hidden flag', () => {
    setExternalSpoolHidden(1, true);

    expect(isExternalSpoolHidden(1)).toBe(true);
    expect(stored()).toEqual({ '1': true });
  });

  it('keeps each printer independent', () => {
    // ⚠️ The reason this is keyed by printer rather than a single global flag:
    // the toggle lives on the card, so hiding one printer's spool must not
    // silently rearrange every other card in a fleet.
    setExternalSpoolHidden(1, true);
    setExternalSpoolHidden(2, true);
    setExternalSpoolHidden(1, false);

    expect(isExternalSpoolHidden(1)).toBe(false);
    expect(isExternalSpoolHidden(2)).toBe(true);
  });

  it('drops the key when shown again rather than storing false', () => {
    // Otherwise the object accumulates an entry for every printer anybody ever
    // toggled twice.
    setExternalSpoolHidden(3, true);
    setExternalSpoolHidden(3, false);

    expect(stored()).toEqual({});
  });

  it('re-reads before writing so two toggles cannot clobber each other', () => {
    setExternalSpoolHidden(1, true);
    // Simulates another card (or another tab) having written in between.
    localStorage.setItem(KEY, JSON.stringify({ '1': true, '9': true }));

    setExternalSpoolHidden(2, true);

    expect(stored()).toEqual({ '1': true, '9': true, '2': true });
  });

  describe('a store it cannot trust reads as nothing hidden', () => {
    it.each([
      ['malformed JSON', '{not json'],
      ['an array', '[1,2,3]'],
      ['a bare string', '"hidden"'],
      ['null', 'null'],
    ])('%s', (_label, raw) => {
      localStorage.setItem(KEY, raw);

      expect(isExternalSpoolHidden(1)).toBe(false);
    });

    it('and the next write repairs it', () => {
      localStorage.setItem(KEY, '{not json');

      setExternalSpoolHidden(1, true);

      expect(stored()).toEqual({ '1': true });
    });
  });

  it('survives localStorage being unavailable', () => {
    // Private mode, blocked cookies. The toggle still applies for this
    // session; it just does not survive a reload — and must not throw out of
    // the render that read it.
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('access denied');
    });
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('access denied');
    });

    expect(() => setExternalSpoolHidden(1, true)).not.toThrow();
    expect(isExternalSpoolHidden(1)).toBe(false);
  });
});
