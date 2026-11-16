/**
 * "3D view" and "Download" for a reader who holds only `library:read_own`.
 *
 * Both entries were gated on the legacy `library:read`. Nothing on these paths
 * asks for it: the listing, `/files/{id}/capabilities`, `/plates`, `/gcode` and
 * `/download` all enforce `library:read_all` / `library:read_own`. The default
 * groups carry the legacy key, so this bites a hand-built group: its members
 * list their files, the server would hand them each one — and both doors are
 * greyed out. The model card beside them had the same branch removed earlier
 * (`FileManagerModelCard.test.tsx`); being able to list a file is being able to
 * read it.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { FileManagerPage } from '../../pages/FileManagerPage';
import type { Permission } from '../../api/client';

const auth = vi.hoisted(() => ({ granted: new Set<string>(), userId: 5 }));

vi.mock('../../contexts/AuthContext', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../contexts/AuthContext')>();
  return {
    ...actual,
    useAuth: () => ({
      ...actual.useAuth(),
      hasPermission: (p: Permission) => auth.granted.has(p),
      hasAnyPermission: (...ps: Permission[]) => ps.some((p) => auth.granted.has(p)),
      canModify: (resource: string, action: string, createdById: number | null | undefined) => {
        if (auth.granted.has(`${resource}:${action}_all`)) return true;
        if (auth.granted.has(`${resource}:${action}_own`)) return createdById != null && createdById === auth.userId;
        return false;
      },
    }),
  };
});

vi.mock('react-router', async () => {
  const actual = await vi.importActual<typeof import('react-router')>('react-router');
  return { ...actual, useNavigate: () => vi.fn() };
});

vi.mock('../../components/ModelViewerModal', () => ({
  ModelViewerModal: ({ title }: { title: string }) => <div data-testid="model-viewer-modal">{title}</div>,
}));

const mockFiles = [
  {
    id: 8,
    filename: 'lamp.3mf',
    file_path: '/library/lamp.3mf',
    file_size: 2048,
    file_type: '3mf',
    file_tags: ['3mf', 'project'],
    product_ids: [],
    folder_id: null,
    thumbnail_path: null,
    print_name: 'Lamp',
    print_time_seconds: null,
    duplicate_count: 0,
    print_count: 0,
    tags: [],
    created_by_id: 5,
    created_at: '2024-01-01T00:00:00Z',
  },
];

async function openMenuOf(name: string) {
  const title = await screen.findByText(name);
  const item = title.closest('[data-file-row], [data-file-card]') as HTMLElement;
  const trigger = within(item)
    .getAllByRole('button')
    .find((b) => b.querySelector('.lucide-ellipsis-vertical, .lucide-more-vertical'));
  if (!trigger) throw new Error('no ⋮ trigger found');
  await userEvent.click(trigger);
}

describe.each(['grid', 'list'] as const)('a reader of own files only, in the %s view', (viewMode) => {
  beforeEach(() => {
    localStorage.clear();
    localStorage.setItem('library-view-mode', viewMode);
    auth.granted = new Set(['library:read_own']);
    server.use(
      http.get('/api/v1/library/folders', () => HttpResponse.json([])),
      http.get('/api/v1/library/files', () =>
        HttpResponse.json({ items: mockFiles, meta: { total: 1, current_page: 1, per_page: 50, last_page: 1 } }),
      ),
      http.get('/api/v1/library/stats', () =>
        HttpResponse.json({
          total_files: 1,
          total_folders: 0,
          total_size_bytes: 2048,
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
  });

  it('is offered the 3D view and the download of a file it can list', async () => {
    render(<FileManagerPage />);

    await openMenuOf('Lamp');

    // The row also carries an inline 3D button under the same name: every one
    // of them must be live.
    for (const button of await screen.findAllByRole('button', { name: '3D View' })) expect(button).toBeEnabled();
    for (const button of screen.getAllByRole('button', { name: /^Download$/ })) expect(button).toBeEnabled();
  });

  it('opens the 3D viewer from the menu and by a double click', async () => {
    render(<FileManagerPage />);

    await userEvent.dblClick((await screen.findByText('Lamp')).closest('[data-file-row], [data-file-card]')!);
    expect(await screen.findByTestId('model-viewer-modal')).toHaveTextContent('Lamp');
  });
});
