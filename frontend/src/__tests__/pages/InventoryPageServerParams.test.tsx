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
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

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

// The Forecast tab renders SERVER-computed rows since task 4 of the
// 2026-08-29 forecast-server-side cycle — the panel's old all=true spool
// feed is gone; opening the tab hits GET /inventory/forecast instead.
const FORECAST_ROWS = SPOOLS.map((s) => ({
  material: s.material,
  subtype: null,
  brand: s.brand,
  color_name: s.color_name,
  rgba: s.rgba,
  total_spools: 1,
  total_remaining_g: 1000,
  total_label_g: 1000,
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
  server.use(
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
      if (!url.searchParams.has('page')) return HttpResponse.json(SPOOLS);
      listRequests.push(url);
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

/** The list request driving the visible page (the all=true one is the
 *  separate stats feed). */
const pageRequests = () => listRequests.filter((u) => u.searchParams.get('all') !== 'true');
/** The full-set slim fetches. Since forecast-server-side task 4 the ONLY
 *  legitimate one is the page-level stats feed — ForecastPanel renders
 *  server-computed rows and owns no spool feed anymore. */
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

  it('keeps a slim all=true feed for the stats cards (no k_profiles fat, both tabs)', async () => {
    render(<InventoryPageRouter />);
    await waitFor(() =>
      expect(listRequests.some((u) => u.searchParams.get('all') === 'true')).toBe(true)
    );
    const statsFeed = listRequests.find((u) => u.searchParams.get('all') === 'true')!;
    // Spans BOTH tabs (totalConsumed counts archived history) — deliberately
    // no archived param here, and never the k_profiles opt-in.
    expect(statsFeed.searchParams.has('archived')).toBe(false);
    expect(statsFeed.searchParams.has('include_k_profiles')).toBe(false);
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

  it('a plain inventory visit fires exactly ONE all=true fetch (the stats feed) — the forecast endpoints stay quiet', async () => {
    render(<InventoryPageRouter />);
    // Settle: rows on screen and the stats feed answered. A leaked panel
    // query would show up as a GET /inventory/forecast — mount-time queries
    // fire in the same tick as the page's own, so by the time the list has
    // settled it would already be in the count.
    await waitFor(() => expect(screen.getAllByLabelText('Select this spool').length).toBe(2));
    // ⚠️ T5 flips this to 0 — the stats feed becomes GET /inventory/stats and
    // all-slim is DELETED. Keeping this green at 1 by leaving all-slim alive
    // is the exact outcome T5 exists to prevent: change the expectation, not
    // the code under it.
    await waitFor(() => expect(fullSetRequests().length).toBe(1));
    expect(fullSetRequests().length).toBe(1);
    expect(forecastRequests.length).toBe(0);
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
    // …the panel never rendered (its SKU rows join brand+material+colour into
    // one label; the table keeps them in separate cells)…
    expect(screen.queryByText('eSun PLA Blue')).toBeNull();
    // …and the panel's feed never fired: since task 4 that feed is
    // GET /inventory/forecast, and a mounted panel would have called it.
    // The page-level flat list + all-slim stats feed each fire ONCE while
    // the spoolman settings are still in flight (spoolmanMode reads false
    // for that first tick — a pre-existing cold-load flicker, out of task-6
    // scope). If a later cycle gates those page-level queries on
    // spoolmanModeReady, these counts drop to 0 — lower is better here,
    // only MORE is a regression.
    expect(forecastRequests.length).toBe(0);
    // ⚠️ T5 flips this to 0 — the stats feed becomes GET /inventory/stats and
    // all-slim is DELETED. The ≤1 here is the cold-load flicker allowance
    // described above, not a licence to keep one all=true fetch alive.
    expect(fullSetRequests().length).toBeLessThanOrEqual(1);
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
        if (!url.searchParams.has('page')) return HttpResponse.json(SPOOLS);
        listRequests.push(url);
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
