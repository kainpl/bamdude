/**
 * The shared inventory-mutation invalidation helper (final review, F3).
 *
 * Eleven mutation surfaces — create / bulk-create / update / delete / archive
 * / restore / bulk edit / reset usage (one and all) / bulk archive-restore-
 * delete / CSV import — route their post-success refresh through
 * `invalidateSpoolAndLocationQueries`. Every one of them moves what
 * `compute_forecast` serves (totals, the rate tier, the alerts, the chart,
 * the logistics rows), and every one is reachable from the header and stats
 * bar WHILE the Forecast tab is open. So the helper is the single place the
 * forecast feeds have to be named — and this file is what keeps them named.
 */

import { describe, it, expect } from 'vitest';
import { QueryClient } from '@tanstack/react-query';
import {
  invalidateForecastQueries,
  invalidateSpoolAndLocationQueries,
} from '../../utils/inventoryQueries';

const FORECAST_KEYS = [
  ['inventory-forecast'],
  ['inventory-forecast-chart'],
  ['inventory-forecast-logistics'],
];

function primed() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  // Seed a cache entry per key so `getQueryState` has something to report.
  qc.setQueryData(['inventory-spools', 'page', { page: 1 }], { items: [] });
  qc.setQueryData(['inventory-spools', 'stats'], { total_spools: 0 });
  qc.setQueryData(['inventory-locations'], []);
  for (const k of FORECAST_KEYS) qc.setQueryData(k, []);
  return qc;
}

const invalidated = (qc: QueryClient, key: unknown[]) =>
  qc.getQueryState(key)?.isInvalidated ?? false;

describe('invalidateSpoolAndLocationQueries', () => {
  it('marks the spool list, the locations AND all three forecast feeds stale', async () => {
    const qc = primed();
    await invalidateSpoolAndLocationQueries(qc, ['inventory-spools']);

    expect(invalidated(qc, ['inventory-spools', 'page', { page: 1 }])).toBe(true);
    expect(invalidated(qc, ['inventory-spools', 'stats'])).toBe(true);
    expect(invalidated(qc, ['inventory-locations'])).toBe(true);
    // The finding: a spool mutation used to leave the forecast asserting
    // pre-mutation numbers for the whole sitting — add 4×1kg of the SKU the
    // red banner names and the banner kept the pre-purchase count.
    for (const k of FORECAST_KEYS) {
      expect(invalidated(qc, k), `forecast key ${JSON.stringify(k)}`).toBe(true);
    }
  });

  it('names all three forecast keys — the two suffixed ones are NOT prefixed by the first', async () => {
    // TanStack matches key arrays element-wise: 'inventory-forecast-chart' is
    // a different first element, not a child of 'inventory-forecast'. Drop one
    // from the list and the chart silently keeps the old series.
    const qc = primed();
    await invalidateForecastQueries(qc);
    for (const k of FORECAST_KEYS) {
      expect(invalidated(qc, k), `forecast key ${JSON.stringify(k)}`).toBe(true);
    }
    // …and it stays scoped: the spool list is somebody else's business.
    expect(invalidated(qc, ['inventory-spools', 'stats'])).toBe(false);
  });

  it('carries the Spoolman-mode spool key through unchanged', async () => {
    const qc = primed();
    qc.setQueryData(['spoolman-inventory-spools'], []);
    await invalidateSpoolAndLocationQueries(qc, ['spoolman-inventory-spools']);
    expect(invalidated(qc, ['spoolman-inventory-spools'])).toBe(true);
    expect(invalidated(qc, ['inventory-spools', 'stats'])).toBe(false);
  });
});
