/**
 * The farm-wide usage ledger, pinned at its API boundary (2026-09-01).
 *
 * The panel decides nothing about the list itself — the page, the order, the
 * filters, the search and the totals are all the server's answer — so what
 * these tests pin is the PARAMS it sends and the served fields it renders
 * verbatim. The load-bearing pins:
 *
 * - the table feeds from GET /inventory/usage with `page` always set: the
 *   unpaged form of that endpoint is the 5000-row download the forecast cycle
 *   deleted, and reaching it again from the browser is the one regression this
 *   whole view must never make;
 * - a persisted localStorage sort is SANITIZED to the server key set before the
 *   first request;
 * - a filter/search/sort change resets the page to 1 in the same render;
 * - the day a person picked reaches the wire as a UTC instant, with the end of
 *   the range EXCLUSIVE (the start of the day after);
 * - a row renders the spool identity and printer NAME the server sent with it,
 *   never a client-side lookup.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { screen, waitFor, fireEvent } from '@testing-library/react';
import { render } from '../utils';
import { SpoolUsageHistoryPanel } from '../../components/SpoolUsageHistoryPanel';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import type { SpoolUsageListItem, SpoolUsageSpoolRef } from '../../api/client';

const SORT_KEY = 'bamdude-usage-history-sort';

let usageRequests: URL[] = [];

function spoolRef(over: Partial<SpoolUsageSpoolRef> = {}): SpoolUsageSpoolRef {
  return {
    id: 7,
    material: 'PLA',
    subtype: null,
    brand: 'SUNLU',
    color_name: 'Black',
    rgba: '000000FF',
    slicer_filament_name: null,
    note: null,
    label_weight: 1000,
    weight_used: 250,
    cost_per_kg: null,
    purchase_date: null,
    filament_diameter: '1.75',
    lot: null,
    archived: false,
    ...over,
  };
}

function item(over: Partial<SpoolUsageListItem> = {}): SpoolUsageListItem {
  return {
    id: 1,
    spool_id: 7,
    created_at: '2026-08-20T10:00:00',
    weight_used: 42.5,
    percent_used: 4,
    status: 'completed',
    cost: 3.25,
    print_name: 'dragon_body.3mf',
    archive_id: 11,
    printer_id: 5,
    printer_name: 'Printer 5',
    printer_archived: false,
    spool: spoolRef(),
    ...over,
  };
}

function setupHandlers(
  items: SpoolUsageListItem[] = [item()],
  total = items.length,
  printers: { id: number; name: string | null; archived: boolean }[] = [
    { id: 5, name: 'Printer 5', archived: false },
  ],
) {
  usageRequests = [];
  server.use(
    http.get('/api/v1/inventory/usage/facets', () =>
      HttpResponse.json({
        statuses: ['completed', 'runout'],
        printers,
        materials: ['PLA', 'PETG'],
        brands: ['SUNLU'],
      }),
    ),
    http.get('/api/v1/inventory/usage', ({ request }) => {
      const url = new URL(request.url);
      usageRequests.push(url);
      return HttpResponse.json({
        items,
        meta: { total, current_page: 1, per_page: 50, last_page: Math.max(1, Math.ceil(total / 50)) },
        totals: { weight_used: 42.5, cost: 3.25 },
      });
    }),
    http.get('/api/v1/settings/', () => HttpResponse.json({ language: 'en', currency: 'USD' })),
  );
}

describe('SpoolUsageHistoryPanel — a renderer of a server-driven ledger', () => {
  beforeEach(() => {
    localStorage.clear();
    localStorage.setItem('auth_token', 'test-admin-token');
    setupHandlers();
  });

  it('always asks for a PAGE — the unpaged form of this endpoint is the download that was deleted', async () => {
    render(<SpoolUsageHistoryPanel />);
    await waitFor(() => expect(usageRequests.length).toBeGreaterThan(0));

    for (const url of usageRequests) {
      expect(url.searchParams.get('page')).toBeTruthy();
      expect(url.searchParams.get('all')).toBeNull();
    }
    expect(usageRequests[0].searchParams.get('sort_by')).toBe('created_at_desc');
    expect(usageRequests[0].searchParams.get('per_page')).toBe('50');
  });

  it('renders the spool identity and the printer NAME the row arrived with', async () => {
    render(<SpoolUsageHistoryPanel />);

    // Composed by the SAME display-name template the table and cards use.
    expect(await screen.findByText('SUNLU PLA Black')).toBeInTheDocument();
    // The dropdown option carries the same name, so this asserts on the CELL.
    expect(screen.getByRole('cell', { name: 'Printer 5' })).toBeInTheDocument();
    expect(screen.getByText('dragon_body.3mf')).toBeInTheDocument();
    expect(screen.getByText('42.5g')).toBeInTheDocument();
  });

  it('labels a retired printer generically, in the row and in its own dropdown', async () => {
    // Its name may have been reused since it burned this filament — the shared
    // `printerLabel` rule, the same one stats and archives follow.
    setupHandlers([item({ printer_id: 9, printer_name: 'Old P1S', printer_archived: true })], 1, [
      { id: 9, name: 'Old P1S', archived: true },
    ]);
    render(<SpoolUsageHistoryPanel />);

    expect(await screen.findByRole('cell', { name: 'Printer 9 (Archived)' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'Printer 9 (Archived)' })).toBeInTheDocument();
    expect(screen.queryByText('Old P1S')).not.toBeInTheDocument();
  });

  it('marks a row whose spool has been retired instead of hiding it', async () => {
    setupHandlers([item({ spool: spoolRef({ archived: true }) })]);
    render(<SpoolUsageHistoryPanel />);

    expect(await screen.findByText('Archived')).toBeInTheDocument();
  });

  it('names a row whose spool is gone rather than rendering it blank', async () => {
    setupHandlers([item({ spool: null })]);
    render(<SpoolUsageHistoryPanel />);

    expect(await screen.findByText('Deleted spool')).toBeInTheDocument();
  });

  it('sanitizes a persisted sort to the server key set before the first request', async () => {
    localStorage.setItem(SORT_KEY, JSON.stringify({ key: 'whatever_the_user_edited', dir: 'asc' }));
    render(<SpoolUsageHistoryPanel />);

    await waitFor(() => expect(usageRequests.length).toBeGreaterThan(0));
    expect(usageRequests[0].searchParams.get('sort_by')).toBe('created_at_desc');
  });

  it('sends a status filter as repeated params and resets the page in the same render', async () => {
    setupHandlers([item()], 300);
    render(<SpoolUsageHistoryPanel />);
    await waitFor(() => expect(usageRequests.length).toBeGreaterThan(0));

    fireEvent.click(await screen.findByRole('button', { name: 'Runout close-out' }));

    await waitFor(() => {
      const last = usageRequests[usageRequests.length - 1];
      expect(last.searchParams.getAll('status')).toEqual(['runout']);
      expect(last.searchParams.get('page')).toBe('1');
    });
  });

  it('starts with the spool-state filters OFF and sends them only once picked', async () => {
    // ⚠️ "All" is the default and has no equivalent on the spool table, where a
    // tab is always active OR archived. Retiring or unloading a reel does not
    // un-burn what it printed, so nothing here is hidden unasked.
    render(<SpoolUsageHistoryPanel />);
    await waitFor(() => expect(usageRequests.length).toBeGreaterThan(0));
    expect(usageRequests[0].searchParams.get('archived')).toBeNull();
    expect(usageRequests[0].searchParams.get('assigned')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Archived' }));
    await waitFor(() =>
      expect(usageRequests[usageRequests.length - 1].searchParams.get('archived')).toBe('archived'),
    );

    fireEvent.click(screen.getByRole('button', { name: 'On the shelf' }));
    await waitFor(() =>
      expect(usageRequests[usageRequests.length - 1].searchParams.get('assigned')).toBe('unassigned'),
    );
  });

  it('turns the picked days into a UTC window whose end is exclusive', async () => {
    render(<SpoolUsageHistoryPanel />);
    await waitFor(() => expect(usageRequests.length).toBeGreaterThan(0));

    fireEvent.change(screen.getByLabelText('From'), { target: { value: '2026-08-01' } });
    fireEvent.change(screen.getByLabelText('To'), { target: { value: '2026-08-31' } });

    await waitFor(() => {
      const last = usageRequests[usageRequests.length - 1];
      expect(last.searchParams.get('date_from')).toBe(new Date('2026-08-01T00:00:00').toISOString());
      // The end of the 31st is the start of September — exclusive, so a print
      // finished at 23:59 on the last day is still inside the window.
      expect(last.searchParams.get('date_to')).toBe(new Date('2026-09-01T00:00:00').toISOString());
    });
  });

  it('takes the search from the PAGE box and sends it to the server', async () => {
    // The panel has no box of its own — one search input lives on the page,
    // whichever view is open. What arrives here is that text, debounced.
    render(<SpoolUsageHistoryPanel search="dragon" />);

    await waitFor(() => {
      const last = usageRequests[usageRequests.length - 1];
      expect(last.searchParams.get('q')).toBe('dragon');
    });
  });

  it('shows the totals for the whole filter, not for the rows on screen', async () => {
    render(<SpoolUsageHistoryPanel />);
    expect(await screen.findByText(/Total: 42\.5 g/)).toBeInTheDocument();
  });

  it('names a spool with the display-name template the rest of the page uses', async () => {
    server.use(
      http.get('/api/v1/settings/', () =>
        HttpResponse.json({ language: 'en', currency: 'USD', spool_display_template: '{material} · {id}' }),
      ),
    );
    render(<SpoolUsageHistoryPanel />);

    expect(await screen.findByText('PLA · 7')).toBeInTheDocument();
    expect(screen.queryByText('SUNLU PLA Black')).not.toBeInTheDocument();
  });

  it('opens the spool behind a row through the callback, by id', async () => {
    const onOpenSpool = vi.fn();
    render(<SpoolUsageHistoryPanel onOpenSpool={onOpenSpool} />);

    fireEvent.click(await screen.findByRole('button', { name: 'SUNLU PLA Black' }));
    expect(onOpenSpool).toHaveBeenCalledWith(7);
  });
});
