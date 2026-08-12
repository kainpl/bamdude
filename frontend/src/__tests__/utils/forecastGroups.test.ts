import { describe, expect, it } from 'vitest';
import type { InventorySpool, SpoolUsageRecord } from '../../api/client';
import {
  RECENT_SKU_WINDOW_DAYS,
  buildSkuGroups,
  collectGroupHistory,
  groupTotals,
  lastTouchedMs,
  type UsageBySpoolId,
} from '../../utils/forecastGroups';

/**
 * The forecast and archived spools.
 *
 * The workflow this exists for: a spool runs out, you swap it, and you archive
 * the empty one so it stops cluttering the manager. Everything below is about
 * that archived spool still counting as *what you burned* while never counting
 * as *what you have*.
 */

const DAY = 86400000;
const NOW = new Date('2026-08-12T12:00:00Z').getTime();
const daysAgo = (n: number) => new Date(NOW - n * DAY).toISOString();

const key = (m: string, s: string | null, b: string | null, c: string | null) =>
  `${m}|${s ?? ''}|${b ?? ''}|${c ?? ''}`;

const spool = (over: Partial<InventorySpool> & { id: number }): InventorySpool =>
  ({
    material: 'PLA',
    subtype: 'Basic',
    brand: 'Bambu',
    color_name: 'Black',
    label_weight: 1000,
    weight_used: 0,
    weight_used_baseline: 0,
    archived_at: null,
    created_at: daysAgo(200),
    ...over,
  }) as InventorySpool;

const usage = (spoolId: number, records: { day: number; g: number }[]): UsageBySpoolId =>
  new Map([[spoolId, records.map(({ day, g }) => ({ spool_id: spoolId, weight_used: g, created_at: daysAgo(day) }) as SpoolUsageRecord)]]);

const NO_USAGE: UsageBySpoolId = new Map();

describe('an archived spool belongs to both lists, differently', () => {
  const spools = [
    spool({ id: 1, archived_at: daysAgo(2), weight_used: 1000 }),
    spool({ id: 2, weight_used: 100 }),
  ];

  it('is left out of what you still have', () => {
    const [g] = buildSkuGroups(spools, NO_USAGE, key, NOW);

    expect(g.spools.map((s) => s.id)).toEqual([2]);
  });

  it('is kept in what you burned', () => {
    const [g] = buildSkuGroups(spools, NO_USAGE, key, NOW);

    expect(g.allSpools.map((s) => s.id)).toEqual([1, 2]);
  });

  it('groups by SKU, not by whether it is archived', () => {
    const groups = buildSkuGroups(spools, NO_USAGE, key, NOW);

    expect(groups).toHaveLength(1);
  });
});

describe('the totals split along that line', () => {
  const group = buildSkuGroups(
    [
      spool({ id: 1, archived_at: daysAgo(2), weight_used: 1000 }), // emptied and retired
      spool({ id: 2, weight_used: 250 }), // the replacement, in use
    ],
    NO_USAGE,
    key,
    NOW,
  )[0];

  it('counts only live spools as stock', () => {
    // ⚠️ The archived spool must not extend "days remaining" — it is gone.
    expect(groupTotals(group).totalRemainingG).toBe(750);
  });

  it('counts only live spools when saying how many you have', () => {
    expect(groupTotals(group).totalSpools).toBe(1);
  });

  it('counts the archived spool as consumed', () => {
    // The whole bug: archiving used to drop these 1000 g from the forecast.
    expect(groupTotals(group).totalUsedG).toBe(1250);
  });

  it('still honours the reset baseline', () => {
    const withReset = buildSkuGroups(
      [spool({ id: 1, archived_at: daysAgo(2), weight_used: 1000, weight_used_baseline: 600 })],
      usage(1, [{ day: 2, g: 1000 }]),
      key,
      NOW,
    )[0];

    expect(groupTotals(withReset).totalUsedG).toBe(400);
  });
});

