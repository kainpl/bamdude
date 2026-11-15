/**
 * Usage history in Spoolman mode (upstream 1d86cff5a, #3407).
 *
 * The History view and the spool dialog's usage list are the SAME components in
 * both inventories; what changes is the door they knock on. The pins:
 *
 * - in Spoolman mode every request goes to the Spoolman spools' own feed and
 *   NONE to the built-in one — the two inventories number their spools
 *   independently, and a Spoolman spool must never show a built-in spool's
 *   prints (upstream 704919339);
 * - the feed is still asked for a PAGE, never whole;
 * - there is no "archived spool" filter: that is Spoolman's to say;
 * - removing a record says the spool's weight in Spoolman is not changed, and
 *   goes to the Spoolman door.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor, fireEvent } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { SpoolUsageHistoryPanel } from '../../components/SpoolUsageHistoryPanel';
import { SpoolUsageHistory } from '../../components/SpoolUsageHistory';

let spoolmanRequests: URL[] = [];
let builtInRequests: string[] = [];
let deleted: string[] = [];
let previousRows = 0;

const ROW = {
  id: 3,
  spool_id: 41,
  created_at: '2026-11-10T10:00:00',
  weight_used: 18.5,
  percent_used: 2,
  status: 'completed',
  cost: 0.74,
  print_name: 'bracket.3mf',
  archive_id: 9,
  printer_id: null,
  printer_name: null,
  printer_archived: false,
  spool: {
    id: 41,
    material: 'PETG',
    subtype: 'HF',
    brand: 'Bambu Lab',
    color_name: 'Orange',
    rgba: 'FF8800FF',
    slicer_filament_name: 'PETG HF',
    note: null,
    label_weight: 1000,
    weight_used: null,
    cost_per_kg: null,
    purchase_date: null,
    filament_diameter: null,
    lot: null,
    archived: false,
  },
};

function setupHandlers() {
  spoolmanRequests = [];
  builtInRequests = [];
  deleted = [];
  server.use(
    http.get('/api/v1/inventory/usage/facets', () => {
      builtInRequests.push('facets');
      return HttpResponse.json({ statuses: [], printers: [], materials: [], brands: [] });
    }),
    http.get('/api/v1/inventory/usage', () => {
      builtInRequests.push('usage');
      return HttpResponse.json({ items: [], meta: { total: 0, current_page: 1, per_page: 50, last_page: 1 }, totals: { weight_used: 0, cost: null } });
    }),
    http.get('/api/v1/inventory/spools/:id/usage', () => {
      builtInRequests.push('spool-usage');
      return HttpResponse.json([]);
    }),
    http.get('/api/v1/spoolman/inventory/usage/facets', () =>
      HttpResponse.json({ statuses: ['completed'], printers: [], materials: ['PETG'], brands: ['Bambu Lab'] }),
    ),
    http.get('/api/v1/spoolman/inventory/usage/source', () =>
      HttpResponse.json({
        has_source: true,
        rows: 1,
        previous_url: previousRows ? 'http://old-host:7912' : null,
        previous_rows: previousRows,
      }),
    ),
    http.post('/api/v1/spoolman/inventory/usage/source/keep-previous', () => {
      previousRows = 0;
      return HttpResponse.json({ has_source: true, rows: 4, previous_url: null, previous_rows: 0 });
    }),
    http.get('/api/v1/spoolman/inventory/usage', ({ request }) => {
      spoolmanRequests.push(new URL(request.url));
      return HttpResponse.json({
        items: [ROW],
        meta: { total: 1, current_page: 1, per_page: 50, last_page: 1 },
        totals: { weight_used: 18.5, cost: 0.74 },
      });
    }),
    http.get('/api/v1/spoolman/inventory/spools/:id/usage', () =>
      HttpResponse.json([
        { id: 3, spool_id: 41, printer_id: null, print_name: 'bracket.3mf', weight_used: 18.5, percent_used: 2, status: 'completed', cost: 0.74, created_at: '2026-11-10T10:00:00' },
      ]),
    ),
    http.delete('/api/v1/spoolman/inventory/spools/:id/usage/:usageId', ({ params }) => {
      deleted.push(`row ${params.id}/${params.usageId}`);
      return HttpResponse.json({ status: 'deleted' });
    }),
    http.delete('/api/v1/spoolman/inventory/spools/:id/usage', ({ params }) => {
      deleted.push(`all ${params.id}`);
      return HttpResponse.json({ status: 'cleared', deleted: 1 });
    }),
    http.get('/api/v1/settings/', () => HttpResponse.json({ language: 'en', currency: 'USD' })),
  );
}

describe('usage history in Spoolman mode', () => {
  beforeEach(() => {
    localStorage.clear();
    localStorage.setItem('auth_token', 'test-admin-token');
    previousRows = 0;
    setupHandlers();
  });

  it('the History view reads the Spoolman feed, paged, and never the built-in one', async () => {
    render(<SpoolUsageHistoryPanel spoolmanMode />);

    expect(await screen.findByText('bracket.3mf')).toBeInTheDocument();
    expect(spoolmanRequests.length).toBeGreaterThan(0);
    for (const url of spoolmanRequests) {
      expect(url.searchParams.get('page')).toBeTruthy();
      expect(url.searchParams.get('archived')).toBeNull();
    }
    expect(builtInRequests).toEqual([]);
  });

  it('names the spool by the snapshot the row carries, through the naming template', async () => {
    render(<SpoolUsageHistoryPanel spoolmanMode />);
    expect(await screen.findByText('Bambu Lab PETG HF Orange')).toBeInTheDocument();
  });

  it('offers no archived-spool filter — that is Spoolman\'s to say', async () => {
    render(<SpoolUsageHistoryPanel spoolmanMode />);
    await screen.findByText('bracket.3mf');
    expect(screen.queryByRole('button', { name: 'Archived' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'In Printer' })).toBeInTheDocument();
  });

  it('removing a record goes to the Spoolman door and says the weight there is not changed', async () => {
    render(<SpoolUsageHistoryPanel spoolmanMode />);
    await screen.findByText('bracket.3mf');

    fireEvent.click(screen.getByRole('button', { name: /weight in Spoolman is not changed/ }));
    await waitFor(() => expect(deleted).toEqual(['row 41/3']));
  });

  it('offers to bring back the history of a previous Spoolman address', async () => {
    previousRows = 4;
    render(<SpoolUsageHistoryPanel spoolmanMode />);

    expect(await screen.findByText(/4 usage records made for http:\/\/old-host:7912/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Same database — keep the history' }));
    await waitFor(() => expect(screen.queryByText(/usage records made for/)).not.toBeInTheDocument());
  });

  it('the built-in view still reads only its own feed', async () => {
    render(<SpoolUsageHistoryPanel />);
    await waitFor(() => expect(builtInRequests).toContain('usage'));
    expect(spoolmanRequests).toEqual([]);
  });

  it('the spool dialog lists a Spoolman spool\'s prints from its own door and clears them there', async () => {
    render(<SpoolUsageHistory spoolId={41} spoolmanMode />);

    expect(await screen.findByText('bracket.3mf')).toBeInTheDocument();
    expect(builtInRequests).toEqual([]);

    fireEvent.click(screen.getByRole('button', { name: /Clear/ }));
    await waitFor(() => expect(deleted).toEqual(['all 41']));
  });
});
