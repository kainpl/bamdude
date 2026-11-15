/**
 * The Forecast tab in Spoolman mode (upstream 1d86cff5a, #3407; guard of 704919339).
 *
 * The forecast is the same server answer in both inventories — the engine picks
 * Spoolman's spools and their own usage rows when Spoolman is the inventory.
 * What these tests pin is the panel's side of that:
 *
 * - its own spool lookups (filter options, a SKU's individual spools) go to the
 *   Spoolman list and NEVER to the built-in spool table or usage feed — the two
 *   inventories number their spools independently;
 * - a Spoolman that does not answer is said out loud, never drawn as an empty
 *   shelf;
 * - reels marked received are created in Spoolman, not in the built-in table.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor, fireEvent, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { ForecastPanel } from '../../components/ForecastPanel';

let builtInRequests: string[] = [];
let spoolmanListRequests = 0;
let spoolmanBulkCreates: { spool: Record<string, unknown>; quantity: number }[] = [];
let forecastDown = false;

const ROW = {
  material: 'PETG',
  subtype: 'HF',
  brand: 'Bambu Lab',
  color_name: 'Orange',
  rgba: 'FF8800FF',
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
  projected_empty_date: '2026-12-15',
  days_until_rop: 140,
  reorder_trigger_date: '2026-12-10',
  stock_break_alert: false,
  reorder_alert: false,
  alerts_snoozed: false,
  spool_ids: [41, 42],
  reserved_g: 0,
  free_g: 1500,
  over_committed: false,
};

const spoolmanSpool = (id: number, over: Record<string, unknown> = {}) => ({
  id,
  material: 'PETG',
  subtype: 'HF',
  brand: 'Bambu Lab',
  color_name: 'Orange',
  rgba: 'FF8800FF',
  label_weight: 1000,
  weight_used: 250,
  weight_used_baseline: 0,
  archived_at: null,
  ...over,
});

function setupHandlers(shoppingList: unknown[] = []) {
  builtInRequests = [];
  spoolmanListRequests = 0;
  spoolmanBulkCreates = [];
  server.use(
    http.get('/api/v1/inventory/forecast', () => {
      if (forecastDown) {
        return HttpResponse.json(
          { detail: { error: 'spoolman_unreachable', message: 'Spoolman server is not reachable' } },
          { status: 503 },
        );
      }
      return HttpResponse.json({
        items: [ROW],
        meta: { total: 1, current_page: 1, per_page: 50, last_page: 1 },
        alert_count: 0,
        global_lead_time_days: 3,
        unmatched_reserved: [],
        reserved_incomplete: false,
      });
    }),
    http.get('/api/v1/inventory/forecast/chart', () => HttpResponse.json({ series: [] })),
    http.get('/api/v1/inventory/forecast/logistics', () => HttpResponse.json([])),
    http.get('/api/v1/inventory/sku-settings', () => HttpResponse.json([])),
    http.get('/api/v1/inventory/shopping-list', () => HttpResponse.json(shoppingList)),
    http.patch('/api/v1/inventory/shopping-list/:id', () => HttpResponse.json({})),
    http.delete('/api/v1/inventory/shopping-list/:id', () => HttpResponse.json({})),
    // The built-in inventory's doors: none may be knocked on in Spoolman mode.
    http.get('/api/v1/inventory/spools/facets', () => {
      builtInRequests.push('facets');
      return HttpResponse.json({ materials: ['ABS'], brands: ['eSun'], categories: [], catalog_ids: [], colors: [] });
    }),
    http.get('/api/v1/inventory/spools', () => {
      builtInRequests.push('spools');
      return HttpResponse.json({ items: [], meta: { total: 0, current_page: 1, per_page: 1, last_page: 1 } });
    }),
    http.post('/api/v1/inventory/spools/bulk', () => {
      builtInRequests.push('bulk-create');
      return HttpResponse.json([]);
    }),
    http.get('/api/v1/inventory/usage', () => {
      builtInRequests.push('usage');
      return HttpResponse.json([]);
    }),
    http.get('/api/v1/spoolman/inventory/spools', () => {
      spoolmanListRequests += 1;
      return HttpResponse.json([
        spoolmanSpool(41),
        spoolmanSpool(42),
        spoolmanSpool(77, { material: 'PLA', brand: 'Sunlu', subtype: 'Matte' }),
      ]);
    }),
    http.post('/api/v1/spoolman/inventory/spools/bulk', async ({ request }) => {
      spoolmanBulkCreates.push((await request.json()) as { spool: Record<string, unknown>; quantity: number });
      return HttpResponse.json([spoolmanSpool(90), spoolmanSpool(91)]);
    }),
    http.get('/api/v1/settings/', () => HttpResponse.json({ language: 'en' })),
  );
}

describe('ForecastPanel in Spoolman mode', () => {
  beforeEach(() => {
    localStorage.clear();
    localStorage.setItem('auth_token', 'test-admin-token');
    forecastDown = false;
    setupHandlers();
  });

  it('takes its filter options from the Spoolman spools, never from the built-in table', async () => {
    render(<ForecastPanel spoolmanMode />);
    await waitFor(() => expect(screen.getByText('Bambu Lab PETG HF Orange')).toBeInTheDocument());

    await waitFor(() => expect(screen.getByRole('option', { name: 'Sunlu' })).toBeInTheDocument());
    expect(screen.getByRole('option', { name: 'PETG' })).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'eSun' })).toBeNull();
    expect(builtInRequests).toEqual([]);
  });

  it('lists a SKU\'s individual spools from the Spoolman list, by the served spool ids', async () => {
    render(<ForecastPanel spoolmanMode />);
    fireEvent.click(await screen.findByText('Bambu Lab PETG HF Orange'));

    await waitFor(() => expect(screen.getByText('#41')).toBeInTheDocument());
    expect(screen.getByText('#42')).toBeInTheDocument();
    expect(screen.queryByText('#77')).toBeNull();
    expect(spoolmanListRequests).toBeGreaterThan(0);
    expect(builtInRequests).toEqual([]);
  });

  it('says Spoolman is not answering instead of drawing an empty shelf', async () => {
    forecastDown = true;
    render(<ForecastPanel spoolmanMode />);

    const failed = await screen.findByTestId('forecast-load-failed', undefined, { timeout: 5000 });
    expect(within(failed).getByText(/Spoolman is not answering/)).toBeInTheDocument();
    expect(screen.queryByText(/No spools/i)).toBeNull();

    forecastDown = false;
    fireEvent.click(within(failed).getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('Bambu Lab PETG HF Orange')).toBeInTheDocument();
  });

  it('creates received reels in Spoolman, not in the built-in table', async () => {
    setupHandlers([
      {
        id: 7,
        material: 'PETG',
        subtype: 'HF',
        brand: 'Bambu Lab',
        color_name: 'Orange',
        quantity_spools: 2,
        status: 'purchased',
        note: null,
        created_at: '2026-11-01T00:00:00',
        updated_at: '2026-11-01T00:00:00',
      },
    ]);
    render(<ForecastPanel spoolmanMode />);
    await screen.findByText('Bambu Lab PETG HF Orange');

    fireEvent.click(await screen.findByText('Shopping List'));
    const received = await screen.findByTitle('Mark as received - adds spools to Stock inventory');
    await waitFor(() => expect(received).toBeEnabled());
    fireEvent.click(received);

    await waitFor(() => expect(spoolmanBulkCreates.length).toBe(1));
    expect(spoolmanBulkCreates[0].quantity).toBe(2);
    expect(spoolmanBulkCreates[0].spool.material).toBe('PETG');
    expect(spoolmanBulkCreates[0].spool.label_weight).toBe(1000);
    expect(builtInRequests).toEqual([]);
  });

  it('the built-in panel still reads only the built-in doors', async () => {
    render(<ForecastPanel />);
    await waitFor(() => expect(builtInRequests).toContain('facets'));
    expect(spoolmanListRequests).toBe(0);
  });
});
