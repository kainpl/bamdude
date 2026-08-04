/**
 * The library shows how many times a file has been printed.
 *
 * A separate file from FileManagerPage.test.tsx because the navigation
 * assertion needs a useNavigate spy, and vi.mock is scoped to a file — putting
 * it in the big suite would silently change routing for all forty tests there.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { FileManagerPage } from '../../pages/FileManagerPage';

const navigateSpy = vi.fn();
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom');
  return { ...actual, useNavigate: () => navigateSpy };
});

// Three files spanning the cases: printed several times, never printed, and
// printed once (which exercises the singular form and gives the click test a
// second printed file to be wrong about).
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
    created_at: '2024-01-02T00:00:00Z',
  },
  {
    id: 3,
    filename: 'cube.gcode.3mf',
    file_path: '/library/cube.gcode.3mf',
    file_size: 2048576,
    file_type: 'gcode',
    file_tags: ['gcode', '3mf', 'sliced'],
    folder_id: null,
    thumbnail_path: null,
    print_name: 'Cube',
    print_time_seconds: 1800,
    duplicate_count: 0,
    print_count: 1,
    created_at: '2024-01-03T00:00:00Z',
  },
];

describe('library print count', () => {
  beforeEach(() => {
    localStorage.clear();
    navigateSpy.mockClear();
    server.use(
      http.get('/api/v1/library/folders', () => HttpResponse.json([])),
      http.get('/api/v1/library/files', () => HttpResponse.json(mockFiles)),
      http.get('/api/v1/library/stats', () =>
        HttpResponse.json({
          total_files: 3,
          total_folders: 0,
          total_size_bytes: 3621440,
          disk_free_bytes: 10737418240,
          disk_total_bytes: 107374182400,
        }),
      ),
      http.get('/api/v1/settings/', () =>
        HttpResponse.json({ check_updates: false, check_printer_firmware: false, library_disk_warning_gb: 5 }),
      ),
      http.get('/api/v1/projects/', () => HttpResponse.json([])),
    );
  });

  it('shows how many times a file has been printed', async () => {
    render(<FileManagerPage />);

    expect(await screen.findByRole('button', { name: 'Printed 4 times' })).toBeInTheDocument();
  });

  it('uses the singular form for a file printed once', async () => {
    // Not cosmetic: i18next renders the raw key when a plural form is missing,
    // so "fileManager.printedTimes_one" on screen is the failure mode.
    render(<FileManagerPage />);

    expect(await screen.findByRole('button', { name: 'Printed once' })).toBeInTheDocument();
  });

  it('says nothing for a file that has never been printed', async () => {
    // Most of a library is unprinted; a badge on every row would be noise, and
    // the filter answers "never printed" instead.
    render(<FileManagerPage />);
    await screen.findByText('Benchy');

    expect(screen.queryByRole('button', { name: /Printed 0/ })).not.toBeInTheDocument();
  });

  it('opens the archives for the file whose chip was clicked', async () => {
    // With two printed files the wrong target is a plausible bug and an
    // invisible one: both open an archive view, and it looks right.
    render(<FileManagerPage />);

    await userEvent.click(await screen.findByRole('button', { name: 'Printed once' }));

    await waitFor(() => expect(navigateSpy).toHaveBeenCalledWith('/archives?file=3&fileName=Cube'));
  });
});
