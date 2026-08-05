/**
 * The bulk queue button exists only when it can do something.
 *
 * An action that cannot apply to the current selection is hidden, not disabled:
 * a button that opens a window whose only content is "nothing here can be
 * queued" spends two clicks saying what the absence of the button says for
 * free.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { FileManagerPage } from '../../pages/FileManagerPage';

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom');
  return { ...actual, useNavigate: () => vi.fn() };
});

const base = {
  file_path: '/library/x',
  file_size: 1024,
  folder_id: null,
  thumbnail_path: null,
  print_time_seconds: null,
  duplicate_count: 0,
  print_count: 0,
  tags: [],
  created_at: '2024-01-01T00:00:00Z',
};

const SLICED = { ...base, id: 1, filename: 'benchy.gcode.3mf', file_type: 'gcode', file_tags: ['gcode', '3mf'], print_name: 'Benchy' };
const RAW = { ...base, id: 2, filename: 'bracket.stl', file_type: 'stl', file_tags: ['stl', 'geometry'], print_name: null };

function mockLibrary(files: unknown[]) {
  server.use(
    http.get('/api/v1/library/folders', () => HttpResponse.json([])),
    http.get('/api/v1/library/files', () => HttpResponse.json(files)),
    http.get('/api/v1/library/stats', () =>
      HttpResponse.json({
        total_files: files.length,
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

/** Tick a file by its own select control, never by position. */
async function selectFile(name: string) {
  const card = (await screen.findByText(name)).closest('.group, [data-file-row]') as HTMLElement;
  const control = card.querySelector('[data-select-file]') as HTMLElement | null;
  if (!control) throw new Error(`no [data-select-file] control for ${name}`);
  await userEvent.click(control);
}

describe('the bulk queue button', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it('appears when the selection holds a sliced file', async () => {
    mockLibrary([SLICED, RAW]);
    render(<FileManagerPage />);
    await screen.findByText('Benchy');

    await selectFile('Benchy');

    expect(await screen.findByRole('button', { name: /^Queue$/i })).toBeInTheDocument();
  });

  it('stays away for a selection of only unsliced files', async () => {
    // The other half. A button hidden unconditionally would pass this alone,
    // which is why the pair is what makes either test mean anything.
    mockLibrary([SLICED, RAW]);
    render(<FileManagerPage />);
    await screen.findByText('bracket.stl');

    await selectFile('bracket.stl');

    // Something IS selected — the toolbar is on screen, just without this button.
    expect(await screen.findByRole('button', { name: /Deselect|Clear/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Queue$/i })).not.toBeInTheDocument();
  });
});
