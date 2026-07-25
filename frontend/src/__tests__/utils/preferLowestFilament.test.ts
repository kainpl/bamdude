/**
 * "Prefer lowest remaining filament" in the auto-matcher.
 *
 * Back-audit finding, row D1 of 0.2.4.7->0.2.4.8. The setting was honoured only
 * by AutoQueue. On Print → pick printer → Add to queue the dialog pins a
 * mapping, and the dispatcher deliberately will NOT re-derive a mapping that is
 * already resolved (`_ensure_ams_mapping` returns early so a manual override is
 * never clobbered) — so a mapping pinned without the setting applied meant the
 * setting was silently ignored on that whole path.
 *
 * The rule mirrors the backend (`auto_queue_ams.py`): sort the candidate pool
 * ascending by `remain`, unknown (-1) last, and let each match tier pick the
 * first hit. That makes it a tiebreaker WITHIN a tier — it must never promote a
 * worse-matching spool over a better one.
 */

import { describe, it, expect } from 'vitest';
import { autoMatchFilament, remainSortKey, sortByRemainAscending } from '../../utils/amsHelpers';

const f = (globalTrayId: number, type: string, color: string, remain: number, extruderId = 0) => ({
  globalTrayId,
  type,
  color,
  remain,
  extruderId,
});

describe('remainSortKey', () => {
  it('orders by remaining percentage', () => {
    expect(remainSortKey({ remain: 10 })).toBeLessThan(remainSortKey({ remain: 90 }));
  });

  it('pushes unmeasurable spools past the 0-100 range', () => {
    // -1 means no RFID / calibration off. Such a spool is only chosen when
    // nothing measurable qualifies — same 101 sentinel the backend uses.
    expect(remainSortKey({ remain: -1 })).toBe(101);
    expect(remainSortKey({})).toBe(101);
    expect(remainSortKey({ remain: 100 })).toBeLessThan(remainSortKey({ remain: -1 }));
  });
});

describe('sortByRemainAscending', () => {
  it('does not mutate its input', () => {
    const input = [f(1, 'PLA', '#FF0000', 80), f(2, 'PLA', '#FF0000', 20)];
    const sorted = sortByRemainAscending(input);
    expect(input[0].globalTrayId).toBe(1);
    expect(sorted[0].globalTrayId).toBe(2);
  });
});

describe('autoMatchFilament with preferLowest', () => {
  const req = { type: 'PLA', color: '#FF0000', nozzle_id: 0 };

  it('picks the fullest-first order when the setting is off', () => {
    // Off: first match in list order wins, exactly as before.
    const loaded = [f(1, 'PLA', '#FF0000', 80), f(2, 'PLA', '#FF0000', 20)];
    expect(autoMatchFilament(req, loaded, new Set())?.globalTrayId).toBe(1);
  });

  it('drains the emptiest compatible spool when the setting is on', () => {
    const loaded = [f(1, 'PLA', '#FF0000', 80), f(2, 'PLA', '#FF0000', 20)];
    expect(autoMatchFilament(req, loaded, new Set(), undefined, true)?.globalTrayId).toBe(2);
  });

  it('never promotes a worse match tier', () => {
    // Tray 2 is nearly empty but the WRONG colour; tray 1 is an exact colour
    // match with plenty left. Exact colour must still win — prefer-lowest is a
    // tiebreaker inside a tier, not across tiers.
    const loaded = [f(1, 'PLA', '#FF0000', 90), f(2, 'PLA', '#0000FF', 3)];
    expect(autoMatchFilament(req, loaded, new Set(), undefined, true)?.globalTrayId).toBe(1);
  });

  it('breaks ties within the type-only tier too', () => {
    // No colour match anywhere — both are type-only, so the emptier one wins.
    const loaded = [f(1, 'PLA', '#00FF00', 70), f(2, 'PLA', '#0000FF', 15)];
    expect(autoMatchFilament(req, loaded, new Set(), undefined, true)?.globalTrayId).toBe(2);
  });

  it('prefers a measurable spool over an unmeasurable one', () => {
    const loaded = [f(1, 'PLA', '#FF0000', -1), f(2, 'PLA', '#FF0000', 60)];
    expect(autoMatchFilament(req, loaded, new Set(), undefined, true)?.globalTrayId).toBe(2);
  });

  it('still skips trays already used by another slot', () => {
    const loaded = [f(1, 'PLA', '#FF0000', 80), f(2, 'PLA', '#FF0000', 20)];
    expect(autoMatchFilament(req, loaded, new Set([2]), undefined, true)?.globalTrayId).toBe(1);
  });

  it('still honours the nozzle hard filter', () => {
    // The emptiest spool is on the wrong extruder — cross-nozzle assignment
    // fails the print, so it must not be chosen however empty it is.
    const loaded = [f(1, 'PLA', '#FF0000', 90, 0), f(2, 'PLA', '#FF0000', 5, 1)];
    expect(autoMatchFilament(req, loaded, new Set(), undefined, true)?.globalTrayId).toBe(1);
  });
});
