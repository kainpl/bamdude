/**
 * Server-driven InventoryPage (task 4, 2026-08-29 server-driven-lists).
 *
 * The page now states its filters/sort/search/page as request params on
 * GET /inventory/spools (tasks 1-3 built the server side) — so these tests
 * pin the PARAMS the page sends, replacing the client-side filter
 * assertions that died with the client pipeline. The load-bearing pins:
 *
 * - `archived` is ALWAYS sent (the paged branch ignores the legacy
 *   `include_archived` — T1 review finding 6);
 * - a swatch-column sort remaps to `color_name` (operator ruling — the
 *   server has no rgba sort key);
 * - grouped mode never sends a sort outside its allowed subset (the server
 *   400s on those — the page must sanitize, not crash);
 * - "Select all N matching" rides the ids endpoint under the SAME filter
 *   params and materializes an explicit selection id set (the CLAUDE.md
 *   bulk invariant);
 * - the cards view opts into `include_k_profiles` (the slim-projection gap).
 */

import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { screen, waitFor, fireEvent, act } from '@testing-library/react';
import { focusManager } from '@tanstack/react-query';
import { render } from '../utils';
import InventoryPageRouter from '../../pages/InventoryPage';
import { http, HttpResponse, delay } from 'msw';
import { server } from '../mocks/server';
import type { SkuForecastRow } from '../../api/client';

const baseSpool = {
  subtype: null,
  color_name: 'Blue',
  rgba: '0000FFFF',
  extra_colors: null,
  effect_type: null,
  label_weight: 1000,
  core_weight: 250,
  core_weight_catalog_id: null,
  weight_used: 0,
  slicer_filament: null,
  slicer_filament_name: null,
  nozzle_temp_min: null,
  nozzle_temp_max: null,
  note: null,
  added_full: null,
  last_used: null,
  encode_time: null,
  tag_uid: null,
  tray_uuid: null,
  data_origin: null,
  tag_type: null,
  archived_at: null,
  created_at: '2025-01-01T00:00:00Z',
  updated_at: '2025-01-01T00:00:00Z',
  cost_per_kg: null,
  last_scale_weight: null,
  last_weighed_at: null,
  storage_location: null,
  location_id: null,
  purchase_location: null,
  purchase_date: null,
  filament_diameter: '1.75',
  lot: null,
  category: null,
  low_stock_threshold_pct: null,
};

const SPOOLS = [
  { ...baseSpool, id: 1, material: 'PLA', brand: 'eSun' },
  { ...baseSpool, id: 2, material: 'PETG', brand: 'SUNLU' },
];

const SETTINGS = {
  currency: 'USD',
  language: 'en',
  date_format: 'system',
  time_format: 'system',
  low_stock_threshold: 20.0,
};

let listRequests: URL[] = [];
let idsRequests: URL[] = [];
let facetsRequests: URL[] = [];
let forecastRequests: URL[] = [];
let statsRequests: URL[] = [];

// GET /inventory/stats (task 5) — the stats bar's five cards, aggregated
// server-side. It replaced the page's last full-table `all=true` fetch, so
// every test in this file that renders the page needs it answered or the bar
// never appears.
const STATS = {
  total_spools: SPOOLS.length,
  active_spools: SPOOLS.length,
  total_weight_g: 2000,
  total_consumed_g: 0,
  by_material: [
    { material: 'PLA', count: 1, remaining_g: 1000 },
    { material: 'PETG', count: 1, remaining_g: 1000 },
  ],
  low_stock_count: 0,
};

// The Forecast tab renders SERVER-computed rows since task 4 of the
// 2026-08-29 forecast-server-side cycle — the panel's old all=true spool
// feed is gone; opening the tab hits GET /inventory/forecast instead.
// ⚠️ TYPED on purpose: an untyped fixture serves a shape the server cannot
// send, and the next field added to SkuForecastRow goes missing here in
// silence (`avg_spool_label_g` already had).
//
// ⚠️ …but the annotation is documentation, NOT an armed guard, today. `npm run
// typecheck` is `tsc -b`; the root tsconfig references only app+node, and
// tsconfig.app.json EXCLUDES src/__tests__. `tsconfig.test.json` covers this
// tree but nothing references it — measured 2026-08-30: deleting a required
// field here still passes the gate, and `tsc -p tsconfig.test.json` reports
// 170 pre-existing errors (mostly missing `vite/client` types for asset
// imports and `import.meta.env`). Wiring the test project into the build is
// its own piece of work; this annotation starts failing red the day it lands,
// which is why it is written now.
const FORECAST_ROWS: SkuForecastRow[] = SPOOLS.map((s) => ({
  material: s.material,
  subtype: null,
  brand: s.brand,
  color_name: s.color_name,
  rgba: s.rgba,
  total_spools: 1,
  total_remaining_g: 1000,
  total_label_g: 1000,
  avg_spool_label_g: 1000,
  total_used_g: 0,
  rate_g_day: null,
  rate_tier: 'none',
  std_dev: null,
  eff_lead_time_days: 0,
  safety_stock_g: 70,
  reorder_point_g: 0,
  days_remaining: null,
  projected_empty_date: null,
  days_until_rop: null,
  reorder_trigger_date: null,
  stock_break_alert: false,
  reorder_alert: false,
  alerts_snoozed: false,
  spool_ids: [s.id],
}));

