/**
 * "Clear filters" must clear every filter, and must be reachable.
 *
 * It used to reset the search box and the type dropdown only, leaving the
 * computed-tag chip row, the username box and the cross-cutting user-tag
 * filter still narrowing the list — so the button promised a reset and handed
 * back a library that was still partial, with nothing on screen saying why.
 *
 * The user-tag filter is worse than the others: it is applied SERVER-side, so
 * when it matches nothing the listing comes back empty and the screen used to
 * show "No files yet" with an Upload button. The reset was not merely
 * incomplete there — it was not on screen at all.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { FileManagerPage } from '../../pages/FileManagerPage';

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom');
  return { ...actual, useNavigate: () => vi.fn() };
});

const USER_TAG = { id: 7, name: 'kid-safe' };

const mockFiles = [
  {
    id: 1,
    filename: 'benchy.gcode.3mf',
    file_path: '/library/benchy.gcode.3mf',
    file_size: 1048576,
    file_type: 'gcode',
    file_tags: ['gcode', '3mf', 'sliced'],
    folder_id: null,
    thumbnail_path: null,
    print_name: 'Benchy',
    print_time_seconds: 3600,
    duplicate_count: 0,
    print_count: 4,
    created_by_username: 'alice',
    // Deliberately NOT tagged: the catalog below still puts a "kid-safe" pill
    // in the toolbar's tag-filter row, and that is the one the tests click. A
    // second pill on the card would make the query ambiguous.
    tags: [],
    created_at: '2024-01-01T00:00:00Z',
  },
  {
    id: 2,
    filename: 'bracket.stl',
    file_path: '/library/bracket.stl',
    file_size: 524288,
    file_type: 'stl',
    file_tags: ['stl', 'geometry'],
    folder_id: null,
    thumbnail_path: null,
    print_name: null,
    print_time_seconds: null,
    duplicate_count: 0,
    print_count: 0,
    created_by_username: 'bob',
    tags: [],
    created_at: '2024-01-02T00:00:00Z',
  },
];

/** Every `tag_ids` the listing has been asked for, newest call last. */
let tagIdsSeen: string[][] = [];

describe('clear filters', () => {
  beforeEach(() => {
    localStorage.clear();
    tagIdsSeen = [];
    server.use(
      http.get('/api/v1/library/folders', () => HttpResponse.json([])),
      // Server-driven (task 2, 2026-08-29): `username` is now a server param
      // too (used to be a client-side filter over the whole loaded page), so
      // the "clears the username filter" test below needs it to actually
      // narrow the mock's result — mirror the backend's substring match.
      http.get('/api/v1/library/files', ({ request }) => {
        const params = new URL(request.url).searchParams;
        const tagIds = params.getAll('tag_ids');
        tagIdsSeen.push(tagIds);
        // The user-tag filter is server-side: matching nothing empties the
        // listing itself, not just the client-side view of it.
        let items = tagIds.length > 0 ? [] : mockFiles;
        const username = params.get('username');
        if (username) {
          const needle = username.toLowerCase();
          items = items.filter((f) => f.created_by_username?.toLowerCase().includes(needle));
        }
        return HttpResponse.json({
          items,
          meta: { total: items.length, current_page: 1, per_page: 50, last_page: 1 },
        });
      }),
      http.get('/api/v1/library/stats', () =>
        HttpResponse.json({
          total_files: 2,
          total_folders: 0,
          total_size_bytes: 1572864,
          disk_free_bytes: 10737418240,
          disk_total_bytes: 107374182400,
        }),
      ),
      http.get('/api/v1/settings/', () =>
        HttpResponse.json({ check_updates: false, check_printer_firmware: false, library_disk_warning_gb: 5 }),
      ),
      http.get('/api/v1/projects/', () => HttpResponse.json([])),
      http.get('/api/v1/library/tags', () => HttpResponse.json([USER_TAG])),
    );
  });

  it('clears a system tag from the filter row', async () => {
    // Replaces two tests written against the computed chip row, which is gone:
    // both kinds of tag now go through `selectedTagIds`, so what they guarded
    // — that clear resets the tag filter, and that nothing comes back from
    // localStorage — is asserted in "turns the server-side tag filter off
    // again" below and in FileManagerTagFilter.test.tsx respectively. What was
    // NOT covered is the new half: a system pill is a filter too, and a reset
    // that only knew about user tags would leave it on.
    server.use(
      http.get('/api/v1/library/tags', () =>
        HttpResponse.json([
          { id: 9, name: '3MF', file_count: 2, is_system: true, code: '3mf' },
          USER_TAG,
        ]),
      ),
    );
    render(<FileManagerPage />);
    await screen.findByText('Benchy');

    await userEvent.click(screen.getByRole('button', { name: '3MF' }));
    await waitFor(() => expect(tagIdsSeen.at(-1)).toEqual(['9']));

    await userEvent.click(await screen.findByRole('button', { name: 'Clear filters' }));

    await waitFor(() => expect(tagIdsSeen.at(-1)).toEqual([]));
  });

  it('clears the username filter', async () => {
    render(<FileManagerPage />);
    await screen.findByText('Benchy');

    await userEvent.type(screen.getByPlaceholderText('Filter by user'), 'zzznobody');
    await waitFor(() => expect(screen.queryByText('Benchy')).not.toBeInTheDocument());

    await userEvent.click(await screen.findByRole('button', { name: 'Clear filters' }));

    expect(await screen.findByText('Benchy')).toBeInTheDocument();
  });

  it('offers the reset when a server-side tag filter empties the library', async () => {
    // The listing itself comes back empty here, which used to take the
    // "No files yet" branch — an upload prompt, and no way back.
    render(<FileManagerPage />);
    await screen.findByText('Benchy');

    await userEvent.click(screen.getByRole('button', { name: 'kid-safe' }));

    await waitFor(() => expect(tagIdsSeen.at(-1)).toEqual(['7']));
    expect(await screen.findByRole('button', { name: 'Clear filters' })).toBeInTheDocument();
    expect(screen.queryByText('No files yet')).not.toBeInTheDocument();
  });

  it('turns the server-side tag filter off again', async () => {
    // Resetting only the client-side filters would leave the backend still
    // filtering, so the library would stay empty and the button would look
    // broken rather than incomplete.
    render(<FileManagerPage />);
    await screen.findByText('Benchy');

    await userEvent.click(screen.getByRole('button', { name: 'kid-safe' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Clear filters' }));

    await waitFor(() => expect(tagIdsSeen.at(-1)).toEqual([]));
    expect(await screen.findByText('Benchy')).toBeInTheDocument();
  });
});
