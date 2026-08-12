/**
 * The list view's own controls: paging under the rows, sorting on the headers.
 *
 * Where they SIT is most of the change. Paging worked before — the range under
 * the title, the arrows above the filters, the page-size selector inside the
 * filter panel: three places for one question, so changing the size meant
 * looking somewhere else to see what it did. None of that is visible to a test
 * that only asks "does it render", which is why these ask which container each
 * control is in and what comes before it.
 *
 * Sorting moved the other way — off a dropdown and onto the column headers, so
 * the assertions are on what the server is actually asked for.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { ArchivesPage } from '../../pages/ArchivesPage';

const archive = (id: number, name: string) => ({
  id,
  filename: `${name}.gcode.3mf`,
  print_name: name,
  printer_id: 1,
  printer_name: 'X1 Carbon',
  print_time_seconds: 3600,
  filament_used_grams: 10,
  status: 'completed',
  started_at: '2024-01-01T10:00:00Z',
  completed_at: '2024-01-01T11:00:00Z',
  thumbnail_path: null,
  notes: null,
  rating: null,
  project_id: null,
  project_name: null,
  project_color: null,
  print_count: 1,
  tags: '',
  created_at: '2024-01-01T09:00:00Z',
  updated_at: '2024-01-01T11:00:00Z',
  has_f3d: false,
});

let lastQuery: URLSearchParams | null = null;

function mockArchives(meta: { current_page: number; per_page: number; total: number; last_page: number }) {
  server.use(
    http.get('/api/v1/archives/', ({ request }) => {
      lastQuery = new URL(request.url).searchParams;
      return HttpResponse.json({ data: [archive(1, 'Benchy'), archive(2, 'Bracket')], meta });
    }),
    http.get('/api/v1/archives/stats', () =>
      HttpResponse.json({
        total_archives: meta.total,
        total_print_time_seconds: 0,
        total_filament_grams: 0,
        prints_this_week: 0,
        prints_this_month: 0,
      }),
    ),
    http.get('/api/v1/printers/', () => HttpResponse.json([{ id: 1, name: 'X1 Carbon' }])),
    http.get('/api/v1/projects/', () => HttpResponse.json([])),
    http.get('/api/v1/archives/tags', () => HttpResponse.json([])),
  );
}

const bar = () => document.querySelector('[data-pagination]') as HTMLElement;

describe('archives pagination', () => {
  beforeEach(() => {
    lastQuery = null;
    localStorage.clear();
  });

  it('keeps the page size and the page controls in one container', async () => {
    mockArchives({ current_page: 2, per_page: 24, total: 96, last_page: 4 });
    render(<ArchivesPage />);
    await waitFor(() => expect(bar()).toBeTruthy());

    expect(within(bar()).getByRole('combobox', { name: /show/i })).toBeInTheDocument();
    expect(within(bar()).getByRole('button', { name: /next page/i })).toBeInTheDocument();
    expect(within(bar()).getByText('Showing 25-48 of 96 archives')).toBeInTheDocument();
  });

  it('puts that container after the rows, not above them', async () => {
    // The arrows used to sit above the filter panel, which is above the first
    // card. Asserting presence alone would have passed then too.
    mockArchives({ current_page: 1, per_page: 24, total: 96, last_page: 4 });
    render(<ArchivesPage />);
    await waitFor(() => expect(bar()).toBeTruthy());

    const firstCard = await screen.findByText('Benchy');
    expect(firstCard.compareDocumentPosition(bar()) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('asks the server for the new size and returns to the first page', async () => {
    mockArchives({ current_page: 3, per_page: 24, total: 96, last_page: 4 });
    render(<ArchivesPage />);
    await waitFor(() => expect(bar()).toBeTruthy());

    await userEvent.selectOptions(within(bar()).getByRole('combobox', { name: /show/i }), '48');

    // Staying on page 3 of a re-sized list lands somewhere the operator did not
    // ask for — and past the end when the list shrinks to fewer pages.
    await waitFor(() => {
      expect(lastQuery?.get('per_page')).toBe('48');
      expect(lastQuery?.get('page')).toBe('1');
    });
  });

  it('sorts by a column when its header is clicked, and flips on a second click', async () => {
    // The select that used to be the only way to sort is gone from the list
    // view; the headers are it. Both directions matter — a header that only
    // ever sorts one way is half a control.
    mockArchives({ current_page: 1, per_page: 24, total: 2, last_page: 1 });
    render(<ArchivesPage />);
    await screen.findByText('Benchy');
    await userEvent.click(screen.getByRole('button', { name: /list view/i }));

    await userEvent.click(await screen.findByRole('button', { name: /^Name/ }));
    await waitFor(() => expect(lastQuery?.get('sort_by')).toBe('name-asc'));

    await userEvent.click(screen.getByRole('button', { name: /^Name/ }));
    await waitFor(() => expect(lastQuery?.get('sort_by')).toBe('name-desc'));
  });

  it('opens a date or a size column at the useful end', async () => {
    // Not one rule for every column: a print history opened by date wants the
    // newest print, and by size the biggest file — only a name reads A→Z.
    mockArchives({ current_page: 1, per_page: 24, total: 2, last_page: 1 });
    render(<ArchivesPage />);
    await screen.findByText('Benchy');
    await userEvent.click(screen.getByRole('button', { name: /list view/i }));

    await userEvent.click(await screen.findByRole('button', { name: /^Size/ }));
    await waitFor(() => expect(lastQuery?.get('sort_by')).toBe('size-desc'));
  });

  it('still offers the size selector when everything fits on one page', async () => {
    // Without this, "2 of 2" is a dead end: the only control that could ask for
    // a different size is the one that just disappeared with the arrows.
    mockArchives({ current_page: 1, per_page: 24, total: 2, last_page: 1 });
    render(<ArchivesPage />);
    await waitFor(() => expect(bar()).toBeTruthy());

    expect(within(bar()).getByRole('combobox', { name: /show/i })).toBeInTheDocument();
    expect(within(bar()).queryByRole('button', { name: /next page/i })).not.toBeInTheDocument();
  });
});
