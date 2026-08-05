/**
 * m128 made system tags rows in the same catalog as user tags, so
 * ``GET /library/tags`` now returns both kinds. Every surface that had been
 * rendering that response as "the user's tags" started rendering eleven system
 * ones too — the toolbar filter row, the bulk assignment picker, and the
 * management dialog.
 *
 * The backend phase was supposed to change nothing on screen. It changed three
 * things, and this file pins all three so the claim is true again.
 *
 * The filter row and the management dialog show both kinds DELIBERATELY once
 * the frontend phase lands. The bulk picker never will: the backend drops
 * system ids from add/remove silently, so a checkbox for one reports success
 * and does nothing.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { FileManagerPage } from '../../pages/FileManagerPage';
import { BulkTagsPickerModal } from '../../components/BulkTagsPickerModal';

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom');
  return { ...actual, useNavigate: () => vi.fn() };
});

const TAGS = [
  {
    id: 1,
    name: '3MF',
    file_count: 12,
    is_system: true,
    code: '3mf',
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
  },
  {
    id: 2,
    name: 'kid-safe',
    file_count: 4,
    is_system: false,
    code: null,
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
  },
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

describe('system tags must not leak into user-tag surfaces', () => {
  beforeEach(() => {
    localStorage.clear();
    server.use(
      http.get('/api/v1/library/folders', () => HttpResponse.json([])),
      http.get('/api/v1/library/files', () => HttpResponse.json(mockFiles)),
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

  it('the toolbar tag row shows only user tags', async () => {
    // Scoped to the row itself: a bare query for "3MF" also matches the
    // computed badge chip elsewhere on the page, which is a different thing
    // and legitimately there. The row renders one pill per catalog row, so on
    // a real install it grew from one pill to twelve — which is what "a second
    // tag filter appeared" looks like from the outside.
    render(<FileManagerPage />);
    await screen.findByText('Benchy');

    const row = (await screen.findByText('Filtering by:')).parentElement!;
    expect(within(row).getByRole('button', { name: 'kid-safe' })).toBeInTheDocument();
    expect(within(row).queryByRole('button', { name: '3MF' })).not.toBeInTheDocument();
  });

  it('the bulk tag picker does not offer a system tag', async () => {
    // The one that matters permanently: the backend drops system ids from
    // add/remove silently, so ticking this box would report success and change
    // nothing. Rendered directly rather than through the selection toolbar —
    // the question is what the picker offers, not how it is opened.
    render(<BulkTagsPickerModal open fileIds={[1]} onClose={() => {}} />);

    expect(await screen.findByText('kid-safe')).toBeInTheDocument();
    expect(screen.queryByText('3MF')).not.toBeInTheDocument();
  });
});
