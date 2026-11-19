/**
 * `/archives?highlight=<archive id>` — the queue card's «View archive» (G03 D10.2).
 *
 * The link used to be `?search=<archive name>`, which the page never read: it opened
 * unfiltered, and named the archive by a name although the row holds its id. Now the
 * page reads the id, opens the page of its (server-paged) list that holds the archive
 * under the operator's own filters, marks the card, and says so when the list does not
 * hold it — never matching anything by name.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { ArchivesPage } from '../../pages/ArchivesPage';

const archive = (id: number) => ({
  id,
  filename: `Part ${id}.gcode.3mf`,
  file_path: `archive/1/Part ${id}.gcode.3mf`,
  print_name: `Part ${id}`,
  printer_id: null,
  status: 'completed',
  created_at: '2026-10-01T09:00:00Z',
  project_id: null,
  project_name: null,
  created_by_id: 1,
  tags: '',
  defective_count: 0,
});

/** 30 archives, newest (highest id) first — as the default date sort answers. */
const ALL = Array.from({ length: 30 }, (_, index) => archive(30 - index));

const listRequests: URLSearchParams[] = [];

function cardOf(id: number): HTMLElement | null {
  return document.querySelector(`[data-archive-id="${id}"]`);
}

describe('ArchivesPage — a link to one archive', () => {
  const scroll = vi.fn();
  const original = Element.prototype.scrollIntoView;

  beforeEach(() => {
    localStorage.clear();
    // Ten a page, so archive 5 (the 26th row) sits on page 3.
    localStorage.setItem('archivePerPage', '10');
    listRequests.length = 0;
    scroll.mockReset();
    Element.prototype.scrollIntoView = scroll;
    server.use(
      http.get('/api/v1/archives/', ({ request }) => {
        const params = new URL(request.url).searchParams;
        listRequests.push(params);
        const rows = params.get('search') ? [] : ALL;
        const perPage = Number(params.get('per_page') ?? 24);
        const page = Number(params.get('page') ?? 1);
        return HttpResponse.json({
          data: rows.slice((page - 1) * perPage, page * perPage),
          meta: { current_page: page, per_page: perPage, total: rows.length, last_page: Math.max(1, Math.ceil(rows.length / perPage)) },
        });
      }),
      http.get('/api/v1/printers/', () => HttpResponse.json([])),
      http.get('/api/v1/projects/', () => HttpResponse.json([])),
      http.get('/api/v1/archives/tags', () => HttpResponse.json([])),
    );
  });

  afterEach(() => {
    Element.prototype.scrollIntoView = original;
  });

  it('opens the page that holds the archive and marks it', async () => {
    window.history.pushState({}, '', '/archives?highlight=5');
    render(<ArchivesPage />);

    await waitFor(() => expect(cardOf(5)).not.toBeNull());
    await waitFor(() => expect(cardOf(5)).toHaveStyle({ outline: '4px solid #facc15' }));
    await waitFor(() => expect(scroll).toHaveBeenCalled());
    // The page the operator lands on is the third of ten — the archive's own.
    expect(listRequests.some((params) => params.get('page') === '3' && params.get('per_page') === '10')).toBe(true);
    // Looked for by id in the page's own list, never by name.
    expect(listRequests.every((params) => params.get('search') === null)).toBe(true);
    // Read once: the address no longer carries it.
    expect(window.location.search).not.toContain('highlight');
  });

  it('says so when the list does not hold the archive', async () => {
    window.history.pushState({}, '', '/archives?highlight=999');
    render(<ArchivesPage />);

    expect(await screen.findByText('The linked archive is not in the current list - try clearing filters')).toBeInTheDocument();
    expect(scroll).not.toHaveBeenCalled();
  });
});
