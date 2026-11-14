/**
 * "Assemble project" from the file manager: the selection bar's button (models only, with the upload right)
 * and "Re-layout" in a file's menu — offered for a project assembled here and for nothing else.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';

import type { Permission } from '../../api/client';
import { SliceJobTrackerProvider } from '../../contexts/SliceJobTrackerContext';
import { FileManagerPage } from '../../pages/FileManagerPage';
import { server } from '../mocks/server';
import { render } from '../utils';

const auth = vi.hoisted(() => ({ granted: new Set<string>(), userId: 5 }));

vi.mock('../../contexts/AuthContext', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../contexts/AuthContext')>();
  return {
    ...actual,
    useAuth: () => ({
      ...actual.useAuth(),
      hasPermission: (p: Permission) => auth.granted.has(p),
      hasAnyPermission: (...ps: Permission[]) => ps.some((p) => auth.granted.has(p)),
      canModify: (resource: string, action: string) => auth.granted.has(`${resource}:${action}_all`),
    }),
  };
});

const file = (id: number, name: string, type: string, over: Record<string, unknown> = {}) => ({
  id,
  filename: name,
  file_path: `/library/${name}`,
  file_size: 1024,
  file_type: type,
  file_tags: [type],
  plan_eligible: false,
  product_ids: [],
  folder_id: null,
  thumbnail_path: null,
  print_name: null,
  print_time_seconds: null,
  duplicate_count: 0,
  created_at: '2026-11-01T00:00:00Z',
  created_by_id: 5,
  ...over,
});

const FILES = [
  file(1, 'jar.stl', 'stl'),
  file(2, 'lid.obj', 'obj'),
  file(3, 'old.3mf', '3mf'),
  file(4, 'jars — X1C.3mf', '3mf', { can_relayout: true, source_type: 'assembled' }),
  file(5, 'bracket.step', 'step'),
];
const BASE = ['library:read_all', 'library:update_all'];
const EMPTY = { printer: [], process: [], filament: [] };

function show(selected: string) {
  window.history.replaceState({}, '', `/files?selected=${selected}`);
  return render(
    <SliceJobTrackerProvider>
      <FileManagerPage />
    </SliceJobTrackerProvider>,
  );
}

function cardOf(name: string): HTMLElement {
  return screen.getByText(name).closest('.group') as HTMLElement;
}

describe('FileManagerPage — "Assemble project"', () => {
  beforeEach(() => {
    localStorage.clear();
    auth.granted = new Set([...BASE, 'library:upload']);
    server.use(
      http.get('/api/v1/library/folders', () => HttpResponse.json([])),
      http.get('/api/v1/library/files', () =>
        HttpResponse.json({ items: FILES, meta: { total: 4, current_page: 1, per_page: 50, last_page: 1 } }),
      ),
      http.get('/api/v1/library/stats', () =>
        HttpResponse.json({ total_files: 4, total_folders: 0, total_size_bytes: 1, disk_free_bytes: 1, disk_total_bytes: 2 }),
      ),
      http.get('/api/v1/settings/', () => HttpResponse.json({ check_updates: false, check_printer_firmware: false })),
      http.get('/api/v1/slicer/presets', () =>
        HttpResponse.json({ local: EMPTY, orca_cloud: EMPTY, cloud: EMPTY, standard: EMPTY }),
      ),
      http.get('/api/v1/slicer/printer-models', () => HttpResponse.json({})),
    );
  });

  it('opens the dialog on the selected models', async () => {
    show('1,2');
    const button = await screen.findByRole('button', { name: 'Assemble project' });
    expect(button).toBeEnabled();
    fireEvent.click(button);
    const dialog = await screen.findByRole('dialog', { name: 'Assemble project' });
    expect(await within(dialog).findByText('jar.stl')).toBeInTheDocument();
    expect(within(dialog).getByText('lid.obj')).toBeInTheDocument();
  });

  it('is disabled, with the reason, when the selection holds another kind of file', async () => {
    show('1,3');
    const button = await screen.findByRole('button', { name: 'Assemble project' });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute('title', expect.stringContaining('Only STL, OBJ and STEP models'));
  });

  it('takes a STEP model like a mesh', async () => {
    show('1,5');
    const button = await screen.findByRole('button', { name: 'Assemble project' });
    expect(button).toBeEnabled();
    fireEvent.click(button);
    const dialog = await screen.findByRole('dialog', { name: 'Assemble project' });
    expect(await within(dialog).findByText('bracket.step')).toBeInTheDocument();
  });

  it('is absent without the right to upload', async () => {
    auth.granted = new Set(BASE);
    show('1,2');
    await screen.findByText('jar.stl');
    await screen.findByRole('button', { name: /Move/ });
    expect(screen.queryByRole('button', { name: 'Assemble project' })).not.toBeInTheDocument();
  });

  it('offers "Re-layout" only on a project assembled here', async () => {
    server.use(
      http.get('/api/v1/library/files/4/assembly', () =>
        HttpResponse.json(
          { detail: { error: 'not_assembled', message: 'This file was not assembled here, or its record is damaged' } },
          { status: 409 },
        ),
      ),
    );
    show('');
    await screen.findByText('old.3mf');
    fireEvent.click(within(cardOf('old.3mf')).getByRole('button', { name: 'File actions' }));
    await screen.findByRole('button', { name: /Download/ });
    expect(screen.queryByRole('button', { name: 'Re-layout' })).not.toBeInTheDocument();
    fireEvent.keyDown(document, { key: 'Escape' });

    fireEvent.click(within(cardOf('jars — X1C.3mf')).getByRole('button', { name: 'File actions' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Re-layout' }));
    expect(await screen.findByRole('dialog', { name: 'Re-layout project' })).toBeInTheDocument();
  });

  it('does not offer "Re-layout" without the right to upload', async () => {
    auth.granted = new Set(BASE);
    show('');
    await screen.findByText('jars — X1C.3mf');
    fireEvent.click(within(cardOf('jars — X1C.3mf')).getByRole('button', { name: 'File actions' }));
    await screen.findByRole('button', { name: /Download/ });
    expect(screen.queryByRole('button', { name: 'Re-layout' })).not.toBeInTheDocument();
  });

  it('offers "Re-layout" from the list row’s menu too', async () => {
    localStorage.setItem('library-view-mode', 'list');
    show('');
    const row = (await screen.findByText('jars — X1C.3mf')).closest('[data-file-row]') as HTMLElement;
    fireEvent.click(within(row).getByRole('button', { name: 'File actions' }));
    expect(await screen.findByRole('button', { name: 'Re-layout' })).toBeInTheDocument();
  });
});