function setupHandlers() {
  listRequests = [];
  idsRequests = [];
  facetsRequests = [];
  forecastRequests = [];
  statsRequests = [];
  server.use(
    http.get('/api/v1/inventory/stats', ({ request }) => {
      statsRequests.push(new URL(request.url));
      return HttpResponse.json(STATS);
    }),
    http.get('/api/v1/inventory/forecast', ({ request }) => {
      forecastRequests.push(new URL(request.url));
      return HttpResponse.json({
        items: FORECAST_ROWS,
        meta: { total: FORECAST_ROWS.length, current_page: 1, per_page: 50, last_page: 1 },
        alert_count: 0,
        global_lead_time_days: 0,
      });
    }),
    http.get('/api/v1/inventory/forecast/chart', () => HttpResponse.json({ series: [] })),
    http.get('/api/v1/inventory/forecast/logistics', () => HttpResponse.json([])),
    http.get('/api/v1/settings/', () => HttpResponse.json(SETTINGS)),
    http.get('/api/v1/settings/spoolman', () =>
      HttpResponse.json({ spoolman_enabled: 'false', spoolman_url: '' })
    ),
    http.get('/api/v1/inventory/spools/facets', ({ request }) => {
      facetsRequests.push(new URL(request.url));
      return HttpResponse.json({
        materials: ['PLA', 'PETG'],
        brands: ['eSun', 'SUNLU'],
        categories: [],
        catalog_ids: [],
        colors: [{ color_name: 'Blue', rgba: '0000FFFF' }],
      });
    }),
    http.get('/api/v1/inventory/spools/ids', ({ request }) => {
      idsRequests.push(new URL(request.url));
      return HttpResponse.json({ ids: SPOOLS.map((s) => s.id) });
    }),
    http.get('/api/v1/inventory/spools', ({ request }) => {
      const url = new URL(request.url);
      // ⚠️ Record BEFORE the page-less early return. The legacy flat-array
      // shape (`?include_archived=…`, no paging params) is a full download
      // too — recording only paged calls gave the "ZERO all=true" guard a
      // blind spot exactly where the last full fetch hides.
      listRequests.push(url);
      if (!url.searchParams.has('page')) return HttpResponse.json(SPOOLS);
      if (url.searchParams.get('group_similar') === 'true') {
        return HttpResponse.json({
          items: SPOOLS.map((s) => ({
            material: s.material,
            subtype: s.subtype ?? '',
            brand: s.brand ?? '',
            color_name: s.color_name ?? '',
            rgba: s.rgba ?? '',
            label_weight: s.label_weight,
            lot: s.lot ?? null,
            group_count: 1,
            ids: [s.id],
            representative: { ...s, k_profile_count: 0, k_profiles: null },
          })),
          meta: { total: SPOOLS.length, current_page: 1, per_page: 24, last_page: 1 },
        });
      }
      return HttpResponse.json({
        items: SPOOLS.map((s) => ({ ...s, k_profile_count: 0, k_profiles: null })),
        meta: { total: SPOOLS.length, current_page: 1, per_page: 24, last_page: 1 },
      });
    }),
    http.get('/api/v1/inventory/assignments', () => HttpResponse.json([])),
    http.get('/api/v1/inventory/catalog', () => HttpResponse.json([])),
    http.get('/api/v1/inventory/locations', () => HttpResponse.json([])),
  );
}

/** The list request driving the visible page. */
const pageRequests = () =>
  listRequests.filter((u) => u.searchParams.has('page') && u.searchParams.get('all') !== 'true');
/** The LEGACY page-less shape — `GET /inventory/spools?include_archived=…`,
 *  answered as a bare full array. It carries no `all=true`, so the full-set
 *  filter below can never see it; it is a full download all the same, and the
 *  rule it must obey is the same one: never on a plain visit, modal-gated
 *  only. Its last caller is SpoolFormModal's category datalist. */