describe('the rate is computed from history the archive keeps', () => {
  it('includes an archived spool records', () => {
    const group = buildSkuGroups(
      [spool({ id: 1, archived_at: daysAgo(3), weight_used: 1000 })],
      usage(1, [{ day: 5, g: 400 }, { day: 4, g: 600 }]),
      key,
      NOW,
    )[0];

    expect(collectGroupHistory(group, usage(1, [{ day: 5, g: 400 }, { day: 4, g: 600 }]))).toHaveLength(2);
  });

  it('still drops a reset spool, archived or not', () => {
    // ⚠️ Pre-reset events have no anchor timestamp and would inflate the rate.
    const group = buildSkuGroups(
      [spool({ id: 1, archived_at: daysAgo(3), weight_used: 1000, weight_used_baseline: 500 })],
      usage(1, [{ day: 5, g: 400 }]),
      key,
      NOW,
    )[0];

    expect(collectGroupHistory(group, usage(1, [{ day: 5, g: 400 }]))).toEqual([]);
  });
});

describe('a SKU with nothing left but archived spools', () => {
  const archivedOnly = (archivedDaysAgo: number) => [
    spool({ id: 1, archived_at: daysAgo(archivedDaysAgo), weight_used: 1000 }),
  ];

  it('stays while it is still a live reorder question', () => {
    // Ran out days ago, replacement not bought yet — this is exactly the row
    // you need, and it used to disappear.
    expect(buildSkuGroups(archivedOnly(4), NO_USAGE, key, NOW)).toHaveLength(1);
  });

  it('goes once nothing has happened for the whole window', () => {
    // ⚠️ Otherwise every colour ever abandoned keeps a permanent red alert: with
    // zero stock and a stale-but-full rate, days-remaining reads 0 forever.
    expect(buildSkuGroups(archivedOnly(RECENT_SKU_WINDOW_DAYS + 5), NO_USAGE, key, NOW)).toHaveLength(0);
  });

  it('counts recent printing even when the spool was archived long ago', () => {
    const stale = archivedOnly(RECENT_SKU_WINDOW_DAYS + 30);

    expect(buildSkuGroups(stale, usage(1, [{ day: 3, g: 200 }]), key, NOW)).toHaveLength(1);
  });

  it('counts a recent archiving even with no usage records at all', () => {
    // A spool consumed before usage history existed, or whose history was
    // cleared. Retiring it today is still evidence the SKU is in play.
    expect(buildSkuGroups(archivedOnly(3), NO_USAGE, key, NOW)).toHaveLength(1);
  });
});

describe('a SKU you still hold', () => {
  it('never expires, however old its last print', () => {
    const spools = [spool({ id: 1, created_at: daysAgo(900), weight_used: 0 })];

    expect(buildSkuGroups(spools, NO_USAGE, key, NOW)).toHaveLength(1);
  });

  it('survives even when its archived siblings are ancient', () => {
    const spools = [
      spool({ id: 1, archived_at: daysAgo(800), weight_used: 1000 }),
      spool({ id: 2, weight_used: 0 }),
    ];
    const [g] = buildSkuGroups(spools, NO_USAGE, key, NOW);

    // ⚠️ And the ancient spool is still consumption evidence — kept, not pruned.
    expect(g.allSpools).toHaveLength(2);
  });
});

describe('lastTouchedMs', () => {
  it('takes the newest usage record', () => {
    const s = spool({ id: 1 });

    expect(lastTouchedMs(s, usage(1, [{ day: 40, g: 10 }, { day: 5, g: 10 }]))).toBe(NOW - 5 * DAY);
  });

  it('falls back to the archive date when there are no records', () => {
    expect(lastTouchedMs(spool({ id: 1, archived_at: daysAgo(7) }), NO_USAGE)).toBe(NOW - 7 * DAY);
  });

  it('prefers whichever is newer', () => {
    const s = spool({ id: 1, archived_at: daysAgo(2) });

    expect(lastTouchedMs(s, usage(1, [{ day: 30, g: 10 }]))).toBe(NOW - 2 * DAY);
  });

  it('is zero for a live spool that has never been used', () => {
    expect(lastTouchedMs(spool({ id: 1 }), NO_USAGE)).toBe(0);
  });
});
