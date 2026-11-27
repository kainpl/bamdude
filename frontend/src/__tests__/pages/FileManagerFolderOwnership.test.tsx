/**
 * Folders have an owner and can be shared (N56): the File Manager offers on a folder
 * exactly what the server's `can_*` flags say, marks a shared folder, and adds files
 * and folders only where the user may write — a folder they merely pass on the way to
 * a file of theirs is view-only. The server enforces the same rules.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
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

const folder = (id: number, name: string, flags: Record<string, unknown>) => ({
  id,
  name,
  parent_id: null,
  file_count: 0,
  products: [],
  children: [],
  is_external: false,
  external_path: null,
  external_readonly: false,
  latest_activity_at: null,
  created_by_id: null,
  shared: false,
  can_write: true,
  can_rename: false,
  can_share: false,
  can_delete: false,
  ...flags,
});

const TREE = [
  folder(1, 'Mine', { created_by_id: 5, can_rename: true, can_share: true, can_delete: true }),
  folder(2, 'For everyone', { created_by_id: 9, shared: true }),
  folder(3, 'Passage', { created_by_id: 9, can_write: false }),
];

let shareBodies: unknown[] = [];

async function openFolderMenu(name: string) {
  const row = (await screen.findByText(name)).closest('[data-folder-row]') as HTMLElement;
  const trigger = within(row)
    .getAllByRole('button')
    .find((b) => b.querySelector('.lucide-ellipsis-vertical, .lucide-more-vertical'));
  if (!trigger) throw new Error('no ⋮ trigger on the folder row');
  await userEvent.click(trigger);
  return row;
}

describe('a folder’s owner and sharing', () => {
  beforeEach(() => {
    localStorage.clear();
    shareBodies = [];
    auth.granted = new Set(['library:read_own', 'library:upload', 'library:update_own', 'library:delete_own']);
    server.use(
      http.get('/api/v1/library/folders', () => HttpResponse.json(TREE)),
      http.get('/api/v1/library/files', () =>
        HttpResponse.json({ items: [], meta: { total: 0, current_page: 1, per_page: 50, last_page: 1 } }),
      ),
      http.get('/api/v1/library/stats', () =>
        HttpResponse.json({
          total_files: 0,
          total_folders: 3,
          total_size_bytes: 0,
          disk_free_bytes: 10737418240,
          disk_total_bytes: 107374182400,
        }),
      ),
      http.get('/api/v1/settings/', () =>
        HttpResponse.json({ check_updates: false, check_printer_firmware: false, library_disk_warning_gb: 5 }),
      ),
      http.get('/api/v1/projects/', () => HttpResponse.json([])),
      http.get('/api/v1/library/tags', () => HttpResponse.json([])),
      http.put('/api/v1/library/folders/1', async ({ request }) => {
        shareBodies.push(await request.json());
        return HttpResponse.json({ ...TREE[0], shared: true });
      }),
    );
  });

  it('marks a shared folder', async () => {
    render(<FileManagerPage />);

    const shared = (await screen.findByText('For everyone')).closest('[data-folder-row]') as HTMLElement;
    const mine = screen.getByText('Mine').closest('[data-folder-row]') as HTMLElement;
    expect(within(shared).getByTestId('folder-shared-mark')).toBeInTheDocument();
    expect(within(mine).queryByTestId('folder-shared-mark')).not.toBeInTheDocument();
  });

  it('lets the owner share their folder', async () => {
    render(<FileManagerPage />);

    const row = await openFolderMenu('Mine');
    await userEvent.click(within(row).getByRole('button', { name: 'Share with everyone' }));

    await waitFor(() => expect(shareBodies).toEqual([{ shared: true }]));
  });

  it('offers nothing on someone else’s shared folder but saying why', async () => {
    render(<FileManagerPage />);

    const row = await openFolderMenu('For everyone');
    const rename = within(row).getByRole('button', { name: /Rename/ });
    expect(rename).toBeDisabled();
    expect(rename).toHaveAttribute('title', 'Only the folder’s owner can rename it');
    const move = within(row).getByRole('button', { name: /Move/ });
    expect(move).toBeDisabled();
    expect(move).toHaveAttribute('title', 'Only the folder’s owner can move it');
    expect(within(row).getByRole('button', { name: /Delete/ })).toBeDisabled();
    expect(within(row).queryByRole('button', { name: 'Share with everyone' })).not.toBeInTheDocument();
  });

  it('adds nothing to a folder the user only passes through', async () => {
    render(<FileManagerPage />);

    await userEvent.click(await screen.findByText('Passage'));

    await waitFor(() =>
      expect(screen.getByRole('button', { name: /^Upload$/ })).toHaveAttribute(
        'title',
        'You can add only to your own folders and folders shared with everyone',
      ),
    );
    expect(screen.getByRole('button', { name: /^Upload$/ })).toBeDisabled();
    expect(screen.getByRole('button', { name: /^New Folder$/i })).toBeDisabled();
  });
});
