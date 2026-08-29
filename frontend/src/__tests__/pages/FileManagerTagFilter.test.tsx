/**
 * One row of tag pills, filtered server-side.
 *
 * There used to be two rows answering the same question: a computed chip row
 * filtering client-side with its state in localStorage, and a user-tag row
 * filtering server-side through ``tag_ids``. m128 made both kinds rows in one
 * catalog, so one row and one mechanism now serve both.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { FileManagerPage } from '../../pages/FileManagerPage';

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom');
  return { ...actual, useNavigate: () => vi.fn() };
});

const stamps = { created_at: '2024-01-01T00:00:00Z', updated_at: '2024-01-01T00:00:00Z' };

const TAGS = [
  { id: 1, name: '3MF', file_count: 12, is_system: true, code: '3mf', ...stamps },
  // Nothing in the library carries this one.
  { id: 2, name: 'STEP', file_count: 0, is_system: true, code: 'step', ...stamps },
  { id: 3, name: 'kid-safe', file_count: 4, is_system: false, code: null, ...stamps },
];

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
    print_count: 0,
    tags: [],
    created_at: '2024-01-01T00:00:00Z',
  },
];

/** Every `tag_ids` the listing has been asked for, newest call last. */
let tagIdsSeen: string[][] = [];

describe('the unified tag filter row', () => {
  beforeEach(() => {
    localStorage.clear();
    tagIdsSeen = [];
    server.use(
      http.get('/api/v1/library/folders', () => HttpResponse.json([])),
      http.get('/api/v1/library/files', ({ request }) => {
        tagIdsSeen.push(new URL(request.url).searchParams.getAll('tag_ids'));
        // Server-driven (task 2, 2026-08-29): FileManagerPage always sends
        // `page`, so the endpoint answers with the {items, meta} envelope.
        return HttpResponse.json({
          items: mockFiles,
          meta: { total: mockFiles.length, current_page: 1, per_page: 50, last_page: 1 },
        });
      }),
      http.get('/api/v1/library/stats', () =>
        HttpResponse.json({
          total_files: 1,
          total_folders: 0,
          total_size_bytes: 1048576,
          disk_free_bytes: 10737418240,
          disk_total_bytes: 107374182400,
        }),
      ),
      http.get('/api/v1/settings/', () =>
        HttpResponse.json({ check_updates: false, check_printer_firmware: false, library_disk_warning_gb: 5 }),
      ),
      http.get('/api/v1/projects/', () => HttpResponse.json([])),
      http.get('/api/v1/library/tags', () => HttpResponse.json(TAGS)),
    );
  });

  /**
   * Re-queried on every call, never captured once. The row lives inside the
   * filter panel, which re-renders on each refetch — a held reference goes
   * stale and clicks on it land on a detached node, silently doing nothing.
   */
  const tagRow = async () => (await screen.findByText('Filtering by:')).parentElement!;
  const clickPill = async (name: string) =>
    userEvent.click(within(await tagRow()).getByRole('button', { name }));

  it('filters by a system tag through the server', async () => {
    // This is the whole point of the merge. The same question used to be a
    // client-side predicate over the loaded page, which is why it needed its
    // own state, its own localStorage key and its own clear button.
    render(<FileManagerPage />);
    await tagRow();

    await clickPill('3MF');

    await waitFor(() => expect(tagIdsSeen.at(-1)).toEqual(['1']));
  });

  it('combines a system tag with a user tag', async () => {
    render(<FileManagerPage />);
    await tagRow();

    await clickPill('3MF');
    await clickPill('kid-safe');

    await waitFor(() => expect(tagIdsSeen.at(-1)?.sort()).toEqual(['1', '3']));
  });

  it('does not offer a system tag no file carries', async () => {
    // The count is GLOBAL, from the catalog. The old row derived it from the
    // loaded page, so pills vanished as you narrowed — making it impossible to
    // switch from one to another, because the second was already gone.
    render(<FileManagerPage />);
    const row = await tagRow();

    expect(within(row).queryByRole('button', { name: 'STEP' })).not.toBeInTheDocument();
    expect(within(row).getByRole('button', { name: '3MF' })).toBeInTheDocument();
  });

  it('still offers a user tag no file carries', async () => {
    // Asymmetric on purpose: somebody made that tag deliberately, and an empty
    // one is precisely the one you want to see in order to start using it.
    server.use(
      http.get('/api/v1/library/tags', () =>
        HttpResponse.json([{ id: 3, name: 'kid-safe', file_count: 0, is_system: false, code: null, ...stamps }]),
      ),
    );
    render(<FileManagerPage />);

    const row = await tagRow();
    expect(within(row).getByRole('button', { name: 'kid-safe' })).toBeInTheDocument();
  });

  it('ignores a filter left behind in localStorage', async () => {
    // The old row persisted its selection. The merged one does not — a library
    // that comes back narrowed with nothing on screen explaining why costs
    // more than one click.
    localStorage.setItem('library-filter-tags', JSON.stringify(['sliced']));

    render(<FileManagerPage />);
    await screen.findByText('Benchy');

    expect(tagIdsSeen.at(-1)).toEqual([]);
  });

  it('has no second tag filter row left', async () => {
    // The computed chip row is gone, not hidden. Its clear button is the
    // cheapest witness that it is really absent.
    render(<FileManagerPage />);
    await screen.findByText('Benchy');

    expect(screen.queryByText('Clear tag filter')).not.toBeInTheDocument();
  });
});