const legacyArrayRequests = () => listRequests.filter((u) => !u.searchParams.has('page'));
/** The full-set slim fetches. Since forecast-server-side task 5 a plain visit
 *  fires NONE: the stats bar reads GET /inventory/stats, ForecastPanel renders
 *  server-computed rows, and the remaining full-set consumers (bulk edit,
 *  "Reset all usage") fetch only while their own surface is open. */
const fullSetRequests = () => listRequests.filter((u) => u.searchParams.get('all') === 'true');

describe('InventoryPage — server-driven params (task 4)', () => {
  beforeEach(() => {
    localStorage.clear();
    setupHandlers();
  });

  it('sends page, per_page and an EXPLICIT archived=active on first load — never include_archived', async () => {
    render(<InventoryPageRouter />);
    await waitFor(() => expect(pageRequests().length).toBeGreaterThan(0));
    const u = pageRequests()[0];
    expect(u.searchParams.get('page')).toBe('1');
    expect(u.searchParams.get('per_page')).toBe('24');
    // The paged branch ignores include_archived entirely (T1 finding 6):
    // the page must state the tab explicitly on every call.
    expect(u.searchParams.get('archived')).toBe('active');
    expect(u.searchParams.has('include_archived')).toBe(false);
  });

  it('the full-set feed survives ONLY behind bulk edit — slim, both tabs, never on the render path', async () => {
    // Task 5 replaced the page-load stats feed with GET /inventory/stats. The
    // full spool SET still has two consumers that need every spool OBJECT
    // (bulk edit's cross-page selection + suggestion pool, and "Reset all
    // usage"'s id list), so it did not disappear — it became modal-gated.
    render(<InventoryPageRouter />);
    await waitFor(() => expect(pageRequests().length).toBeGreaterThan(0));
    expect(fullSetRequests()).toEqual([]);

    fireEvent.click(screen.getByRole('button', { name: /Bulk edit/ }));

    await waitFor(() => expect(fullSetRequests().length).toBe(1));
    const fullSet = fullSetRequests()[0];
    // Spans BOTH tabs (the selection and the suggestion pool are not
    // tab-scoped) — deliberately no archived param, and never the
    // k_profiles opt-in.
    expect(fullSet.searchParams.has('archived')).toBe(false);
    expect(fullSet.searchParams.has('include_k_profiles')).toBe(false);
    // And the modal waits for it rather than mounting empty and filling.
    await waitFor(() => expect(screen.getByText('Bulk edit spools')).toBeInTheDocument());
  });

  it('a FAILED id fetch leaves the reset dialog dismissable — and refuses to post an empty list', async () => {
    // Review finding 2. ConfirmModal treats `isLoading` as modal-WIDE: it
    // disables Cancel as well as Confirm, swallows Escape and nulls the
    // backdrop click. A failed fetch settles with `data === undefined`
    // forever, so gating on data alone left the dialog with two dead buttons
    // and no way out but a page reload — losing filters and selection.
    let resetPosts = 0;
    server.use(
      http.get('/api/v1/inventory/stats', () =>
        HttpResponse.json({ ...STATS, total_consumed_g: 1234, total_spools: 2 })
      ),
      http.get('/api/v1/inventory/spools', ({ request }) => {
        const url = new URL(request.url);
        listRequests.push(url);
        if (url.searchParams.get('all') === 'true') return new HttpResponse(null, { status: 500 });
        if (!url.searchParams.has('page')) return HttpResponse.json(SPOOLS);
        return HttpResponse.json({
          items: SPOOLS.map((s) => ({ ...s, k_profile_count: 0, k_profiles: null })),
          meta: { total: SPOOLS.length, current_page: 1, per_page: 24, last_page: 1 },
        });
      }),
      http.post('/api/v1/inventory/spools/reset-consumed-counter-bulk', () => {
        resetPosts += 1;
        return HttpResponse.json({ reset: 0 });
      })
    );
    render(<InventoryPageRouter />);

    fireEvent.click(await screen.findByLabelText('Reset all counters'));
    const cancel = await screen.findByRole('button', { name: 'Cancel' });
    // The query's one retry has to burn before it settles into error.
    await waitFor(() => expect(cancel).toBeEnabled(), { timeout: 5000 });

    // Confirming in this state must not report success over nothing…
    fireEvent.click(screen.getByRole('button', { name: 'Reset counter' }));
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Cancel' })).toBeNull());
    expect(resetPosts).toBe(0);

    // …and the way out is a working Cancel, not a page reload.
    fireEvent.click(await screen.findByLabelText('Reset all counters'));
    const cancelAgain = await screen.findByRole('button', { name: 'Cancel' });
    await waitFor(() => expect(cancelAgain).toBeEnabled(), { timeout: 5000 });
    fireEvent.click(cancelAgain);
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Cancel' })).toBeNull());
  });

  it('a FAILED full-set fetch makes "Bulk edit" say so instead of dying silently', async () => {
    // Review finding 3, the twin of the loading guards: "error is not
    // loading". The modal renders nothing until the set arrives, so without
    // this the button simply stops responding — and re-clicking it re-sets
    // state it already holds, so nothing re-renders and nothing refetches.
    let fullSetAttempts = 0;
    server.use(
      http.get('/api/v1/inventory/spools', ({ request }) => {
        const url = new URL(request.url);
        listRequests.push(url);
        if (url.searchParams.get('all') === 'true') {
          fullSetAttempts += 1;
          return new HttpResponse(null, { status: 500 });
        }
        if (!url.searchParams.has('page')) return HttpResponse.json(SPOOLS);
        return HttpResponse.json({
          items: SPOOLS.map((s) => ({ ...s, k_profile_count: 0, k_profiles: null })),
          meta: { total: SPOOLS.length, current_page: 1, per_page: 24, last_page: 1 },
        });
      })
    );
    render(<InventoryPageRouter />);
    await waitFor(() => expect(pageRequests().length).toBeGreaterThan(0));

    fireEvent.click(screen.getByRole('button', { name: /Bulk edit/ }));
    expect(await screen.findByText('Bulk action failed')).toBeInTheDocument();
    expect(screen.queryByText('Bulk edit spools')).toBeNull();

    // And the button is live again: closing on the error disables the query,
    // so the next click re-enables it and a fresh attempt starts.
    const attemptsAfterFirst = fullSetAttempts;
    fireEvent.click(screen.getByRole('button', { name: /Bulk edit/ }));
    await waitFor(() => expect(fullSetAttempts).toBeGreaterThan(attemptsAfterFirst), { timeout: 5000 });
  });

  it('"Reset all counters" offers itself off the SERVED count and fetches its ids only once armed', async () => {
    // The button used to be gated on a fetched id list — which is why the
    // list had to be fetched on every visit. It now reads the served counts,
    // and the ids arrive behind the confirmation.
    let resetBody: { spool_ids: number[] } | null = null;
    server.use(
      http.get('/api/v1/inventory/stats', () =>
        HttpResponse.json({ ...STATS, total_consumed_g: 1234, total_spools: 2 })
      ),
      http.post('/api/v1/inventory/spools/reset-consumed-counter-bulk', async ({ request }) => {
        resetBody = (await request.json()) as { spool_ids: number[] };
        return HttpResponse.json({ reset: resetBody.spool_ids.length });
      })
    );
    render(<InventoryPageRouter />);

    const eraser = await screen.findByLabelText('Reset all counters');
    expect(fullSetRequests()).toEqual([]); // offered without fetching anything

    fireEvent.click(eraser);
    // The confirmation's count is served, so it is right immediately.
    await waitFor(() => expect(screen.getByText(/all 2 active spools/)).toBeInTheDocument());
    await waitFor(() => expect(fullSetRequests().length).toBe(1));

    fireEvent.click(screen.getByRole('button', { name: 'Reset counter' }));
    // Every id, archived included — the mutation is unchanged, only its
    // source moved behind the arming click.
    await waitFor(() => expect(resetBody).toEqual({ spool_ids: [1, 2] }));
  });

  it('the Archived tab re-states archived=archived on the list AND the facets', async () => {
    render(<InventoryPageRouter />);
    await waitFor(() => expect(pageRequests().length).toBeGreaterThan(0));

    fireEvent.click(screen.getByRole('button', { name: /^Archived$/ }));

    await waitFor(() =>
      expect(pageRequests().some((u) => u.searchParams.get('archived') === 'archived')).toBe(true)
    );
    await waitFor(() =>
      expect(facetsRequests.some((u) => u.searchParams.get('archived') === 'archived')).toBe(true)
    );
  });

  it('a usage chip becomes the usage param', async () => {
    render(<InventoryPageRouter />);
    await waitFor(() => expect(pageRequests().length).toBeGreaterThan(0));

    fireEvent.click(screen.getByRole('button', { name: /^Used$/ }));

    await waitFor(() =>
      expect(pageRequests().some((u) => u.searchParams.get('usage') === 'used')).toBe(true)
    );
  });

  it('search reaches the server as q (debounced)', async () => {
    render(<InventoryPageRouter />);
    await waitFor(() => expect(pageRequests().length).toBeGreaterThan(0));

    fireEvent.change(screen.getByPlaceholderText(/search/i), { target: { value: 'sun' } });

    await waitFor(
      () => expect(pageRequests().some((u) => u.searchParams.get('q') === 'sun')).toBe(true),
      { timeout: 2000 },
    );
  });

  it('a swatch-column sort is remapped to color_name (operator ruling — no rgba sort key server-side)', async () => {
    localStorage.setItem('bamdude-inventory-sort', JSON.stringify({ column: 'rgba', direction: 'asc' }));
    render(<InventoryPageRouter />);
    await waitFor(() => expect(pageRequests().length).toBeGreaterThan(0));
    const u = pageRequests()[0];
    expect(u.searchParams.get('sort_by')).toBe('color_name_asc');
  });

  it('grouped mode drops a sort outside the group-key subset instead of sending it (the server 400s on those)', async () => {
    localStorage.setItem('bamdude-inventory-group', 'true');
    localStorage.setItem('bamdude-inventory-sort', JSON.stringify({ column: 'weight_check', direction: 'desc' }));
    render(<InventoryPageRouter />);
    await waitFor(() =>
      expect(pageRequests().some((u) => u.searchParams.get('group_similar') === 'true')).toBe(true)
    );
    const grouped = pageRequests().find((u) => u.searchParams.get('group_similar') === 'true')!;
    expect(grouped.searchParams.has('sort_by')).toBe(false);
  });

  it('grouped mode keeps a group-key sort (color_name via the swatch remap included)', async () => {
    localStorage.setItem('bamdude-inventory-group', 'true');
    localStorage.setItem('bamdude-inventory-sort', JSON.stringify({ column: 'color_combined', direction: 'desc' }));
    render(<InventoryPageRouter />);
    await waitFor(() =>
      expect(
        pageRequests().some(
          (u) =>
            u.searchParams.get('group_similar') === 'true' &&
            u.searchParams.get('sort_by') === 'color_name_desc',
        ),
      ).toBe(true)
    );
  });

  it('"Select all N matching" rides the ids endpoint under the same filters and materializes the selection', async () => {
    render(<InventoryPageRouter />);
    // Wait for rows.
    await waitFor(() => expect(screen.getAllByLabelText('Select this spool').length).toBe(2));

    // Tick one row — the bulk bar appears with the explicit select-all action.
    fireEvent.click(screen.getAllByLabelText('Select this spool')[0]);
    const selectAll = await screen.findByRole('button', { name: /Select all 2 matching the filter/ });
    fireEvent.click(selectAll);

    await waitFor(() => expect(idsRequests.length).toBeGreaterThan(0));
    const u = idsRequests[0];
    // Same tab scoping as the list — and never any paging/sort params.
    expect(u.searchParams.get('archived')).toBe('active');
    expect(u.searchParams.has('page')).toBe(false);
    expect(u.searchParams.has('sort_by')).toBe(false);

    // The answer became an explicit id set: the toolbar now counts ALL of it.
    await waitFor(() => expect(screen.getByText('2 selected')).toBeInTheDocument());
  });

  it('the cards view opts into include_k_profiles (per-profile chips need the array — T1 projection gap)', async () => {
    localStorage.setItem(
      'bamdude-inventory-filters',
      JSON.stringify({ viewMode: 'cards' }),
    );
    render(<InventoryPageRouter />);
    await waitFor(() =>
      expect(pageRequests().some((u) => u.searchParams.get('include_k_profiles') === 'true')).toBe(true)
    );
  });
});

