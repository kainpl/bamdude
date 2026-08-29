/**
 * FileManagerPage's paging controls (task 2, plus three review fix rounds,
 * 2026-08-29).
 *
 * Things flagged as unverified across the rounds:
 *
 * - Paging under the rows actually asks the server for the page clicked.
 * - A filter change while on page 2 must never let the query see the STALE
 *   page combined with the NEW filter — that would be one wasted request
 *   (the mismatched combination), fired before a second, correct one. The
 *   fix moved the reset from a post-commit `useEffect` to a render-time
 *   adjustment specifically so the mismatched render is thrown away before
 *   its `useQuery` ever gets a chance to fetch.
 * - The per-page selector shows the actual default (50) on first load —
 *   PaginationBar's own built-in options ([12,24,48,96]) don't include it,
 *   which used to render the select with nothing selected.
 * - A row selected on one page must not survive onto another — otherwise the
 *   bulk bar (Move / Delete / Tag) can point at a row that is no longer on
 *   screen, ticked or not.
 * - A page number the server itself no longer has (the final review's
 *   example: the list shrank out from under a page-3 view) must clamp back
 *   into range rather than render "No files yet" with the pager's own math
 *   broken (`current_page > last_page`).
 * - The clamp above is a THIRD path that changes `page`, next to the manual
 *   pager and the filter-driven reset — a re-review caught that it was the
 *   only one of the three still leaving a stale selection behind.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { FileManagerPage } from '../../pages/FileManagerPage';

const file = (id: number, name: string) => ({
  id,
  filename: `${name}.gcode.3mf`,
  file_path: `/library/${name}.gcode.3mf`,
  file_size: 1024,
  file_type: 'gcode',
  file_tags: ['gcode', '3mf', 'sliced'],
  folder_id: null,
  thumbnail_path: null,
  print_name: name,
  print_time_seconds: 3600,
  duplicate_count: 0,
  print_count: 0,
  created_at: '2024-01-01T00:00:00Z',
});

/** Every request's full query-string, newest last. */
let requests: URLSearchParams[] = [];

function mockLibrary() {
  server.use(
    http.get('/api/v1/library/folders', () => HttpResponse.json([])),
    // The mock echoes back whatever `page` was actually requested (never a
    // hardcoded value) — the whole point of these tests is checking WHICH
    // page the query asked for, so the response has to reflect that.
    http.get('/api/v1/library/files', ({ request }) => {
      const params = new URL(request.url).searchParams;
      requests.push(params);
      const page = Number(params.get('page') ?? '1');
      return HttpResponse.json({
        items: [file(1, 'Benchy'), file(2, 'Bracket')],
        meta: { current_page: page, per_page: 50, total: 100, last_page: 2 },
      });
    }),
    http.get('/api/v1/library/stats', () =>
      HttpResponse.json({
        total_files: 100,
        total_folders: 0,
        total_size_bytes: 1024,
        disk_free_bytes: 1,
        disk_total_bytes: 2,
      }),
    ),
    http.get('/api/v1/settings/', () =>
      HttpResponse.json({ check_updates: false, check_printer_firmware: false, library_disk_warning_gb: 5 }),
    ),
    http.get('/api/v1/projects/', () => HttpResponse.json([])),
    http.get('/api/v1/library/tags', () => HttpResponse.json([])),
  );
}

const bar = () => document.querySelector('[data-pagination]') as HTMLElement;

