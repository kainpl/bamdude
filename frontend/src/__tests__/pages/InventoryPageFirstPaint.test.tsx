/**
 * What the inventory page paints, and when.
 *
 * Three couplings, all of them about the page's slower half holding its faster
 * half hostage:
 *
 * - the farm summary is its own query over farm-wide data and must not wait for
 *   a page of spool rows;
 * - a cold list keeps the table's shape instead of collapsing the whole thing
 *   to a centred spinner — which is also the one case that genuinely still
 *   blanks, the first flip of "Group similar" (two observers, and a
 *   placeholder only ever sees its own observer's last result);
 * - a refetch behind that placeholder is visible, because otherwise stale rows
 *   read as current under a filter bar that has already changed.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor, fireEvent } from '@testing-library/react';
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

const DEFAULT_SPOOLS = [
  { ...baseSpool, id: 1, material: 'PLA', brand: 'eSun' },
  { ...baseSpool, id: 2, material: 'PETG', brand: 'SUNLU' },
];
let spools = DEFAULT_SPOOLS;

// The summary card counts ACTIVE spools; total_spools is deliberately a
// different number here so a test cannot pass by matching the wrong one.
const STATS = {
  total_spools: 412,
  active_spools: 400,
  total_weight_g: 380_000,
  total_consumed_g: 91_000,
  by_material: [{ material: 'PLA', count: 200, remaining_g: 190_000 }],
  low_stock_count: 7,
};

const SETTINGS = {
  currency: 'USD',
  language: 'en',
  date_format: 'system',
  time_format: 'system',
  low_stock_threshold: 20.0,
};

/** A list endpoint whose answers this test decides, one call at a time. */
let listGate: ((value: unknown) => void) | null = null;
let holdList = false;

function pagedPayload(groupSimilar: boolean) {
  if (groupSimilar) {
    return {
      items: spools.map((s) => ({
        material: s.material,
        subtype: '',
        brand: s.brand ?? '',
        color_name: s.color_name ?? '',
        rgba: s.rgba ?? '',
        label_weight: s.label_weight,
        lot: null,
        group_count: 1,
        ids: [s.id],
        remaining_total: Math.max(0, s.label_weight - s.weight_used),
        weight_used_total: s.weight_used,
        representative: { ...s, k_profile_count: 0, k_profiles: null },
      })),
      meta: { total: spools.length, current_page: 1, per_page: 24, last_page: 1 },
    };
  }
  return {
    items: spools.map((s) => ({ ...s, k_profile_count: 0, k_profiles: null })),
    meta: { total: spools.length, current_page: 1, per_page: 24, last_page: 1 },
  };
}

function setupHandlers({ statsPending = false }: { statsPending?: boolean } = {}) {
  server.use(
    http.get('/api/v1/inventory/stats', async () => {
      if (statsPending) await new Promise(() => {});
      return HttpResponse.json(STATS);
    }),
    http.get('/api/v1/settings/', () => HttpResponse.json(SETTINGS)),
    http.get('/api/v1/settings/spoolman', () =>
      HttpResponse.json({ spoolman_enabled: 'false', spoolman_url: '' })
    ),
    http.get('/api/v1/inventory/spools/facets', () =>
      HttpResponse.json({
        materials: ['PLA', 'PETG'],
        brands: ['eSun', 'SUNLU'],
        categories: [],
        catalog_ids: [],
        colors: [{ color_name: 'Blue', rgba: '0000FFFF' }],
      })
    ),
    http.get('/api/v1/inventory/spools/ids', () =>
      HttpResponse.json({ ids: spools.map((s) => s.id) })
    ),
    http.get('/api/v1/inventory/spools', async ({ request }) => {
      const url = new URL(request.url);
      if (!url.searchParams.has('page')) return HttpResponse.json(spools);
      if (holdList) {
        await new Promise((resolve) => {
          listGate = resolve;
        });
      }
      return HttpResponse.json(pagedPayload(url.searchParams.get('group_similar') === 'true'));
    }),
    http.get('/api/v1/inventory/assignments', () => HttpResponse.json([])),
    http.get('/api/v1/inventory/catalog', () => HttpResponse.json([])),
    http.get('/api/v1/inventory/locations', () => HttpResponse.json([])),
  );
}

describe('InventoryPage first paint', () => {
  beforeEach(() => {
    listGate = null;
    holdList = false;
    spools = DEFAULT_SPOOLS;
    localStorage.clear();
  });

  it('shows the farm summary while the first page of spools is still loading', async () => {
    holdList = true;
    setupHandlers();
    render(<InventoryPageRouter />);

    // The summary's own query answered; the list has not.
    expect(await screen.findByText(/400/)).toBeInTheDocument();
    expect(screen.getByTestId('spool-table-skeleton')).toBeInTheDocument();
  });

  it('draws a skeleton summary while the stats query itself is in flight', async () => {
    setupHandlers({ statsPending: true });
    render(<InventoryPageRouter />);

    expect(await screen.findByTestId('inventory-stats-skeleton')).toBeInTheDocument();
  });

  it('keeps the page shape on a cold list instead of one centred spinner', async () => {
    holdList = true;
    setupHandlers();
    render(<InventoryPageRouter />);

    expect(await screen.findByTestId('spool-table-skeleton')).toBeInTheDocument();
    expect(screen.queryByTestId('loading-block')).not.toBeInTheDocument();
  });

  it('does not collapse to a spinner when Group similar is first toggled', async () => {
    setupHandlers();
    render(<InventoryPageRouter />);
    await screen.findByText(/400/);

    // The grouped observer has no previous data of its own, so it genuinely is
    // loading — but the page must not lose its shape over it.
    holdList = true;
    const toggle = await screen.findByRole('button', { name: /group/i });
    fireEvent.click(toggle);

    await waitFor(() => {
      expect(screen.getByTestId('spool-table-skeleton')).toBeInTheDocument();
    });
    expect(screen.queryByTestId('loading-block')).not.toBeInTheDocument();
    // The summary is farm-wide and unaffected by grouping.
    expect(screen.getByText(/400/)).toBeInTheDocument();
  });

  it('shows that a filter change is still fetching', async () => {
    setupHandlers();
    render(<InventoryPageRouter />);
    await screen.findByText(/400/);

    holdList = true;
    fireEvent.change(await screen.findByPlaceholderText(/search/i), { target: { value: 'petg' } });

    const spinner = await screen.findByTestId('inventory-refetching', undefined, { timeout: 3000 });
    expect(spinner).toBeInTheDocument();

    holdList = false;
    listGate?.(null);
    await waitFor(() => {
      expect(screen.queryByTestId('inventory-refetching')).not.toBeInTheDocument();
    });
  });

  it('uses kilograms beside the remaining bar in table and card views', async () => {
    spools = [{ ...baseSpool, id: 1, material: 'PLA', brand: 'eSun', label_weight: 19_000 }];
    setupHandlers();
    render(<InventoryPageRouter />);

    await waitFor(() => {
      expect(screen.getAllByText('19.00kg').length).toBeGreaterThanOrEqual(2);
    });
    expect(screen.queryByText('19000g')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /cards/i }));
    await waitFor(() => {
      expect(screen.getAllByText('19.00kg').length).toBeGreaterThanOrEqual(2);
    });
    expect(screen.queryByText('19000g')).not.toBeInTheDocument();
  });
});