describe('InventoryPage — the Forecast tab renders server-computed rows', () => {
  beforeEach(() => {
    localStorage.clear();
    setupHandlers();
    // The panel's remaining side feeds — the settings CRUD surfaces and the
    // shopping list stayed client-called through the forecast-server-side
    // rewrite; answered empty so opening the tab settles cleanly. The
    // 5000-row /inventory/usage feed is deliberately NOT handled: the panel
    // must never call it again, and an unhandled request would fail loudly.
    server.use(
      http.get('/api/v1/inventory/sku-settings', () => HttpResponse.json([])),
      http.get('/api/v1/inventory/shopping-list', () => HttpResponse.json([])),
    );
  });

  it('a plain inventory visit fires ZERO all=true fetches — the cycle\'s whole point', async () => {
    render(<InventoryPageRouter />);
    // Settle: rows on screen and the stats bar rendered off the endpoint. A
    // leaked panel query would show up as a GET /inventory/forecast —
    // mount-time queries fire in the same tick as the page's own, so by the
    // time the list has settled it would already be in the count.
    await waitFor(() => expect(screen.getAllByLabelText('Select this spool').length).toBe(2));
    await waitFor(() => expect(statsRequests.length).toBe(1));
    // Task 5: the last full-table fetch is gone. The stats bar reads
    // GET /inventory/stats and the remaining full-set consumers are gated on
    // their own modal being open — nothing downloads the inventory to render
    // a page of it.
    expect(fullSetRequests()).toEqual([]);
    expect(forecastRequests.length).toBe(0);
  });

  it('the stats cards render the SERVED numbers, not a client aggregate', async () => {
    server.use(
      http.get('/api/v1/inventory/stats', () =>
        HttpResponse.json({
          ...STATS,
          total_weight_g: 4321,
          low_stock_count: 7,
          active_spools: 9,
          by_material: [{ material: 'ASA', count: 3, remaining_g: 4321 }],
        })
      )
    );
    render(<InventoryPageRouter />);

    // Numbers no client aggregate over the two mocked spools could produce.
    await waitFor(() => expect(screen.getByText('7')).toBeInTheDocument()); // low stock
    expect(screen.getByText(/9 spools/)).toBeInTheDocument(); // active count
    expect(screen.getByText('ASA')).toBeInTheDocument(); // by-material chip
    expect(fullSetRequests()).toEqual([]);
  });

  it('opening the Forecast tab fires the server forecast query — never a full-set spool feed (task 4)', async () => {
    render(<InventoryPageRouter />);
    await waitFor(() => expect(screen.getAllByLabelText('Select this spool').length).toBe(2));
    const fullSetBefore = fullSetRequests().length; // the stats feed

    const forecastTab = screen.getByRole('button', { name: /^Forecast$/ });
    await waitFor(() => expect(forecastTab).toBeEnabled());
    fireEvent.click(forecastTab);

    // The panel's ONE feed: server-sorted, server-paged forecast rows.
    await waitFor(() => expect(forecastRequests.length).toBeGreaterThan(0));
    const u = forecastRequests[0];
    expect(u.searchParams.get('page')).toBe('1');
    expect(u.searchParams.get('per_page')).toBe('50');
    expect(u.searchParams.get('sort_by')).toBe('material_asc');

    // And the panel renders its SKU rows off the served response.
    await waitFor(() => expect(screen.getByText('eSun PLA Blue')).toBeInTheDocument());
    expect(screen.getByText('SUNLU PETG Blue')).toBeInTheDocument();

    // The old panel-owned all=true spool feed (and the 5000-row usage feed)
    // are DEAD — no new full-set request beyond the page's own stats feed.
    expect(fullSetRequests().length).toBe(fullSetBefore);
  });

  it('Spoolman mode + a stale persisted forecast viewMode never mounts the panel — zero local-inventory traffic (task-6 guard, T5 review Minor 1)', async () => {
    // The Forecast tab is unreachable in Spoolman mode (the button is hidden),
    // but viewMode persists in localStorage: use Forecast in local mode, turn
    // Spoolman on, revisit — without the render-branch guard the panel would
    // mount and fetch the LOCAL inventory under a Spoolman UI.
    localStorage.setItem('bamdude-inventory-filters', JSON.stringify({ viewMode: 'forecast' }));
    server.use(
      // Enabled with NO url = the proxied-inventory Spoolman flavor — the
      // one that renders OUR page with spoolmanMode=true (a url would render
      // Spoolman's own iframe and never reach this code at all).
      http.get('/api/v1/settings/spoolman', () =>
        HttpResponse.json({ spoolman_enabled: 'true', spoolman_url: '' })
      ),
      http.get('/api/v1/spoolman/inventory/spools', () => HttpResponse.json(SPOOLS)),
    );
    render(<InventoryPageRouter />);

    // The guard falls back to the (Spoolman-fed) table view…
    await waitFor(() => expect(screen.getAllByLabelText('Select this spool').length).toBe(2));
    // …the panel never rendered. ⚠️ The marker is a PANEL-ONLY control, not
    // the joined "brand material colour" label: the Spoolman table's own
    // display_name column composes the very same string, so that assertion
    // only ever passed on a timing accident (the content area happened to be
    // showing "Loading…" at this instant). Gating the page-level list feed on
    // the forecast verdict — final review F4 — removed the flicker that
    // produced the accident, and the table now legitimately reaches its
    // steady state here.
    expect(screen.queryByText('Shopping List')).toBeNull();
    expect(screen.queryByText('Global lead time:')).toBeNull();
    // …and the panel's feed never fired: since task 4 that feed is
    // GET /inventory/forecast, and a mounted panel would have called it.
    // The cold-load flicker this test used to tolerate (spoolmanMode reads
    // false for the first tick, so the page-level flat list fired once) is
    // GONE: final review F4 gates that feed on the forecast verdict, so a
    // stored forecast viewMode fetches nothing at all until the verdict is in.
    expect(forecastRequests.length).toBe(0);
    // Task 5: zero, not "at most one". No full-table fetch survives a visit
    // in EITHER mode — the flicker can no longer cost the whole inventory,
    // because nothing on the render path asks for it any more.
    expect(fullSetRequests()).toEqual([]);
    expect(listRequests.length).toBeLessThanOrEqual(2);
  });
});

