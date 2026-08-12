/**
 * The Settings page must not write its stale copy over a server-side change (#2716).
 *
 * The whole bug was a two-way diff pretending to answer a three-way question.
 * With only "local" and "server" there is no way to tell an edit from a value
 * that moved underneath you, so the page reverted the latter. The baseline is
 * the missing third input; these cases are the truth table it makes possible.
 */

import { describe, it, expect } from 'vitest';

import { adoptUntouchedServerChanges } from '../../utils/settingsReconcile';

const baseline = { currency: 'USD', save_thumbnails: true, log_retention_days: 7 };

describe('adoptUntouchedServerChanges', () => {
  it('adopts a server change to a field the user never touched', () => {
    // Another admin switched the currency while this page sat open.
    const local = { ...baseline };
    const server = { ...baseline, currency: 'EUR' };
    expect(adoptUntouchedServerChanges(baseline, local, server)).toEqual({ currency: 'EUR' });
  });

  it('does NOT adopt over a field the user is editing', () => {
    // The user's value stays; the debounced save writes it over the server's,
    // so the newer write wins rather than the page always winning.
    const local = { ...baseline, currency: 'GBP' };
    const server = { ...baseline, currency: 'EUR' };
    expect(adoptUntouchedServerChanges(baseline, local, server)).toEqual({});
  });

  it('adopts one field while leaving an edited one alone', () => {
    const local = { ...baseline, currency: 'GBP' };
    const server = { ...baseline, currency: 'EUR', log_retention_days: 30 };
    expect(adoptUntouchedServerChanges(baseline, local, server)).toEqual({ log_retention_days: 30 });
  });

  it('adopts nothing when the server has not moved', () => {
    // The common case, and the one that must stay cheap: a refetch returning
    // the same values is not a change.
    const local = { ...baseline, currency: 'GBP' };
    expect(adoptUntouchedServerChanges(baseline, local, { ...baseline })).toEqual({});
  });

  it('treats a local edit back to the baseline value as untouched', () => {
    // The user typed something and undid it. There is nothing to preserve, so
    // the server's newer value wins — which is the correct reading of "the user
    // has no unsaved opinion about this field".
    const local = { ...baseline };
    const server = { ...baseline, save_thumbnails: false };
    expect(adoptUntouchedServerChanges(baseline, local, server)).toEqual({ save_thumbnails: false });
  });

  it('handles falsy values without treating them as absent', () => {
    // `false` and `0` are real settings values; an `if (value)` style check
    // here would silently refuse to adopt them.
    const base = { flag: true, count: 5 };
    const server = { flag: false, count: 0 };
    expect(adoptUntouchedServerChanges(base, { ...base }, server)).toEqual({ flag: false, count: 0 });
  });

  it('ignores keys the server does not send', () => {
    // Iteration is over the server object, so a local-only field (e.g. the
    // browser-detected external_url) is never clobbered.
    const local = { ...baseline, external_url: 'http://localhost:3000' };
    const server = { ...baseline, currency: 'EUR' };
    const adopted = adoptUntouchedServerChanges(baseline, local as typeof baseline, server);
    expect(adopted).not.toHaveProperty('external_url');
  });
});
