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
      http.get('/api/v1/library/files', ({ request }) => {
        const tagIds = new URL(request.url).searchParams.getAll('tag_ids');
        tagIdsSeen.push(tagIds);
        // The user-tag filter is server-side: matching nothing empties the
        // listing itself, not just the client-side view of it.
        return HttpResponse.json(tagIds.length > 0 ? [] : mockFiles);
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

  /**
   * The chips AND together, so SLICED + GEO matches nothing — which is what
   * puts the empty state, and therefore the Clear filters button, on screen.
   * Narrowing to one file would leave the grid rendered and the button absent.
   */
  const activateImpossibleChipPair = async () => {
    await userEvent.click(screen.getByRole('button', { name: 'SLICED' }));
    await userEvent.click(screen.getByRole('button', { name: 'GEO' }));
  };

  it('clears the computed-tag chip filter', async () => {
    render(<FileManagerPage />);
    await screen.findByText('Benchy');

    await activateImpossibleChipPair();

    await userEvent.click(await screen.findByRole('button', { name: 'Clear filters' }));

    expect(await screen.findByText('Benchy')).toBeInTheDocument();
    expect(screen.getByText('bracket.stl')).toBeInTheDocument();
  });

  it('does not let the chip filter come back from localStorage', async () => {
    // It is persisted, so a reset that only touched React state would look
    // right until the next reload and then quietly re-narrow the library.
    render(<FileManagerPage />);
    await screen.findByText('Benchy');

    await activateImpossibleChipPair();
    await userEvent.click(await screen.findByRole('button', { name: 'Clear filters' }));
    await screen.findByText('bracket.stl');

    await waitFor(() => expect(JSON.parse(localStorage.getItem('library-filter-tags') || '[]')).toEqual([]));
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