describe('InventoryPage — page mutations drop the selection (review round 2, finding 1)', () => {
  beforeEach(() => {
    localStorage.clear();
    setupHandlers();
  });

  // The clamp test drives a background refetch through TanStack's public
  // focusManager; restore its automatic behaviour so no state leaks into
  // the other tests in this file.
  afterEach(() => {
    focusManager.setFocused(undefined);
  });

  /**
   * Shadows the default spools handler with a MULTI-PAGE, mutable-meta pool:
   * the pager renders its arrows only when last_page > 1, and shrinking
   * `state.lastPage` between requests reproduces the background-shrink race
   * the out-of-range clamp exists for.
   */
  function useMultiPagePool(state: { lastPage: number; total: number }) {
    server.use(
      http.get('/api/v1/inventory/spools', ({ request }) => {
        const url = new URL(request.url);
        listRequests.push(url);
        if (!url.searchParams.has('page')) return HttpResponse.json(SPOOLS);
        if (url.searchParams.get('all') === 'true') {
          return HttpResponse.json({
            items: SPOOLS.map((s) => ({ ...s, k_profile_count: 0, k_profiles: null })),
            meta: { total: SPOOLS.length, current_page: 1, per_page: SPOOLS.length, last_page: 1 },
          });
        }
        return HttpResponse.json({
          items: SPOOLS.map((s) => ({ ...s, k_profile_count: 0, k_profiles: null })),
          meta: {
            total: state.total,
            current_page: Number(url.searchParams.get('page') ?? '1'),
            per_page: 24,
            last_page: state.lastPage,
          },
        });
      }),
    );
  }

  it('a pager click clears the selection — the bulk bar never stays armed over swapped-out rows', async () => {
    useMultiPagePool({ lastPage: 2, total: 30 });
    render(<InventoryPageRouter />);
    await waitFor(() => expect(screen.getAllByLabelText('Select this spool').length).toBe(2));

    fireEvent.click(screen.getAllByLabelText('Select this spool')[0]);
    expect(await screen.findByText('1 selected')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Next page' }));

    // The handler serves the SAME rows on page 2, so if the clear were
    // removed the still-selected id would keep the bar reading "1 selected"
    // — this waitFor would time out (verified red under exactly that
    // mutation).
    await waitFor(() => expect(screen.queryByText('1 selected')).not.toBeInTheDocument());
  });

  it('an out-of-range clamp drops the selection with the page — the twice-shipped library bug', async () => {
    const state = { lastPage: 2, total: 30 };
    useMultiPagePool(state);
    render(<InventoryPageRouter />);
    await waitFor(() => expect(screen.getAllByLabelText('Select this spool').length).toBe(2));

    // Land on page 2 while it legitimately exists.
    fireEvent.click(screen.getByRole('button', { name: 'Next page' }));
    await waitFor(() =>
      expect(pageRequests().some((u) => u.searchParams.get('page') === '2')).toBe(true)
    );

    // Arm a selection on the later page.
    fireEvent.click(screen.getAllByLabelText('Select this spool')[0]);
    expect(await screen.findByText('1 selected')).toBeInTheDocument();

    // The inventory shrinks under us; the next background refetch (driven
    // deterministically through the same focusManager transition a real
    // window refocus produces) answers page 2 with last_page=1 → the
    // render-phase clamp must reset the page AND drop the selection.
    state.lastPage = 1;
    state.total = 2;
    act(() => {
      focusManager.setFocused(false);
      focusManager.setFocused(true);
    });

    await waitFor(() => expect(screen.queryByText('1 selected')).not.toBeInTheDocument());
    // And the clamp corrected the page — the follow-up request asks page 1.
    await waitFor(() => {
      const reqs = pageRequests();
      expect(reqs[reqs.length - 1].searchParams.get('page')).toBe('1');
    });
  });
});

// ── Final review wave ─────────────────────────────────────────────────────────

describe('InventoryPage — the Forecast tab is not a second list (final review, F3/F4/F9)', () => {
  beforeEach(() => {
    localStorage.clear();
    setupHandlers();
    // The panel's remaining client-called side feeds.
    server.use(
      http.get('/api/v1/inventory/sku-settings', () => HttpResponse.json([])),
      http.get('/api/v1/inventory/shopping-list', () => HttpResponse.json([])),
    );
  });

  /** Land straight on the Forecast tab, the way a returning operator does. */
  const onForecastTab = () =>
    localStorage.setItem('bamdude-inventory-filters', JSON.stringify({ viewMode: 'forecast' }));

  it('fetches NO page of spool rows under the Forecast tab — and a persisted "All" cannot resurrect a full-table fetch there (F4)', async () => {
    // The sharp half of the finding: `pageSize` persists and accepts -1 →
    // `{all: true}`. Before the viewMode gate, a user who had once picked
    // "All" in the spools pager was firing a FULL-TABLE download every 30
    // seconds while sitting on the Forecast tab — the precise fetch this
    // cycle exists to have deleted, back through a stored preference. The
    // older "zero all=true on a plain visit" pins missed it because they run
    // at the default page size.
    onForecastTab();
    localStorage.setItem('bamdude-inventory-pageSize', '-1');
    render(<InventoryPageRouter />);

    // The panel paints its served rows…
    await waitFor(() => expect(screen.getByText('eSun PLA Blue')).toBeInTheDocument());
    // …and the list feed never ran, in either shape.
    expect(fullSetRequests()).toEqual([]);
    expect(pageRequests()).toEqual([]);
  });

  it('paints the panel without waiting on the spools list feed (F4)', async () => {
    // The panel used to render BEHIND `listLoading` — a LoadingBlock stood in
    // its place until a page of rows nobody displays had resolved. Here that
    // feed would never resolve at all; the panel must not care.
    onForecastTab();
    server.use(
      http.get('/api/v1/inventory/spools', async ({ request }) => {
        listRequests.push(new URL(request.url));
        await delay('infinite');
      }),
    );
    render(<InventoryPageRouter />);

    expect(await screen.findByText('eSun PLA Blue')).toBeInTheDocument();
    expect(screen.getByText('SUNLU PETG Blue')).toBeInTheDocument();
    // The caption still counts, off the served stats rather than list meta —
    // a bare `serverMeta?.total ?? 0` would read 0 here and disable the header
    // actions over a full inventory.
    expect(screen.getAllByText(/2 spools/).length).toBeGreaterThan(0);
  });

  it('a spool mutation made WHILE the Forecast tab is open refreshes the forecast (F3)', async () => {
    // The review's scenario, in the cheapest reachable form: the stats bar
    // renders on the Forecast tab, so its "Reset all counters" action is a
    // spool mutation fired with the panel mounted. Resetting usage flips whole
    // spools out of the engine's history rate tier — before the fix the panel
    // kept the pre-mutation rate, days-left and banners for the whole sitting.
    onForecastTab();
    server.use(
      http.get('/api/v1/inventory/stats', () =>
        HttpResponse.json({ ...STATS, total_consumed_g: 1234, total_spools: 2 })
      ),
      http.post('/api/v1/inventory/spools/reset-consumed-counter-bulk', () =>
        HttpResponse.json({ reset: 2 })
      ),
    );
    render(<InventoryPageRouter />);

    await waitFor(() => expect(screen.getByText('eSun PLA Blue')).toBeInTheDocument());
    const before = forecastRequests.length;
    expect(before).toBeGreaterThan(0);

    fireEvent.click(await screen.findByLabelText('Reset all counters'));
    await waitFor(() => expect(fullSetRequests().length).toBe(1)); // ids arrive on arming
    fireEvent.click(screen.getByRole('button', { name: 'Reset counter' }));

    await waitFor(() => expect(forecastRequests.length).toBeGreaterThan(before));
  });

  it('a plain visit fires no PAGE-LESS full array either — that shape is modal-gated (F9)', async () => {
    // `GET /inventory/spools?include_archived=false` answers a bare full
    // array. It carries no `all=true`, so the full-set guard above is blind to
    // it — which is exactly where the last full download could hide. Its only
    // caller left is SpoolFormModal's category datalist, and a modal is not a
    // visit.
    render(<InventoryPageRouter />);
    await waitFor(() => expect(screen.getAllByLabelText('Select this spool').length).toBe(2));
    expect(legacyArrayRequests()).toEqual([]);

    fireEvent.click(screen.getByRole('button', { name: /Add Spool/ }));
    await waitFor(() => expect(legacyArrayRequests().length).toBe(1));
    expect(legacyArrayRequests()[0].searchParams.has('page')).toBe(false);
  });
});