describe('FileManagerPage pagination', () => {
  beforeEach(() => {
    requests = [];
    localStorage.clear();
    mockLibrary();
  });

  it('requests the new page when the next-page control is used', async () => {
    render(<FileManagerPage />);
    await waitFor(() => expect(bar()).toBeTruthy());

    await userEvent.click(within(bar()).getByRole('button', { name: /next page/i }));

    await waitFor(() => expect(requests.at(-1)?.get('page')).toBe('2'));
  });

  it('never combines a new filter with the stale page — a filter change on page 2 fires exactly once, already on page 1', async () => {
    render(<FileManagerPage />);
    await waitFor(() => expect(bar()).toBeTruthy());

    // Get onto page 2 for real (the internal `page` state, not just what the
    // mock happens to report) before touching a filter.
    await userEvent.click(within(bar()).getByRole('button', { name: /next page/i }));
    await waitFor(() => expect(requests.at(-1)?.get('page')).toBe('2'));

    requests = []; // only the requests caused by the filter click matter below
    await userEvent.click(screen.getByRole('button', { name: 'Not printed' }));

    await waitFor(() => expect(requests.length).toBeGreaterThan(0));
    // Exactly one request — an effect-based reset would have fired a first
    // one with the STALE page (2) and the NEW filter, then a second with
    // page reset to 1.
    expect(requests).toHaveLength(1);
    expect(requests[0].get('unprinted_only')).toBe('true');
    expect(requests[0].get('page')).toBe('1');
  });

  it('shows the selected per-page value on first load', async () => {
    render(<FileManagerPage />);
    await waitFor(() => expect(bar()).toBeTruthy());

    expect(within(bar()).getByRole('combobox', { name: /show/i })).toHaveValue('50');
  });

  it('drops the selection on page change — a bulk action must never point at a row that is no longer on screen', async () => {
    render(<FileManagerPage />);
    await screen.findByText('Benchy');

    const card = screen.getByText('Benchy').closest('.group') as HTMLElement;
    await userEvent.click(within(card).getByLabelText('Select file'));
    expect(await screen.findByText('1 selected')).toBeInTheDocument();

    await userEvent.click(within(bar()).getByRole('button', { name: /next page/i }));

    await waitFor(() => expect(screen.queryByText('1 selected')).not.toBeInTheDocument());
    // The bulk bar itself is gone, not merely relabelled.
    expect(screen.queryByRole('button', { name: /^Move$/i })).not.toBeInTheDocument();
  });

  it('clamps back into range when a response reports a smaller last_page than the page requested', async () => {
    // Own mock, not the shared `mockLibrary()` — `last_page` needs to change
    // mid-test (simulating the list shrinking out from under a page-3 view),
    // which the shared helper's fixed value can't do.
    let responseLastPage = 5;
    server.use(
      http.get('/api/v1/library/folders', () => HttpResponse.json([])),
      http.get('/api/v1/library/files', ({ request }) => {
        const params = new URL(request.url).searchParams;
        requests.push(params);
        const page = Number(params.get('page') ?? '1');
        return HttpResponse.json({
          items: page === 1 ? [file(1, 'Benchy'), file(2, 'Bracket')] : [],
          meta: { current_page: page, per_page: 50, total: 100, last_page: responseLastPage },
        });
      }),
      http.get('/api/v1/library/stats', () =>
        HttpResponse.json({
          total_files: 100,
          total_folders: 0,
          total_size_bytes: 1024,
          disk_free_bytes: 1,
          disk_total_bytes: 2,
        }),
      ),
      http.get('/api/v1/settings/', () =>
        HttpResponse.json({ check_updates: false, check_printer_firmware: false, library_disk_warning_gb: 5 }),
      ),
      http.get('/api/v1/projects/', () => HttpResponse.json([])),
      http.get('/api/v1/library/tags', () => HttpResponse.json([])),
    );

    render(<FileManagerPage />);
    await waitFor(() => expect(bar()).toBeTruthy());

    await userEvent.click(within(bar()).getByRole('button', { name: /next page/i }));
    await waitFor(() => expect(requests.at(-1)?.get('page')).toBe('2'));

    // The list shrinks: the very response to the page-3 request below is the
    // one that discovers it, same as it would in production.
    responseLastPage = 1;
    await userEvent.click(within(bar()).getByRole('button', { name: /next page/i }));
    await waitFor(() => expect(requests.at(-1)?.get('page')).toBe('3'));

    // Left unclamped this stays on page 3 showing "No files yet" with the
    // pager reading current_page=3 of last_page=1. The clamp effect must
    // fire a follow-up fetch back onto page 1.
    await waitFor(() => expect(requests.at(-1)?.get('page')).toBe('1'));
  });

  it('drops the selection too when the clamp fires — the residual IMP2 case the clamp effect itself reopened', async () => {
    // Trigger: on page 2, tick a row, then something invalidates the list
    // (here: Generate Thumbnails' own onSuccess, standing in for the
    // review's "a delete/move invalidation") and the refetch of that SAME
    // page 2 is what discovers the list shrank. `page2FetchCount` tells the
    // handler which of the two page-2 fetches it's answering — the first
    // (still valid, needed so there's a row on screen to select) or the
    // second (the one invalidation triggers, which reports the shrink).
    let page2FetchCount = 0;
    server.use(
      http.get('/api/v1/library/folders', () => HttpResponse.json([])),
      http.get('/api/v1/library/files', ({ request }) => {
        const params = new URL(request.url).searchParams;
        requests.push(params);
        const page = Number(params.get('page') ?? '1');
        if (page === 2) page2FetchCount += 1;
        const shrunk = page === 2 && page2FetchCount >= 2;
        return HttpResponse.json({
          items: shrunk ? [] : [file(1, 'Benchy'), file(2, 'Bracket')],
          meta: { current_page: page, per_page: 50, total: 100, last_page: shrunk ? 1 : 2 },
        });
      }),
      http.get('/api/v1/library/stats', () =>
        HttpResponse.json({
          total_files: 100,
          total_folders: 0,
          total_size_bytes: 1024,
          disk_free_bytes: 1,
          disk_total_bytes: 2,
        }),
      ),
      http.get('/api/v1/settings/', () =>
        HttpResponse.json({ check_updates: false, check_printer_firmware: false, library_disk_warning_gb: 5 }),
      ),
      http.get('/api/v1/projects/', () => HttpResponse.json([])),
      http.get('/api/v1/library/tags', () => HttpResponse.json([])),
      http.post('/api/v1/library/generate-stl-thumbnails', () =>
        HttpResponse.json({ processed: 0, succeeded: 0, failed: 0, results: [] }),
      ),
    );

    render(<FileManagerPage />);
    await screen.findByText('Benchy');

    await userEvent.click(within(bar()).getByRole('button', { name: /next page/i }));
    await waitFor(() => expect(requests.at(-1)?.get('page')).toBe('2'));
    await screen.findByText('Benchy'); // page 2's own (still-valid) rows

    const card = screen.getByText('Benchy').closest('.group') as HTMLElement;
    await userEvent.click(within(card).getByLabelText('Select file'));
    expect(await screen.findByText('1 selected')).toBeInTheDocument();

    // The invalidation — same query key (page 2, no filter/sort change), so
    // this refetch is what actually discovers the shrink.
    await userEvent.click(screen.getByText('Generate Thumbnails'));

    await waitFor(() => expect(requests.at(-1)?.get('page')).toBe('1'));
    // Selection must not survive the clamp any more than it survives a
    // manual page change or a filter-driven reset.
    await waitFor(() => expect(screen.queryByText('1 selected')).not.toBeInTheDocument());
    expect(screen.queryByRole('button', { name: /^Move$/i })).not.toBeInTheDocument();
  });
});
