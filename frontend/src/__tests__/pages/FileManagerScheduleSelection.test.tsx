/**
 * The Schedule button exists only when it can do something.
 *
 * An action that cannot apply to the current selection is hidden, not disabled:
 * a button that opens a dialog for a file nothing can print spends two clicks
 * saying what the absence of the button says for free.
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
    // Server-driven (task 2, 2026-08-29): FileManagerPage always sends
    // `page`, so the endpoint answers with the {items, meta} envelope.
    http.get('/api/v1/library/files', () =>
      HttpResponse.json({
        items: files,
        meta: { total: files.length, current_page: 1, per_page: 50, last_page: 1 },
      }),
    ),
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

describe('the Schedule button over a selection', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it('appears when the selection holds a sliced file', async () => {
    mockLibrary([SLICED, RAW]);
    render(<FileManagerPage />);
    await screen.findByText('Benchy');

    await selectFile('Benchy');

    expect(await screen.findByRole('button', { name: /^Schedule$/i })).toBeInTheDocument();
  });

  it('stays away for a selection of only unsliced files', async () => {
    // The other half. A button rendered unconditionally would pass this alone,
    // which is why the pair is what makes either test mean anything.
    mockLibrary([SLICED, RAW]);
    render(<FileManagerPage />);
    await screen.findByText('bracket.stl');

    await selectFile('bracket.stl');

    // Something IS selected — the toolbar is on screen, just without this button.
    expect(await screen.findByRole('button', { name: /Deselect|Clear/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Schedule$/i })).not.toBeInTheDocument();
  });

  it('carries every sliced file in the selection into the run', async () => {
    // The whole point of the merge: this used to be gated on exactly one file
    // and a second button did the many-file case. One button, one dialog, N
    // files — so the counter has to show the size of the selection.
    mockLibrary([SLICED, { ...SLICED, id: 3, filename: 'cube.gcode.3mf', print_name: 'Cube' }, RAW]);
    render(<FileManagerPage />);
    await screen.findByText('Benchy');

    await selectFile('Benchy');
    await selectFile('Cube');
    await selectFile('bracket.stl');
    await userEvent.click(await screen.findByRole('button', { name: /^Schedule$/i }));

    // Two, not three — the STL is in the selection but cannot be printed.
    expect(await screen.findByText('1/2')).toBeInTheDocument();
  });

  it('leaves the undistributed files selected when the run is abandoned', async () => {
    // Closing the dialog stops the run. What was never queued has to stay
    // ticked — the selection is the only record on screen of what is left, and
    // clearing it would quietly lose the operator's place.
    mockLibrary([SLICED, { ...SLICED, id: 3, filename: 'cube.gcode.3mf', print_name: 'Cube' }]);
    render(<FileManagerPage />);
    await screen.findByText('Benchy');

    await selectFile('Benchy');
    await selectFile('Cube');
    await userEvent.click(await screen.findByRole('button', { name: /^Schedule$/i }));
    await screen.findByText('1/2');

    await userEvent.click(screen.getByRole('button', { name: /cancel/i }));

    expect(await screen.findByText('2 selected')).toBeInTheDocument();
  });
});
