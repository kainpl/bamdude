/**
 * Restore on an archived spool inside an expanded group row of the table (upstream 23d04c864).
 * The group row passed a no-op to its members, so the button did nothing.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor, fireEvent, within } from '@testing-library/react';
import { render } from '../utils';
import InventoryPageRouter from '../../pages/InventoryPage';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

const archived = (id: number) => ({
  id,
  material: 'PETG',
  subtype: null,
  brand: 'eSun',
  color_name: 'Blue',
  rgba: '0000FFFF',
  label_weight: 1000,
  core_weight: 250,
  weight_used: 0,
  weight_used_baseline: 0,
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
  archived_at: '2026-04-01T00:00:00Z',
  created_at: '2025-01-02T00:00:00Z',
  updated_at: '2025-01-02T00:00:00Z',
  k_profiles: [],
  k_profile_count: 0,
  cost_per_kg: null,
  last_scale_weight: null,
  last_weighed_at: null,
});

const MEMBERS = [archived(2), archived(3)];

describe('InventoryPage — Restore inside a group row', () => {
  const restored: number[] = [];

  beforeEach(() => {
    restored.length = 0;
    localStorage.clear();
    localStorage.setItem('bamdude-inventory-group', 'true');
    server.use(
      http.get('/api/v1/inventory/stats', () =>
        HttpResponse.json({
          total_inventory: 0,
          total_weight_g: 0,
          total_consumed_g: 0,
          by_material: [],
          low_stock_count: 0,
        }),
      ),
      http.get('/api/v1/inventory/forecast', () =>
        HttpResponse.json({
          items: [],
          meta: { total: 0, current_page: 1, per_page: 50, last_page: 1 },
          alert_count: 0,
          unmatched_reserved: [],
          global_lead_time_days: 0,
        }),
      ),
      http.get('/api/v1/settings/spoolman', () => HttpResponse.json({ spoolman_enabled: 'false', spoolman_url: '' })),
      http.get('/api/v1/inventory/spools/facets', () =>
        HttpResponse.json({ materials: ['PETG'], brands: ['eSun'], categories: [], catalog_ids: [], colors: [] }),
      ),
      http.get('/api/v1/inventory/spools/ids', () => HttpResponse.json({ ids: [2, 3] })),
      http.get('/api/v1/inventory/spools/:id', ({ params }) =>
        HttpResponse.json(MEMBERS.find((s) => s.id === Number(params.id))),
      ),
      http.get('/api/v1/inventory/spools', ({ request }) => {
        const url = new URL(request.url);
        if (!url.searchParams.has('page')) return HttpResponse.json(MEMBERS);
        if (url.searchParams.get('group_similar') === 'true') {
          return HttpResponse.json({
            items: [
              {
                material: 'PETG',
                subtype: '',
                brand: 'eSun',
                color_name: 'Blue',
                rgba: '0000FFFF',
                label_weight: 1000,
                lot: null,
                group_count: 2,
                ids: [2, 3],
                remaining_total: 2000,
                weight_used_total: 0,
                representative: { ...MEMBERS[0], k_profiles: null },
              },
            ],
            meta: { total: 1, current_page: 1, per_page: 24, last_page: 1 },
          });
        }
        return HttpResponse.json({ items: MEMBERS, meta: { total: 2, current_page: 1, per_page: 24, last_page: 1 } });
      }),
      http.post('/api/v1/inventory/spools/:id/restore', ({ params }) => {
        restored.push(Number(params.id));
        return HttpResponse.json({ ...archived(Number(params.id)), archived_at: null });
      }),
      http.get('/api/v1/inventory/assignments', () => HttpResponse.json([])),
      http.get('/api/v1/inventory/catalog', () => HttpResponse.json([])),
      http.get('/api/v1/inventory/locations', () => HttpResponse.json([])),
    );
  });

  it('restores the member it was pressed on', async () => {
    render(<InventoryPageRouter />);

    fireEvent.click(await screen.findByRole('button', { name: /^Archived/ }));
    // The group's header row (two archived spools of one kind) expands on a click.
    fireEvent.click(await screen.findByText('#2, #3'));

    const rows = await waitFor(() => {
      const found = screen.getAllByTitle('Restore');
      expect(found).toHaveLength(2);
      return found;
    });
    fireEvent.click(rows[1]);

    await waitFor(() => expect(restored).toEqual([3]));
    expect(within(document.body).queryByText('Restore failed')).not.toBeInTheDocument();
  });
});
