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

function setupHandlers() {
  listRequests = [];
  idsRequests = [];
  facetsRequests = [];
  server.use(
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

/** The list request driving the visible page (the all=true ones are the
 *  separate stats feed and — on the Forecast tab only — the panel's own
 *  feed, task 5). */
const pageRequests = () => listRequests.filter((u) => u.searchParams.get('all') !== 'true');
/** The full-set slim fetches: the page-level stats feed plus, once the
 *  Forecast tab opens, ForecastPanel's own feed. Same URL shape by design —
 *  the tests below pin them apart by COUNT. */
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

describe('InventoryPage — the Forecast tab feeds itself (task 5)', () => {
  beforeEach(() => {
    localStorage.clear();
    setupHandlers();
    // The panel's other feeds — separate endpoints, unchanged by task 5
    // (spec §3.5); answered empty so opening the tab settles cleanly.
    server.use(
      http.get('/api/v1/inventory/usage', () => HttpResponse.json([])),
      http.get('/api/v1/inventory/sku-settings', () => HttpResponse.json([])),
      http.get('/api/v1/inventory/shopping-list', () => HttpResponse.json([])),
    );
  });

  it('a plain inventory visit fires exactly ONE all=true fetch (the stats feed) — the forecast feed stays quiet', async () => {
    render(<InventoryPageRouter />);
    // Settle: rows on screen and the stats feed answered. A leaked forecast
    // query would be a SECOND identical all=true request — mount-time
    // queries fire in the same tick as the page's own, so by the time the
    // list has settled it would already be in the count.
    await waitFor(() => expect(screen.getAllByLabelText('Select this spool').length).toBe(2));
    await waitFor(() => expect(fullSetRequests().length).toBe(1));
    expect(fullSetRequests().length).toBe(1);
  });

  it('opening the Forecast tab fires the panel\'s own full-set feed and renders the SKU table off it', async () => {
    render(<InventoryPageRouter />);
    await waitFor(() => expect(screen.getAllByLabelText('Select this spool').length).toBe(2));
    const before = fullSetRequests().length;

    const forecastTab = screen.getByRole('button', { name: /^Forecast$/ });
    await waitFor(() => expect(forecastTab).toBeEnabled());
    fireEvent.click(forecastTab);

    await waitFor(() => expect(fullSetRequests().length).toBe(before + 1));
    // Same span as the old getSpools(true) prop: both tabs, slim rows.
    const feed = fullSetRequests()[fullSetRequests().length - 1];
    expect(feed.searchParams.has('archived')).toBe(false);
    expect(feed.searchParams.has('include_k_profiles')).toBe(false);

    // And the panel renders its SKU rows off that feed, not off a prop.
    await waitFor(() => expect(screen.getByText('eSun PLA Blue')).toBeInTheDocument());
    expect(screen.getByText('SUNLU PETG Blue')).toBeInTheDocument();
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
