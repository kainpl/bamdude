/**
 * Linking a library file or folder to products is the order desk's business too
 * (WS-13 E13 B01/B06): the file manager offers it only with the library right AND
 * `products:update`, and a move that would change which products a file belongs to
 * (a folder brings its own set; the root has none) is not offered without it — the
 * dialog says why. The server stays the final gate.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { QueryClient } from '@tanstack/react-query';
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

const P1 = { id: 7, name: 'Lamp', is_active: true };
const P2 = { id: 8, name: 'Vase', is_active: true };

const FOLDERS = [
  { id: 1, name: 'Loose folder', parent_id: null, file_count: 0, products: [], children: [] },
  { id: 2, name: 'Lamp folder', parent_id: null, file_count: 0, products: [P1], children: [] },
  { id: 3, name: 'Vase folder', parent_id: null, file_count: 0, products: [P2], children: [] },
];

const file = (id: number, name: string, productIds: number[]) => ({
  id,
  filename: `${name}.gcode.3mf`,
  file_path: `/library/${name}.gcode.3mf`,
  file_size: 1024,
  file_type: 'gcode',
  file_tags: ['gcode', '3mf', 'sliced'],
  product_ids: productIds,
  folder_id: null,
  thumbnail_path: null,
  print_name: name,
  print_time_seconds: 60,
  duplicate_count: 0,
  created_at: '2026-10-01T00:00:00Z',
  created_by_id: 5,
});

const FILES = [file(1, 'Linked', [P1.id]), file(2, 'Loose', [])];
const LIBRARY = ['library:read_all', 'library:upload', 'library:update_all', 'library:delete_all'];

async function linkExternalButton() {
  const label = await screen.findByText('Link External');
  return label.closest('button') as HTMLButtonElement;
}

async function openFolderMenu(name: string) {
  const row = screen.getByText(name).closest('.group') as HTMLElement;
  const buttons = within(row).getAllByRole('button');
  fireEvent.click(buttons[buttons.length - 1]);
  return screen.findByRole('button', { name: /^(Link to\.\.\.|Change Link\.\.\.)$/ });
}

async function openMoveFor(name: string) {
  const card = screen.getByText(name).closest('.group') as HTMLElement;
  fireEvent.click(within(card).getByLabelText('Select file'));
  fireEvent.click(await screen.findByRole('button', { name: 'Move' }));
  // The title counts in the language's plural forms (WS-13 E13 T9: «1 файлів» on the frame).
  return screen.findByRole('dialog', { name: 'Move 1 file' });
}

// The files sit at the root, which the dialog already marks «current» — the empty set
// of products is therefore the loose folder's.
const target = (dialog: HTMLElement, name: string) =>
  within(dialog).getByText(name).closest('button') as HTMLButtonElement;

describe('FileManagerPage — links to products ask the right to change orders (E13 B06)', () => {
  beforeEach(() => {
    localStorage.clear();
    window.history.replaceState({}, '', '/files');
    server.use(
      http.get('/api/v1/library/folders', () => HttpResponse.json(FOLDERS)),
      http.get('/api/v1/library/files', () =>
        HttpResponse.json({ items: FILES, meta: { total: 2, current_page: 1, per_page: 50, last_page: 1 } }),
      ),
      http.get('/api/v1/library/stats', () =>
        HttpResponse.json({ total_files: 2, total_folders: 3, total_size_bytes: 1, disk_free_bytes: 1, disk_total_bytes: 2 }),
      ),
      http.get('/api/v1/settings/', () => HttpResponse.json({ check_updates: false, check_printer_firmware: false })),
    );
  });

  it('offers no link to products with the library right alone, and keeps the links readable', async () => {
    auth.granted = new Set(LIBRARY);
    render(<FileManagerPage />);
    await screen.findByText('Linked');
    expect(screen.queryAllByTitle('Link to products')).toHaveLength(0);
    const badge = screen.getByTitle('Linked to 1 product');
    expect(badge.tagName).not.toBe('BUTTON');
    expect(screen.getByTitle('Lamp').tagName).not.toBe('BUTTON');
    const item = await openFolderMenu('Loose folder');
    expect(item).toBeDisabled();
    expect(item).toHaveAttribute('title', 'Linking to products needs the right to change products');
  });

  it('offers it with the right to change orders too', async () => {
    auth.granted = new Set([...LIBRARY, 'orders:update', 'products:update', 'customers:update', 'stock:move', 'stock:adjust']);
    render(<FileManagerPage />);
    await screen.findByText('Linked');
    expect(screen.queryAllByTitle('Link to products').length).toBeGreaterThan(0);
    expect(screen.getByTitle('Linked to 1 product (click to manage)').tagName).toBe('BUTTON');
    expect(await openFolderMenu('Loose folder')).not.toBeDisabled();
  });

  it('does not offer a move that would change a file’s products, and says why', async () => {
    auth.granted = new Set(LIBRARY);
    render(<FileManagerPage />);
    await screen.findByText('Linked');
    const dialog = await openMoveFor('Linked');
    expect(target(dialog, 'Lamp folder')).not.toBeDisabled(); // same products: library work
    expect(target(dialog, 'Vase folder')).toBeDisabled();
    expect(target(dialog, 'Loose folder')).toBeDisabled();
    expect(dialog).toHaveTextContent(
      'A folder marked «changes products» would change which products these files belong to — that needs the right to change orders.',
    );
  });

  it('offers every move with the right to change orders', async () => {
    auth.granted = new Set([...LIBRARY, 'orders:update', 'products:update', 'customers:update', 'stock:move', 'stock:adjust']);
    render(<FileManagerPage />);
    await screen.findByText('Linked');
    const dialog = await openMoveFor('Linked');
    await waitFor(() => expect(target(dialog, 'Vase folder')).not.toBeDisabled());
    expect(target(dialog, 'Loose folder')).not.toBeDisabled();
    expect(dialog).not.toHaveTextContent('changes products');
  });

  // WS-13 E13 final review #3: a move can relink files (it changes their products), so the
  // catalog's figures and a product's files are read again — as a link from the dialog does.
  it('refreshes the product catalog and the products’ files after a move', async () => {
    auth.granted = new Set([...LIBRARY, 'orders:update', 'products:update', 'customers:update', 'stock:move', 'stock:adjust']);
    server.use(http.post('/api/v1/library/files/move', () => HttpResponse.json({ status: 'ok', moved: 1 })));
    const invalidate = vi.spyOn(QueryClient.prototype, 'invalidateQueries');
    render(<FileManagerPage />);
    await screen.findByText('Linked');
    const dialog = await openMoveFor('Linked');
    await waitFor(() => expect(target(dialog, 'Vase folder')).not.toBeDisabled());
    fireEvent.click(target(dialog, 'Vase folder'));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Move' }));
    const keys = () => invalidate.mock.calls.map(([filters]) => JSON.stringify((filters as { queryKey?: unknown })?.queryKey));
    await waitFor(() => expect(keys()).toContain(JSON.stringify(['products'])));
    expect(keys()).toContain(JSON.stringify(['product-file-groups']));
    invalidate.mockRestore();
  });

  it('lets a file with no products move into a folder with none without the right', async () => {
    auth.granted = new Set(LIBRARY);
    render(<FileManagerPage />);
    await screen.findByText('Loose');
    const dialog = await openMoveFor('Loose');
    expect(target(dialog, 'Loose folder')).not.toBeDisabled();
    expect(target(dialog, 'Lamp folder')).toBeDisabled();
  });
});

// G11: mounting a directory of the host is an administrator's act — the server asks
// `settings:update`, so the button follows that right and says why it is off.
describe('FileManagerPage — linking an external folder asks the right to change settings', () => {
  beforeEach(() => {
    localStorage.clear();
    window.history.replaceState({}, '', '/files');
    server.use(
      http.get('/api/v1/library/folders', () => HttpResponse.json(FOLDERS)),
      http.get('/api/v1/library/files', () =>
        HttpResponse.json({ items: FILES, meta: { total: 2, current_page: 1, per_page: 50, last_page: 1 } }),
      ),
      http.get('/api/v1/library/stats', () =>
        HttpResponse.json({ total_files: 2, total_folders: 3, total_size_bytes: 1, disk_free_bytes: 1, disk_total_bytes: 2 }),
      ),
      http.get('/api/v1/settings/', () => HttpResponse.json({ check_updates: false, check_printer_firmware: false })),
    );
  });

  it('is off, with the reason, for someone who may only upload', async () => {
    auth.granted = new Set(LIBRARY);
    render(<FileManagerPage />);
    const button = await linkExternalButton();
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute('title', 'Linking a folder of the host needs the permission to change settings');
  });

  it('is offered with the right to change settings', async () => {
    auth.granted = new Set([...LIBRARY, 'settings:update']);
    render(<FileManagerPage />);
    const button = await linkExternalButton();
    expect(button).not.toBeDisabled();
    expect(button).toHaveAttribute('title', 'Link External Folder');
  });
});
