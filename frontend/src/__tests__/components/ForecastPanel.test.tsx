/**
 * ForecastPanel as a renderer (task 4, 2026-08-29 forecast-server-side).
 *
 * The panel's math died server-side (tasks 1-3); these tests pin the panel
 * at its API boundary — the PARAMS it sends and the served fields it renders
 * verbatim — replacing the client-math assertions that moved to pytest with
 * the engine. The load-bearing pins:
 *
 * - the table feeds from GET /inventory/forecast with sort/filter/page as
 *   request params; the old heavy feeds (all=true spools, /inventory/usage)
 *   never fire;
 * - a persisted localStorage sort is SANITIZED to the server key set before
 *   the first request — an unknown sort_by is a real 400 server-side (the
 *   Plan-B lesson), so garbage must never reach the wire;
 * - a filter/sort change resets the page to 1 in the same render
 *   (the FileManagerPage pageResetSignature pattern);
 * - the expanded SKU row fetches its spools LAZILY and narrows to the row's
 *   served spool_ids (the endpoint cannot express subtype or NULL fields —
 *   the ids are the engine's own group membership);
 * - the chart renders served series and refetches on the timeframe toggle;
 * - the CSV button hits the server export endpoint (house binary idiom).
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { screen, waitFor, fireEvent } from '@testing-library/react';
import { render } from '../utils';
import { ForecastPanel } from '../../components/ForecastPanel';
import { http, HttpResponse, delay } from 'msw';
import { server } from '../mocks/server';
import type { SkuForecastRow, ForecastChartSeriesEntry } from '../../api/client';

const FORECAST_SORT_KEY = 'bamdude-forecast-sort';

function row(over: Partial<SkuForecastRow> = {}): SkuForecastRow {
  return {
    material: 'PLA',
    subtype: null,
    brand: 'eSun',
    color_name: 'Blue',
    rgba: '0000FFFF',
    total_spools: 2,
    total_remaining_g: 1500,
    total_label_g: 2000,
    avg_spool_label_g: 1000,
    total_used_g: 500,
    rate_g_day: 10,
    rate_tier: 'history',
    std_dev: 2,
    eff_lead_time_days: 5,
    safety_stock_g: 50,
    reorder_point_g: 100,
    days_remaining: 150,
    projected_empty_date: '2026-09-15',
    days_until_rop: 140,
    reorder_trigger_date: '2026-09-10',
    stock_break_alert: false,
    reorder_alert: false,
    alerts_snoozed: false,
    spool_ids: [1, 2],
    ...over,
  };
}

// The nested-table fixtures: only the fields the expanded row renders.
const detailSpool = (id: number, over: Record<string, unknown> = {}) => ({
  id,
  material: 'PLA',
  subtype: null,
  brand: 'eSun',
  color_name: 'Blue',
  rgba: '0000FFFF',
  label_weight: 1000,
  weight_used: 250,
  weight_used_baseline: 0,
  archived_at: null,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  k_profile_count: 0,
  k_profiles: null,
  ...over,
});

let forecastRequests: URL[] = [];
let chartRequests: URL[] = [];
let logisticsRequests: URL[] = [];
let spoolListRequests: URL[] = [];
let usageRequests = 0;
let csvRequests = 0;

function setupHandlers({
  rows = [row()],
  alertCount = 0,
  total,
  chart = [] as ForecastChartSeriesEntry[],
  shoppingList = [] as unknown[],
  detailSpools = [detailSpool(1), detailSpool(2)],
}: {
  rows?: SkuForecastRow[];
  alertCount?: number;
  total?: number;
  chart?: ForecastChartSeriesEntry[];
  shoppingList?: unknown[];
  detailSpools?: ReturnType<typeof detailSpool>[];
} = {}) {
  forecastRequests = [];
  chartRequests = [];
  logisticsRequests = [];
  spoolListRequests = [];
  usageRequests = 0;
  csvRequests = 0;
  server.use(
    http.get('/api/v1/inventory/forecast', ({ request }) => {
      const url = new URL(request.url);
      forecastRequests.push(url);
      const grandTotal = total ?? rows.length;
      const all = url.searchParams.get('all') === 'true';
      const perPage = all ? grandTotal || 1 : Number(url.searchParams.get('per_page') ?? 50);
      return HttpResponse.json({
        items: rows,
        meta: {
          total: grandTotal,
          current_page: all ? 1 : Number(url.searchParams.get('page') ?? 1),
          per_page: perPage,
          last_page: all ? 1 : Math.max(1, Math.ceil(grandTotal / perPage)),
        },
        alert_count: alertCount,
        global_lead_time_days: 3,
      });
    }),
    http.get('/api/v1/inventory/forecast/chart', ({ request }) => {
      chartRequests.push(new URL(request.url));
      return HttpResponse.json({ series: chart });
    }),
    http.get('/api/v1/inventory/forecast/logistics', ({ request }) => {
      logisticsRequests.push(new URL(request.url));
      return HttpResponse.json([]);
    }),
    http.get('/api/v1/inventory/shopping-list/export.csv', () => {
      csvRequests += 1;
      return new HttpResponse('"Qty"\r\n', {
        headers: {
          'Content-Type': 'text/csv',
          'Content-Disposition': 'attachment; filename="shopping-list.csv"',
        },
      });
    }),
    http.get('/api/v1/inventory/sku-settings', () => HttpResponse.json([])),
    http.get('/api/v1/inventory/shopping-list', () => HttpResponse.json(shoppingList)),
    http.get('/api/v1/inventory/spools/facets', () =>
      HttpResponse.json({
        materials: ['PLA', 'PETG'],
        brands: ['eSun', 'SUNLU'],
        categories: [],
        catalog_ids: [],
        colors: [],
      })
    ),
    http.get('/api/v1/inventory/spools', ({ request }) => {
      const url = new URL(request.url);
      spoolListRequests.push(url);
      return HttpResponse.json({
        items: detailSpools,
        meta: { total: detailSpools.length, current_page: 1, per_page: detailSpools.length || 1, last_page: 1 },
      });
    }),
    http.get('/api/v1/inventory/usage', () => {
      usageRequests += 1;
      return HttpResponse.json([]);
    }),
    http.get('/api/v1/settings/', () => HttpResponse.json({ language: 'en' }))
  );
}

describe('ForecastPanel — a renderer of server-computed rows', () => {
  beforeEach(() => {
    localStorage.clear();
    localStorage.setItem('auth_token', 'test-admin-token');
    setupHandlers();
  });

  it('feeds the table from GET /inventory/forecast with the default params — the old heavy feeds stay silent', async () => {
    render(<ForecastPanel />);
    await waitFor(() => expect(forecastRequests.length).toBeGreaterThan(0));
    const u = forecastRequests[0];
    expect(u.searchParams.get('page')).toBe('1');
    expect(u.searchParams.get('per_page')).toBe('50');
    expect(u.searchParams.get('sort_by')).toBe('material_asc');
    expect(u.searchParams.has('material')).toBe(false);
    expect(u.searchParams.has('brand')).toBe(false);
    expect(u.searchParams.has('alerts_only')).toBe(false);

    // The row renders from the served fields, no client math.
    await waitFor(() => expect(screen.getByText('eSun PLA Blue')).toBeInTheDocument());
    expect(screen.getByText('150d')).toBeInTheDocument(); // served days_remaining, verbatim

    // The four dead queries: no all=true spool feed, no usage-history feed.
    expect(usageRequests).toBe(0);
    expect(spoolListRequests.length).toBe(0);
  });

  it('sanitizes a garbage persisted sort key to the default before the first request (the 400 lesson)', async () => {
    localStorage.setItem(FORECAST_SORT_KEY, JSON.stringify({ key: 'velocity', dir: 'sideways' }));
    render(<ForecastPanel />);
    await waitFor(() => expect(forecastRequests.length).toBeGreaterThan(0));
    expect(forecastRequests[0].searchParams.get('sort_by')).toBe('material_asc');
  });

  it('sanitizes an unparsable persisted sort value to the default', async () => {
    localStorage.setItem(FORECAST_SORT_KEY, 'not-json{{');
    render(<ForecastPanel />);
    await waitFor(() => expect(forecastRequests.length).toBeGreaterThan(0));
    expect(forecastRequests[0].searchParams.get('sort_by')).toBe('material_asc');
  });

  it('a valid persisted sort passes through as the composed server key', async () => {
    localStorage.setItem(FORECAST_SORT_KEY, JSON.stringify({ key: 'stock', dir: 'desc' }));
    render(<ForecastPanel />);
    await waitFor(() => expect(forecastRequests.length).toBeGreaterThan(0));
    expect(forecastRequests[0].searchParams.get('sort_by')).toBe('stock_desc');
  });

  it('sanitizes a garbage sort DIRECTION too — a VALID key with a junk dir', async () => {
    // The three cases above all bail on the KEY check and never reach the
    // direction half. With a valid key the dir is the only thing left that
    // can poison the composed param: a bare `dir as SortDir` would send
    // sort_by=stock_sideways — a real 400 server-side, i.e. a blank tab.
    localStorage.setItem(FORECAST_SORT_KEY, JSON.stringify({ key: 'stock', dir: 'sideways' }));
    render(<ForecastPanel />);
    await waitFor(() => expect(forecastRequests.length).toBeGreaterThan(0));
    expect(forecastRequests[0].searchParams.get('sort_by')).toBe('stock_asc');
  });

  it('a header click sends the server sort param; a filter change narrows AND resets to page 1', async () => {
    setupHandlers({ total: 120 }); // 3 pages at 50/page
    render(<ForecastPanel />);
    await waitFor(() => expect(screen.getByText('eSun PLA Blue')).toBeInTheDocument());

    // Sort: 'Spools' starts descending (a quantity column).
    fireEvent.click(screen.getByText('Spools'));
    await waitFor(() =>
      expect(forecastRequests.some((u) => u.searchParams.get('sort_by') === 'spools_desc')).toBe(true)
    );

    // Walk to page 2…
    fireEvent.click(screen.getByLabelText('Next page'));
    await waitFor(() => expect(forecastRequests.some((u) => u.searchParams.get('page') === '2')).toBe(true));

    // …then filter — the SAME render must reset the page: the request that
    // carries the filter must never carry the stale page.
    fireEvent.change(screen.getByDisplayValue('Material'), { target: { value: 'PLA' } });
    await waitFor(() =>
      expect(forecastRequests.some((u) => u.searchParams.get('material') === 'PLA')).toBe(true)
    );
    for (const u of forecastRequests.filter((r) => r.searchParams.get('material') === 'PLA')) {
      expect(u.searchParams.get('page')).toBe('1');
    }
  });

  it('the expanded SKU row fetches its spools lazily and narrows to the served spool_ids', async () => {
    // #99 matches every server-expressible filter (material/brand/color) but a
    // DIFFERENT subtype — a dimension the spool list cannot filter on. The
    // row's served spool_ids are the engine's own membership; the panel must
    // narrow to them instead of re-implementing grouping client-side.
    setupHandlers({
      detailSpools: [detailSpool(1), detailSpool(2), detailSpool(99, { subtype: 'Matte' })],
    });
    render(<ForecastPanel />);
    await waitFor(() => expect(screen.getByText('eSun PLA Blue')).toBeInTheDocument());
    expect(spoolListRequests.length).toBe(0); // lazy: nothing until expand

    fireEvent.click(screen.getByText('eSun PLA Blue'));
    await waitFor(() => expect(spoolListRequests.length).toBe(1));
    const u = spoolListRequests[0];
    expect(u.searchParams.get('material')).toBe('PLA');
    expect(u.searchParams.get('brand')).toBe('eSun');
    expect(u.searchParams.getAll('colors')).toEqual(['Blue']);
    expect(u.searchParams.get('archived')).toBe('active');
    expect(u.searchParams.get('all')).toBe('true');

    await waitFor(() => expect(screen.getByText('#1')).toBeInTheDocument());
    expect(screen.getByText('#2')).toBeInTheDocument();
    expect(screen.queryByText('#99')).toBeNull();
  });

  it('a null-field SKU omits the inexpressible filters and still narrows to its own ids', async () => {
    setupHandlers({
      rows: [row({ material: 'ASA', brand: null, color_name: null, rgba: null, spool_ids: [5, 6] })],
      detailSpools: [
        detailSpool(5, { material: 'ASA', brand: null, color_name: null }),
        detailSpool(6, { material: 'ASA', brand: '', color_name: null }),
        detailSpool(7, { material: 'ASA', brand: 'Polymaker', color_name: null }),
      ],
    });
    render(<ForecastPanel />);
    await waitFor(() => expect(screen.getByText('ASA')).toBeInTheDocument());

    fireEvent.click(screen.getByText('ASA'));
    await waitFor(() => expect(spoolListRequests.length).toBe(1));
    const u = spoolListRequests[0];
    expect(u.searchParams.get('material')).toBe('ASA');
    // NULL brand/color cannot be said in the filter language — omitted, and
    // the served spool_ids do the exact narrowing instead.
    expect(u.searchParams.has('brand')).toBe(false);
    expect(u.searchParams.getAll('colors')).toEqual([]);

    await waitFor(() => expect(screen.getByText('#5')).toBeInTheDocument());
    expect(screen.getByText('#6')).toBeInTheDocument();
    expect(screen.queryByText('#7')).toBeNull();
  });

  it('renders the chart from served series and refetches on the timeframe toggle', async () => {
    setupHandlers({
      chart: [
        {
          sku: { material: 'PLA', subtype: null, brand: 'eSun', color_name: 'Blue' },
          rgba: '0000FFFF',
          rop_g: 200,
          usage: [['2026-08-25', 40]],
          projection: [
            ['2026-08-30', 400],
            ['2026-08-31', 390],
          ],
        },
      ],
    });
    render(<ForecastPanel />);
    // Default timeframe request, then the chart card renders off the response.
    await waitFor(() => expect(chartRequests.length).toBeGreaterThan(0));
    expect(chartRequests[0].searchParams.get('days')).toBe('30');
    await waitFor(() =>
      expect(screen.getByText('Projected Stock - Top 5 Materials')).toBeInTheDocument()
    );

    fireEvent.click(screen.getByText('1W'));
    await waitFor(() => expect(chartRequests.some((u) => u.searchParams.get('days') === '7')).toBe(true));
  });

  it('hides the chart when the server sends no series', async () => {
    setupHandlers({ chart: [] });
    render(<ForecastPanel />);
    await waitFor(() => expect(chartRequests.length).toBeGreaterThan(0));
    expect(screen.queryByText('Projected Stock - Top 5 Materials')).toBeNull();
  });

  it('the CSV button downloads through the server export endpoint', async () => {
    Object.defineProperty(window.URL, 'createObjectURL', {
      value: vi.fn(() => 'blob:test'),
      writable: true,
      configurable: true,
    });
    Object.defineProperty(window.URL, 'revokeObjectURL', {
      value: vi.fn(),
      writable: true,
      configurable: true,
    });
    // Keep jsdom from trying to navigate the blob: anchor; the spy also
    // proves the download was actually triggered.
    const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    setupHandlers({
      shoppingList: [
        {
          id: 1,
          material: 'PLA',
          subtype: null,
          brand: 'eSun',
          color_name: 'Blue',
          quantity_spools: 2,
          note: null,
          status: 'pending',
          purchased_at: null,
          added_at: '2026-08-01T00:00:00Z',
        },
      ],
    });
    render(<ForecastPanel />);
    await waitFor(() => expect(screen.getByText('Shopping List')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Shopping List'));
    await waitFor(() => expect(screen.getByText('CSV')).toBeInTheDocument());
    fireEvent.click(screen.getByText('CSV'));
    await waitFor(() => expect(csvRequests).toBe(1));
    await waitFor(() => expect(anchorClick).toHaveBeenCalledTimes(1));
    anchorClick.mockRestore();
  });

  it('the alert badge counts from the served alert_count and the banners feed from the alerts_only query', async () => {
    setupHandlers({
      rows: [row({ stock_break_alert: true, days_remaining: 3 })],
      alertCount: 2,
    });
    render(<ForecastPanel />);
    await waitFor(() => expect(screen.getByText('2 alerts')).toBeInTheDocument());
    // The banner feed: alerts_only=true&all=true — the farm-wide alert rows,
    // never the paged table slice.
    await waitFor(() =>
      expect(
        forecastRequests.some(
          (u) => u.searchParams.get('alerts_only') === 'true' && u.searchParams.get('all') === 'true'
        )
      ).toBe(true)
    );
  });

  it('clearing the last alert takes the OPEN banner panel down with its toggle', async () => {
    // The banner feed is farm-wide and the table is a paged slice, so the two
    // legitimately carry different SKUs — which is what makes the banner
    // identifiable here.
    const alertRow = row({
      material: 'ABS', brand: 'Polymaker', color_name: 'Red',
      stock_break_alert: true, days_remaining: 2, spool_ids: [8],
    });
    setupHandlers();
    let alertCount = 1;
    server.use(
      http.get('/api/v1/inventory/forecast', ({ request }) => {
        const url = new URL(request.url);
        forecastRequests.push(url);
        const alertsOnly = url.searchParams.get('alerts_only') === 'true';
        return HttpResponse.json({
          // ⚠️ The alerts feed deliberately keeps answering with the row. It
          // goes DISABLED the instant alert_count hits 0, and TanStack never
          // refetches a disabled query — not even on focus — so its cache
          // outlives the alert. That cache is what this guard defends against.
          items: alertsOnly ? [alertRow] : [row()],
          meta: { total: 1, current_page: 1, per_page: 50, last_page: 1 },
          alert_count: alertCount,
          global_lead_time_days: 3,
        });
      }),
      http.post('/api/v1/inventory/sku-settings', () => {
        alertCount = 0; // the snooze cleared the farm's last alert
        return HttpResponse.json({ id: 1 });
      }),
    );

    render(<ForecastPanel />);
    await waitFor(() => expect(screen.getByText('1 alert')).toBeInTheDocument());
    fireEvent.click(screen.getByText('1 alert'));
    await waitFor(() => expect(screen.getByText('Polymaker ABS Red')).toBeInTheDocument());

    // Snooze from the table row's bell — the review's exact scenario.
    fireEvent.click(screen.getByTitle('Mute alerts for this SKU'));

    // The toggle unmounts at alert_count 0. The banner must go with it, or
    // the panel keeps asserting an alert with no button left to dismiss it.
    await waitFor(() => expect(screen.queryByText('1 alert')).toBeNull());
    expect(screen.queryByText('Polymaker ABS Red')).toBeNull();
  });

  it('Mark received stays disabled while the cart-rows feed is in flight — no 1000g spools', async () => {
    setupHandlers({
      shoppingList: [{
        id: 1, material: 'PLA', subtype: null, brand: 'eSun', color_name: 'Blue',
        quantity_spools: 2, note: null, status: 'purchased',
        purchased_at: '2026-08-02T00:00:00Z', added_at: '2026-08-01T00:00:00Z',
      }],
    });
    let bulkCreates = 0;
    let statusPatches = 0;
    server.use(
      http.get('/api/v1/inventory/forecast', async ({ request }) => {
        const url = new URL(request.url);
        forecastRequests.push(url);
        // The cart-rows feed (all=true) never answers: the window between the
        // click that OPENS the panel and the row arriving is the whole
        // finding — until then rowFor() is null and avgSpoolG collapses to
        // the 1000 g fallback that bulkCreateSpools writes as label_weight.
        if (url.searchParams.get('all') === 'true') await delay('infinite');
        return HttpResponse.json({
          items: [row()],
          meta: { total: 1, current_page: 1, per_page: 50, last_page: 1 },
          alert_count: 0,
          global_lead_time_days: 3,
        });
      }),
      http.post('/api/v1/inventory/spools/bulk', () => {
        bulkCreates += 1;
        return HttpResponse.json([]);
      }),
      http.patch('/api/v1/inventory/shopping-list/:id/status', () => {
        statusPatches += 1;
        return HttpResponse.json({});
      }),
    );

    render(<ForecastPanel />);
    await waitFor(() => expect(screen.getByText('Shopping List')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Shopping List'));

    const received = await screen.findByTitle('Mark as received - adds spools to Stock inventory');
    expect(received).toBeDisabled();

    // A click inside the window must write nothing at all — permanently
    // wrong spool sizes are silent, so the gate is the only signal.
    fireEvent.click(received);
    await waitFor(() => expect(received).toBeDisabled());
    expect(bulkCreates).toBe(0);
    expect(statusPatches).toBe(0);
  });

  it('the logistics view WAITS instead of claiming "no usage data" while its feed is in flight', async () => {
    setupHandlers({
      shoppingList: [{
        id: 1, material: 'PLA', subtype: null, brand: 'eSun', color_name: 'Blue',
        quantity_spools: 2, note: null, status: 'pending',
        purchased_at: null, added_at: '2026-08-01T00:00:00Z',
      }],
    });
    server.use(
      http.get('/api/v1/inventory/forecast/logistics', async ({ request }) => {
        logisticsRequests.push(new URL(request.url));
        await delay('infinite');
        return HttpResponse.json([]);
      }),
    );

    render(<ForecastPanel />);
    await waitFor(() => expect(screen.getByText('Shopping List')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Shopping List'));
    fireEvent.click(await screen.findByText('Logistics'));

    // A missing logistics row means "not yet", not "none" — the definitive
    // negative is the one claim this view exists to make, so making it early
    // is worse than saying nothing.
    await waitFor(() => expect(screen.getByRole('status')).toBeInTheDocument());
    expect(
      screen.queryByText('No usage data available - cannot project stock timeline.')
    ).toBeNull();
  });

  it('the duration→spools suggestion uses the SKU\'s real spool size, archived spools included', async () => {
    // Task-4 review, Minor 5: an archived-only SKU — the one the 90-day
    // retention window is telling you to reorder — serves total_spools 0 and
    // total_label_g 0, because both are live-gated. The old code divided by
    // those and fell back to a fabricated 1000 g. `avg_spool_label_g` is the
    // engine's archived-inclusive mean and the only number here that can
    // answer "how big is a spool of this".
    //
    // 100 g/day × 30 days = 3000 g. At the real 750 g size that is 4 spools;
    // at the 1000 g fallback it would read 3 — so this fails loudly if the
    // suggestion ever goes back to the live totals.
    setupHandlers({
      rows: [row({ total_spools: 0, total_label_g: 0, avg_spool_label_g: 750, rate_g_day: 100 })],
    });
    render(<ForecastPanel />);

    fireEvent.click(await screen.findByTitle('Add to shopping list'));
    fireEvent.click(await screen.findByText('By Duration'));

    expect(await screen.findByText('4 spools')).toBeInTheDocument();
  });

  it('a SKU with no label weight anywhere keeps the documented 1000 g fallback', async () => {
    // null, not 0 — the server refuses to fabricate a size, and the client's
    // own guess is the honest last resort. 100 g/day × 30 days / 1000 = 3.
    setupHandlers({ rows: [row({ avg_spool_label_g: null, rate_g_day: 100 })] });
    render(<ForecastPanel />);

    fireEvent.click(await screen.findByTitle('Add to shopping list'));
    fireEvent.click(await screen.findByText('By Duration'));

    expect(await screen.findByText('3 spools')).toBeInTheDocument();
  });
});

// ── Final review wave ─────────────────────────────────────────────────────────

describe('ForecastPanel — failures are loud and the served mean is the only spool size', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  /** A purchased cart item for the panel's default PLA/eSun/Blue row. */
  const purchasedItem = {
    id: 1, material: 'PLA', subtype: null, brand: 'eSun', color_name: 'Blue',
    quantity_spools: 2, note: null, status: 'purchased' as const,
    purchased_at: '2026-08-02T00:00:00Z', added_at: '2026-08-01T00:00:00Z',
  };

  it('a failed spool create leaves the order UNTOUCHED — no status flip, and it says so (F1)', async () => {
    // The shipped order was PATCH-then-create: a 422 on the create left the
    // item `received` server-side with zero spools made, its button
    // permanently disabled, and not a word on screen. Creating FIRST means a
    // failure costs nothing but a retry.
    setupHandlers({ shoppingList: [purchasedItem] });
    let statusPatches = 0;
    let deletes = 0;
    server.use(
      http.post('/api/v1/inventory/spools/bulk', () => new HttpResponse(null, { status: 422 })),
      http.patch('/api/v1/inventory/shopping-list/:id/status', () => {
        statusPatches += 1;
        return HttpResponse.json({});
      }),
      http.delete('/api/v1/inventory/shopping-list/:id', () => {
        deletes += 1;
        return HttpResponse.json({ status: 'ok' });
      }),
    );

    render(<ForecastPanel />);
    fireEvent.click(await screen.findByText('Shopping List'));
    const received = await screen.findByTitle('Mark as received - adds spools to Stock inventory');
    await waitFor(() => expect(received).toBeEnabled());
    fireEvent.click(received);

    // The failure is dismissable, not silent…
    expect(
      await screen.findByText('Receiving failed. Retry to finish it - spools already added are not added again.')
    ).toBeInTheDocument();
    // …and nothing was committed: the row is still Purchased, still actionable.
    expect(statusPatches).toBe(0);
    expect(deletes).toBe(0);
    expect(screen.getByText('Purchased')).toBeInTheDocument();
  });

  it('a retry after a MID-SEQUENCE failure resumes instead of creating the spools twice (N2)', async () => {
    // Receiving is three writes. Creating first (F1) made the flow recoverable
    // and the toast tells the operator to retry — so the retry must not repeat
    // the write that already landed. The window: bulk-create succeeded, the
    // status PATCH 503'd. Clicking again used to mint a SECOND batch of N
    // spools, silently, on the advice of our own error message.
    let bulkCreates = 0;
    let patchAttempts = 0;
    let deletes = 0;
    let removed = false;
    setupHandlers({ shoppingList: [purchasedItem] });
    server.use(
      http.get('/api/v1/inventory/shopping-list', () =>
        HttpResponse.json(removed ? [] : [purchasedItem])
      ),
      http.post('/api/v1/inventory/spools/bulk', () => {
        bulkCreates += 1;
        return HttpResponse.json([]);
      }),
      http.patch('/api/v1/inventory/shopping-list/:id/status', () => {
        patchAttempts += 1;
        if (patchAttempts === 1) return new HttpResponse(null, { status: 503 });
        return HttpResponse.json({});
      }),
      http.delete('/api/v1/inventory/shopping-list/:id', () => {
        deletes += 1;
        removed = true;
        return HttpResponse.json({ status: 'ok' });
      }),
    );

    render(<ForecastPanel />);
    fireEvent.click(await screen.findByText('Shopping List'));
    const title = 'Mark as received - adds spools to Stock inventory';
    await waitFor(() => expect(screen.getByTitle(title)).toBeEnabled());
    fireEvent.click(screen.getByTitle(title));

    // First attempt: the spools landed, the status flip did not.
    await waitFor(() => expect(patchAttempts).toBe(1));
    expect(bulkCreates).toBe(1);
    expect(deletes).toBe(0);

    // The retry the toast advises RESUMES at the PATCH…
    await waitFor(() => expect(screen.getByTitle(title)).toBeEnabled());
    fireEvent.click(screen.getByTitle(title));
    await waitFor(() => expect(deletes).toBe(1));

    // …creating nothing a second time, and the order completes.
    expect(bulkCreates).toBe(1);
    expect(patchAttempts).toBe(2);
    await waitFor(() => expect(screen.queryByTitle(title)).toBeNull());
  });

  it('Mark received writes the SERVED spool size as label_weight, not a live-only mean (F2)', async () => {
    // The flagship flow: an archived-only SKU serves total_spools 0 and
    // total_label_g 0 (both live-gated), so the old divisor collapsed to the
    // fabricated 1000 g and PERSISTED it on every created spool. 750 is the
    // engine's archived-inclusive mean — the same number the cart dialog and
    // the bridge-gap count use.
    setupHandlers({
      rows: [row({ total_spools: 0, total_label_g: 0, avg_spool_label_g: 750 })],
      shoppingList: [purchasedItem],
    });
    let created: { spool: { label_weight: number }; quantity: number } | null = null;
    server.use(
      http.post('/api/v1/inventory/spools/bulk', async ({ request }) => {
        created = (await request.json()) as typeof created;
        return HttpResponse.json([]);
      }),
      http.patch('/api/v1/inventory/shopping-list/:id/status', () => HttpResponse.json({})),
      http.delete('/api/v1/inventory/shopping-list/:id', () => HttpResponse.json({ status: 'ok' })),
    );

    render(<ForecastPanel />);
    fireEvent.click(await screen.findByText('Shopping List'));
    const received = await screen.findByTitle('Mark as received - adds spools to Stock inventory');
    await waitFor(() => expect(received).toBeEnabled());
    fireEvent.click(received);

    await waitFor(() => expect(created).not.toBeNull());
    expect(created!.spool.label_weight).toBe(750);
    expect(created!.quantity).toBe(2);
  });

  it('a rejected global lead-time save says so instead of reverting in silence (F6)', async () => {
    // The editor closes synchronously on Save, so a failed PUT snapped the
    // number back to the served value with no explanation — plus an
    // unhandled rejection.
    setupHandlers();
    server.use(
      http.put('/api/v1/settings/', () => new HttpResponse(null, { status: 500 })),
    );
    render(<ForecastPanel />);

    // The toolbar paints before the feed answers, and the editor seeds its
    // input from the value at CLICK time — so wait for the served rows first,
    // or the 0 of the loading tick is what gets edited.
    await screen.findByText('eSun PLA Blue');
    const wrapper = screen.getByText('Global lead time:').parentElement!;
    fireEvent.click(wrapper.querySelector('button')!);
    const input = await screen.findByDisplayValue('3');
    fireEvent.change(input, { target: { value: '7' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    expect(await screen.findByText('Failed to save settings')).toBeInTheDocument();
  });

  it('a failed expanded-row spool fetch says so and offers a retry (F8)', async () => {
    // Without this the nested table renders its headers over zero rows —
    // indistinguishable from a SKU that simply has no spools. "Error is not
    // empty."
    setupHandlers();
    let attempts = 0;
    server.use(
      http.get('/api/v1/inventory/spools', ({ request }) => {
        attempts += 1;
        spoolListRequests.push(new URL(request.url));
        return new HttpResponse(null, { status: 500 });
      }),
    );
    render(<ForecastPanel />);
    fireEvent.click(await screen.findByText('eSun PLA Blue'));

    expect(await screen.findByText('Could not load the spools of this SKU')).toBeInTheDocument();
    // …and the way back is a live button, not a page reload.
    const before = attempts;
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(attempts).toBeGreaterThan(before));
  });
});
