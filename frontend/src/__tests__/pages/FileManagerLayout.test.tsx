/**
 * The File Manager's layout contract, read off the rendered classes.
 *
 * jsdom lays nothing out, so these pin the grid template and the boxes that
 * decide the layout: the list view's name column has a floor and its tag column
 * a ceiling (a `minmax(0,1fr)` name next to an unbounded `max-content` tag track
 * was squeezed to zero by one file with many tags), and the list scrolls
 * sideways itself instead of pushing the page.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { screen } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { FileManagerPage } from '../../pages/FileManagerPage';

vi.mock('react-router', async () => {
  const actual = await vi.importActual<typeof import('react-router')>('react-router');
  return { ...actual, useNavigate: () => vi.fn() };
});

const stamps = { created_at: '2024-01-01T00:00:00Z', updated_at: '2024-01-01T00:00:00Z' };

const mockFiles = [
  {
    id: 1,
    filename: 'benchy.gcode.3mf',
    file_path: '/library/benchy.gcode.3mf',
    file_size: 1048576,
    file_type: 'gcode',
    file_tags: ['gcode', '3mf', 'sliced'],
    product_ids: [],
    folder_id: null,
    thumbnail_path: null,
    print_name: 'Benchy',
    print_time_seconds: 3600,
    duplicate_count: 0,
    print_count: 0,
    tags: [
      { id: 3, name: 'kid-safe', file_count: 1, is_system: false, code: null, ...stamps },
      { id: 4, name: 'a-very-long-tag-name-somebody-typed-in-a-hurry', file_count: 1, is_system: false, code: null, ...stamps },
    ],
    created_at: '2024-01-01T00:00:00Z',
  },
];

function mockLibrary() {
  server.use(
    http.get('/api/v1/library/folders', () => HttpResponse.json([])),
    http.get('/api/v1/library/files', () =>
      HttpResponse.json({
        items: mockFiles,
        meta: { total: mockFiles.length, current_page: 1, per_page: 50, last_page: 1 },
      }),
    ),
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
    http.get('/api/v1/library/tags', () => HttpResponse.json([])),
  );
}

/** The tracks of an arbitrary `grid-cols-[a_b_c]` class, in order. */
function tracksOf(el: HTMLElement): string[] {
  const cls = Array.from(el.classList).find((c) => c.startsWith('grid-cols-['));
  if (!cls) throw new Error('no grid template on the list');
  return cls.slice('grid-cols-['.length, -1).split('_');
}

describe('the File Manager list view', () => {
  beforeEach(() => {
    localStorage.clear();
    localStorage.setItem('library-view-mode', 'list');
    mockLibrary();
  });

  const listGrid = async () => {
    const row = (await screen.findByText('Benchy')).closest('[data-file-row]') as HTMLElement;
    expect(row).toBeTruthy();
    return row.parentElement as HTMLElement;
  };

  it('gives the name column a floor, so it never collapses to nothing', async () => {
    render(<FileManagerPage />);
    const tracks = tracksOf(await listGrid());

    // checkbox · NAME · …
    expect(tracks[0]).toBe('auto');
    expect(tracks[1]).toMatch(/^minmax\((?!0[,)])[\d.]+(rem|px),1fr\)$/);
  });

  it('bounds the tag column instead of sizing it to the widest row', async () => {
    render(<FileManagerPage />);
    const grid = await listGrid();
    const tracks = tracksOf(grid);

    // …size · date · actions are the last three; the tags sit right before them.
    const tags = tracks[tracks.length - 4];
    expect(tags).toMatch(/^fit-content\([\d.]+(rem|px)\)$/);
    // No other track may be left that a long row can stretch without limit in
    // the tag column's place.
    expect(tracks.filter((t) => t === 'max-content').length).toBe(tracks.length - 3);

    // A single chip cannot widen the column past its ceiling either.
    const chip = screen.getByRole('button', { name: 'a-very-long-tag-name-somebody-typed-in-a-hurry' });
    expect(chip.className).toMatch(/\bmax-w-\[[\d.]+rem\]/);
    expect(chip.className).toContain('truncate');
  });

  it('scrolls the list sideways itself — the page never does', async () => {
    render(<FileManagerPage />);
    const grid = await listGrid();

    // The rows keep their floors (min-content), and the box around them scrolls.
    expect(grid.classList.contains('min-w-min')).toBe(true);
    const box = grid.parentElement as HTMLElement;
    expect(box.classList.contains('overflow-x-auto')).toBe(true);
    expect(box.classList.contains('min-w-0')).toBe(true);
  });
});

describe('the File Manager header', () => {
  beforeEach(() => {
    localStorage.clear();
    mockLibrary();
  });

  it('lets the action row wrap in the space left of the title instead of squeezing the title', async () => {
    render(<FileManagerPage />);
    const title = await screen.findByRole('heading', { level: 1 });
    const header = title.closest('.justify-between') as HTMLElement;
    const actions = header.lastElementChild as HTMLElement;

    for (const cls of ['flex-wrap', 'sm:flex-1', 'sm:justify-end', 'min-w-0']) {
      expect(actions.classList.contains(cls), cls).toBe(true);
    }
  });
});
