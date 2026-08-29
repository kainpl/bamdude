/**
 * Move and Tags on a single file.
 *
 * Both lived only in the multi-select toolbar, so moving one file meant ticking
 * its checkbox first — a selection step for an action that needs no selection.
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
  { id: 1, name: '3MF', file_count: 2, is_system: true, code: '3mf', ...stamps },
  { id: 2, name: 'kid-safe', file_count: 1, is_system: false, code: null, ...stamps },
  { id: 3, name: 'petg-only', file_count: 0, is_system: false, code: null, ...stamps },
];

const mockFiles = [
  {
    id: 1,
    filename: 'benchy.gcode.3mf',
    file_path: '/library/benchy.gcode.3mf',
    file_size: 1048576,
    file_type: 'gcode',
    file_tags: ['gcode', '3mf'],
    folder_id: null,
    thumbnail_path: null,
    print_name: 'Benchy',
    print_time_seconds: 3600,
    duplicate_count: 0,
    print_count: 0,
    tags: [{ id: 2, name: 'kid-safe' }],
    created_at: '2024-01-01T00:00:00Z',
  },
  {
    id: 2,
    filename: 'bracket.stl',
    file_path: '/library/bracket.stl',
    file_size: 524288,
    file_type: 'stl',
    file_tags: ['stl'],
    folder_id: null,
    thumbnail_path: null,
    print_name: null,
    print_time_seconds: null,
    duplicate_count: 0,
    print_count: 0,
    tags: [],
    created_at: '2024-01-02T00:00:00Z',
  },
];

let assigned: { file_ids: number[]; tag_ids: number[]; action: string }[] = [];

describe('per-file actions', () => {
  beforeEach(() => {
    localStorage.clear();
    assigned = [];
    server.use(
      http.get('/api/v1/library/folders', () =>
        HttpResponse.json([{ id: 5, name: 'Parts', parent_id: null, file_count: 0, projects: [], children: [] }]),
      ),
      // Server-driven (task 2, 2026-08-29): FileManagerPage always sends
      // `page`, so the endpoint answers with the {items, meta} envelope.
      http.get('/api/v1/library/files', () =>
        HttpResponse.json({
          items: mockFiles,
          meta: { total: mockFiles.length, current_page: 1, per_page: 50, last_page: 1 },
        }),
      ),
      http.get('/api/v1/library/stats', () =>
        HttpResponse.json({
          total_files: 2,
          total_folders: 1,
          total_size_bytes: 1572864,
          disk_free_bytes: 10737418240,
          disk_total_bytes: 107374182400,
        }),
      ),
      http.get('/api/v1/settings/', () =>
        HttpResponse.json({ check_updates: false, check_printer_firmware: false, library_disk_warning_gb: 5 }),
      ),
      http.get('/api/v1/projects/', () => HttpResponse.json([])),
      http.get('/api/v1/library/tags', () => HttpResponse.json(TAGS)),
      http.post('/api/v1/library/tags/bulk-assign', async ({ request }) => {
        assigned.push((await request.json()) as { file_ids: number[]; tag_ids: number[]; action: string });
        return HttpResponse.json({ files_updated: 1, associations_added: 1, associations_removed: 0 });
      }),
    );
  });

  /**
   * Open the ⋮ menu of the card whose title is `name`.
   *
   * Picks the trigger by its icon rather than by position: "the last button in
   * the card" also matches the selection target, and clicking THAT makes the
   * multi-select toolbar appear — which has its own Move button, so the test
   * would pass while proving nothing about the menu.
   */
  const openMenuOf = async (name: string) => {
    const card = (await screen.findByText(name)).closest('.group') as HTMLElement;
    const trigger = within(card)
      .getAllByRole('button')
      .find((b) => b.querySelector('.lucide-ellipsis-vertical, .lucide-more-vertical'));
    if (!trigger) throw new Error('no ⋮ trigger found on the card');
    await userEvent.click(trigger);
  };

  it('offers Move on a single file', async () => {
    render(<FileManagerPage />);
    await screen.findByText('Benchy');

    // Precondition: nothing is selected, so the toolbar's Move is not on
    // screen. Without this the assertion below passes on the wrong button.
    expect(screen.queryByRole('button', { name: /^Move$/i })).not.toBeInTheDocument();

    await openMenuOf('Benchy');

    expect(await screen.findByRole('button', { name: /^Move$/i })).toBeInTheDocument();
  });

  it('the tags popover shows what is already on the file', async () => {
    // The thing the bulk modal cannot express: it has an add/remove mode
    // switch precisely because many files disagree, and for one file the
    // honest answer is a checkbox that shows the current state.
    render(<FileManagerPage />);
    await screen.findByText('Benchy');

    await openMenuOf('Benchy');
    // "Tag" (the menu verb), not "Tags" — the toolbar's Tags button opens the
    // catalog dialog, and matching it would test the wrong surface entirely.
    await userEvent.click(await screen.findByRole('button', { name: /^Tag$/ }));

    expect(await screen.findByRole('checkbox', { name: 'kid-safe' })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: 'petg-only' })).not.toBeChecked();
  });

  it('does not offer a system tag in the popover', async () => {
    render(<FileManagerPage />);
    await screen.findByText('Benchy');

    await openMenuOf('Benchy');
    // "Tag" (the menu verb), not "Tags" — the toolbar's Tags button opens the
    // catalog dialog, and matching it would test the wrong surface entirely.
    await userEvent.click(await screen.findByRole('button', { name: /^Tag$/ }));

    await screen.findByRole('checkbox', { name: 'kid-safe' });
    expect(screen.queryByRole('checkbox', { name: '3MF' })).not.toBeInTheDocument();
  });

  it('applies a tag to that file and no other', async () => {
    // Two files present: the wrong target is a plausible bug and an invisible
    // one, since both would report success.
    render(<FileManagerPage />);
    await screen.findByText('Benchy');

    await openMenuOf('Benchy');
    // "Tag" (the menu verb), not "Tags" — the toolbar's Tags button opens the
    // catalog dialog, and matching it would test the wrong surface entirely.
    await userEvent.click(await screen.findByRole('button', { name: /^Tag$/ }));
    await userEvent.click(await screen.findByRole('checkbox', { name: 'petg-only' }));

    await waitFor(() => expect(assigned).toHaveLength(1));
    expect(assigned[0]).toEqual({ file_ids: [1], tag_ids: [3], action: 'add' });
  });

  it('anchors the popover to the card, not the pointer', async () => {
    // In list view the ⋮ menu is portal-rendered far from its row, so the
    // cursor is nowhere near the file — the panel used to open there. Both
    // placements pin the RIGHT edge to the file's right edge; the card adds a
    // bottom edge so it grows up and left into the card it belongs to.
    //
    // jsdom reports every box as 0×0, so the arithmetic itself cannot be
    // checked here. What CAN be checked is that the panel is positioned off
    // the file's box at all, rather than off clientX/clientY — which is the
    // regression in question.
    render(<FileManagerPage />);
    await screen.findByText('Benchy');

    await openMenuOf('Benchy');
    await userEvent.click(await screen.findByRole('button', { name: /^Tag$/ }));

    const panel = await screen.findByRole('dialog', { name: /tags/i });
    expect(panel).toHaveStyle({ position: 'fixed' });
    // Grid mode → bottom-anchored. A pointer-anchored panel sets `top`/`left`.
    expect(panel.style.bottom).not.toBe('');
    expect(panel.style.right).not.toBe('');
    expect(panel.style.left).toBe('');
  });

  it('unticking removes the tag', async () => {
    render(<FileManagerPage />);
    await screen.findByText('Benchy');

    await openMenuOf('Benchy');
    // "Tag" (the menu verb), not "Tags" — the toolbar's Tags button opens the
    // catalog dialog, and matching it would test the wrong surface entirely.
    await userEvent.click(await screen.findByRole('button', { name: /^Tag$/ }));
    await userEvent.click(await screen.findByRole('checkbox', { name: 'kid-safe' }));

    await waitFor(() => expect(assigned).toHaveLength(1));
    expect(assigned[0]).toEqual({ file_ids: [1], tag_ids: [2], action: 'remove' });
  });
});
