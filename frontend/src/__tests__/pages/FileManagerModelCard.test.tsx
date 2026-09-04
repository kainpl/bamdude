/**
 * The model card entry in the File Manager's two per-file menus.
 *
 * ⚠️ **`.3mf` only, and `.3mf` is a TAG, not a `file_type`.** m035 collapsed a
 * sliced `.gcode.3mf` to `file_type: 'gcode'`, so gating on the column would
 * have hidden the card from most of a working farm's library — the sliced files
 * are the ones an operator has. Both containers carry the `3mf` file tag; an
 * STL carries neither the tag nor a card to read.
 *
 * The card itself is read-only (spec §Decisions 5): a library 3MF is somebody's
 * source of truth and BamDude never writes into one.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { FileManagerPage } from '../../pages/FileManagerPage';

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom');
  return { ...actual, useNavigate: () => vi.fn() };
});

const file = (over: Record<string, unknown>) => ({
  file_path: '/library/x',
  file_size: 1024,
  product_ids: [],
  folder_id: null,
  thumbnail_path: null,
  print_name: null,
  print_time_seconds: null,
  duplicate_count: 0,
  print_count: 0,
  tags: [],
  created_at: '2024-01-01T00:00:00Z',
  ...over,
});

const mockFiles = [
  file({ id: 1, filename: 'lamp.3mf', file_type: '3mf', file_tags: ['3mf', 'project'], print_name: 'Lamp' }),
  file({
    id: 2,
    filename: 'benchy.gcode.3mf',
    file_type: 'gcode',
    file_tags: ['gcode', '3mf'],
    print_name: 'Benchy',
  }),
  file({ id: 3, filename: 'bracket.stl', file_type: 'stl', file_tags: ['stl', 'geometry'], print_name: 'Bracket' }),
  // A raw sliced G-code with no 3MF container around it: printable, and there
  // is nothing in it to read a card from.
  file({ id: 4, filename: 'raw.gcode', file_type: 'gcode', file_tags: ['gcode'], print_name: 'Raw' }),
];

describe('File Manager — model card entry', () => {
  beforeEach(() => {
    localStorage.clear();
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
          total_files: 4,
          total_folders: 0,
          total_size_bytes: 3072,
          disk_free_bytes: 10737418240,
          disk_total_bytes: 107374182400,
        }),
      ),
      http.get('/api/v1/settings/', () =>
        HttpResponse.json({ check_updates: false, check_printer_firmware: false, library_disk_warning_gb: 5 }),
      ),
      http.get('/api/v1/projects/', () => HttpResponse.json([])),
      http.get('/api/v1/library/tags', () => HttpResponse.json([])),
      http.get('/api/v1/library/files/1/card', () =>
        HttpResponse.json({
          title: 'Desk lamp',
          description: null,
          designer: 'Ada',
          designer_user_id: null,
          license: null,
          copyright: null,
          creation_date: null,
          modification_date: null,
          origin: null,
          profile_title: null,
          profile_description: null,
          profile_cover: null,
          profile_user_id: null,
          profile_user_name: null,
          design_model_id: null,
          design_profile_id: null,
          design_region: null,
          auxiliaries: {},
          error: null,
        }),
      ),
    );
  });

  /** Open the ⋮ menu of the card whose title is `name`. Picked by its icon, not
   *  by position: "the last button in the card" is the selection target, and
   *  clicking that opens the multi-select toolbar instead. */
  const openMenuOf = async (name: string) => {
    const card = (await screen.findByText(name)).closest('.group') as HTMLElement;
    const trigger = within(card)
      .getAllByRole('button')
      .find((b) => b.querySelector('.lucide-ellipsis-vertical, .lucide-more-vertical'));
    if (!trigger) throw new Error('no ⋮ trigger found on the card');
    await userEvent.click(trigger);
  };

  it('offers the model card on an unsliced 3MF', async () => {
    render(<FileManagerPage />);
    await screen.findByText('Lamp');

    await openMenuOf('Lamp');
    expect(await screen.findByRole('button', { name: /^Model card$/i })).toBeInTheDocument();
  });

  it('offers it ENABLED — being able to list the files is being able to read them', async () => {
    // ⚠️ The entry used to carry a `library:read` branch that greyed it out,
    // and that branch was a LIVE BUG, not dead code — the component's own
    // comment says so. Both the listing and `GET /files/{id}/card` enforce
    // `library:read_all` / `library:read_own`; the legacy `library:read` is a
    // frontend gate nothing on this path asks for. A user holding only
    // `library:read_own` therefore listed the files, could read every card the
    // server would have handed them, and found this entry greyed out. Reading
    // the card needs exactly what LISTING the files needs, so the file type is
    // the only question left here.
    render(<FileManagerPage />);
    await screen.findByText('Lamp');

    await openMenuOf('Lamp');
    expect(await screen.findByRole('button', { name: /^Model card$/i })).toBeEnabled();
  });

  it('offers it on a SLICED 3MF too — the container is the same file', async () => {
    render(<FileManagerPage />);
    await screen.findByText('Benchy');

    await openMenuOf('Benchy');
    expect(await screen.findByRole('button', { name: /^Model card$/i })).toBeInTheDocument();
  });

  it('does not offer it on a raw .gcode — there is no 3MF container to read', async () => {
    render(<FileManagerPage />);
    await screen.findByText('Raw');

    await openMenuOf('Raw');
    expect(await screen.findByRole('button', { name: /^Download$/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Model card$/i })).not.toBeInTheDocument();
  });

  it('does not offer it on an STL, which has no card to read', async () => {
    render(<FileManagerPage />);
    await screen.findByText('Bracket');

    await openMenuOf('Bracket');
    // Something from this menu must be on screen, or the absence below proves
    // only that the menu never opened.
    expect(await screen.findByRole('button', { name: /^Download$/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Model card$/i })).not.toBeInTheDocument();
  });

  // ⚠️ The list mode is a DIFFERENT menu component (`FileListActions`, portal-
  // rendered from a row) with its own copy of every entry. A test that drove
  // only the card grid would have proved nothing about half the surface.
  describe('list mode', () => {
    beforeEach(() => {
      localStorage.setItem('library-view-mode', 'list');
    });

    /** Open the ⋮ menu of the ROW whose title is `name`. */
    const openRowMenuOf = async (name: string) => {
      const row = (await screen.findByText(name)).closest('[data-file-row]') as HTMLElement;
      const trigger = within(row)
        .getAllByRole('button')
        .find((b) => b.querySelector('.lucide-ellipsis-vertical, .lucide-more-vertical'));
      if (!trigger) throw new Error('no ⋮ trigger found on the row');
      await userEvent.click(trigger);
    };

    it('offers the model card on a 3MF row', async () => {
      render(<FileManagerPage />);
      await screen.findByText('Lamp');

      await openRowMenuOf('Lamp');
      expect(await screen.findByRole('button', { name: /^Model card$/i })).toBeInTheDocument();
    });

    it('does not offer it on an STL row', async () => {
      render(<FileManagerPage />);
      await screen.findByText('Bracket');

      await openRowMenuOf('Bracket');
      expect(await screen.findByRole('button', { name: /^Download$/i })).toBeInTheDocument();
      expect(screen.queryByRole('button', { name: /^Model card$/i })).not.toBeInTheDocument();
    });

    it('opens the card of the row whose menu was used', async () => {
      render(<FileManagerPage />);
      await screen.findByText('Lamp');

      await openRowMenuOf('Lamp');
      await userEvent.click(await screen.findByRole('button', { name: /^Model card$/i }));

      expect(await screen.findByText('Desk lamp')).toBeInTheDocument();
    });
  });

  it('opens the card of the file whose menu was used', async () => {
    render(<FileManagerPage />);
    await screen.findByText('Lamp');

    await openMenuOf('Lamp');
    await userEvent.click(await screen.findByRole('button', { name: /^Model card$/i }));

    expect(await screen.findByText('Desk lamp')).toBeInTheDocument();
    expect(screen.getByText('Ada')).toBeInTheDocument();
  });
});
