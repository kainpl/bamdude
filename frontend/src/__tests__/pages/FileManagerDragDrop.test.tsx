/**
 * Moving by drag-and-drop in the File Manager.
 *
 * A folder dragged onto another folder (or onto "All Files", the root) is
 * moved; so are files dragged onto a folder — through the endpoints and the
 * rules the dialogs already use. A target the server would refuse is marked
 * "not allowed" while the drag is over it. The drag carries MIME types of its
 * own, so it never opens the page's upload overlay, and a file dropped from the
 * operating system still uploads exactly as before.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { FileManagerPage } from '../../pages/FileManagerPage';
import type { Permission } from '../../api/client';

const auth = vi.hoisted(() => ({ denied: new Set<string>() }));

vi.mock('../../contexts/AuthContext', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../contexts/AuthContext')>();
  return {
    ...actual,
    useAuth: () => ({
      ...actual.useAuth(),
      hasPermission: (p: Permission) => !auth.denied.has(p),
      hasAnyPermission: (...ps: Permission[]) => ps.some((p) => !auth.denied.has(p)),
      canModify: (resource: string, action: string) =>
        !auth.denied.has(`${resource}:${action}_all`) || !auth.denied.has(`${resource}:${action}_own`),
    }),
  };
});

vi.mock('react-router', async () => {
  const actual = await vi.importActual<typeof import('react-router')>('react-router');
  return { ...actual, useNavigate: () => vi.fn() };
});

vi.mock('../../components/FileUploadModal', () => ({
  FileUploadModal: ({ initialFiles }: { initialFiles?: File[] }) => (
    <div data-testid="upload-modal">{(initialFiles ?? []).map((f) => f.name).join(',')}</div>
  ),
}));

const folder = (id: number, name: string, parent_id: number | null, children: unknown[] = [], extra: Record<string, unknown> = {}) => ({
  id,
  name,
  parent_id,
  is_external: false,
  external_readonly: false,
  external_path: null,
  file_count: 0,
  latest_activity_at: null,
  products: [] as { id: number; name: string }[],
  children,
  ...extra,
});

const tree = [
  folder(1, 'Parts', null, [folder(2, 'Brackets', 1, [folder(3, 'Small', 2)])]),
  folder(4, 'Spares', null),
  folder(5, 'NAS', null, [], { is_external: true, external_path: '/mnt/nas' }),
  folder(7, 'Lamp files', null, [], { products: [{ id: 9, name: 'Lamp' }] }),
];

const fileBase = {
  file_path: '/library/x',
  file_size: 2048,
  product_ids: [] as number[],
  folder_id: null,
  thumbnail_path: null,
  print_time_seconds: null,
  duplicate_count: 0,
  print_count: 0,
  tags: [],
  file_tags: [],
  print_name: null,
  created_by_id: 1,
  created_at: '2024-01-01T00:00:00Z',
};
const files = [
  { ...fileBase, id: 11, filename: 'a.stl', file_type: 'stl' },
  { ...fileBase, id: 12, filename: 'b.stl', file_type: 'stl' },
  { ...fileBase, id: 13, filename: 'c.stl', file_type: 'stl' },
];

let puts: Array<{ id: string; body: unknown }> = [];
let fileMoves: unknown[] = [];
let refusal: { status: number; detail: unknown } | null = null;

function mockLibrary() {
  server.use(
    http.get('/api/v1/library/folders', () => HttpResponse.json(tree)),
    http.put('/api/v1/library/folders/:id', async ({ request, params }) => {
      puts.push({ id: String(params.id), body: await request.json() });
      if (refusal) return HttpResponse.json({ detail: refusal.detail }, { status: refusal.status });
      return HttpResponse.json({ id: Number(params.id), name: 'x', parent_id: null, products: [] });
    }),
    http.post('/api/v1/library/files/move', async ({ request }) => {
      fileMoves.push(await request.json());
      return HttpResponse.json({ status: 'ok', moved: 1 });
    }),
    http.get('/api/v1/library/files', () =>
      HttpResponse.json({ items: files, meta: { total: files.length, current_page: 1, per_page: 50, last_page: 1 } }),
    ),
    http.get('/api/v1/library/stats', () =>
      HttpResponse.json({ total_files: 3, total_folders: 6, total_size_bytes: 0, disk_free_bytes: 10737418240, disk_total_bytes: 107374182400 }),
    ),
    http.get('/api/v1/settings/', () => HttpResponse.json({ library_disk_warning_gb: 5 })),
    http.get('/api/v1/library/tags', () => HttpResponse.json([])),
  );
}

/** What a browser hands the handlers of one drag: the types are readable throughout, the data is not needed. */
function transfer(initialTypes: string[] = [], osFiles: File[] = []) {
  const data: Record<string, string> = {};
  const types = [...initialTypes];
  return {
    types,
    files: osFiles,
    dropEffect: 'none',
    effectAllowed: 'all',
    setData(type: string, value: string) {
      data[type] = value;
      if (!types.includes(type)) types.push(type);
    },
    getData: (type: string) => data[type] ?? '',
  };
}

async function treeRow(name: string): Promise<HTMLElement> {
  const label = await screen.findByText(name, { selector: 'span' });
  const row = label.closest('[data-folder-row]') as HTMLElement | null;
  if (!row) throw new Error(`no tree row for ${name}`);
  return row;
}

async function rootRow(): Promise<HTMLElement> {
  await treeRow('Spares');
  return document.querySelector('[data-folder-root]') as HTMLElement;
}

async function fileItem(name: string): Promise<HTMLElement> {
  const title = await screen.findByText(name);
  return title.closest('[data-file-row], [data-file-card]') as HTMLElement;
}

async function expand(name: string) {
  const row = await treeRow(name);
  await userEvent.click(within(row).getAllByRole('button')[0]);
}

/** Drag `source` over `target`; returns the transfer so the test can read `dropEffect`. */
function dragOver(source: HTMLElement, target: HTMLElement) {
  const dataTransfer = transfer();
  fireEvent.dragStart(source, { dataTransfer });
  fireEvent.dragEnter(target, { dataTransfer });
  fireEvent.dragOver(target, { dataTransfer });
  return dataTransfer;
}

const uploadOverlay = () => screen.queryByText('Drop files to upload');

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem('library-view-mode', 'list');
  auth.denied = new Set();
  puts = [];
  fileMoves = [];
  refusal = null;
  window.history.pushState({}, '', '/files');
  mockLibrary();
});

describe('dragging a folder', () => {
  it('moves it under the folder it is dropped on', async () => {
    render(<FileManagerPage />);
    await expand('Parts');
    const target = await treeRow('Spares');
    const dataTransfer = dragOver(await treeRow('Brackets'), target);

    expect(dataTransfer.types).toEqual(['application/x-bamdude-folder']);
    expect(dataTransfer.dropEffect).toBe('move');
    expect(target).toHaveAttribute('data-drop-state', 'legal');

    fireEvent.drop(target, { dataTransfer });
    await waitFor(() => expect(puts).toEqual([{ id: '2', body: { parent_id: 4 } }]));
    expect(target).not.toHaveAttribute('data-drop-state');
  });

  it('moves it to the root when dropped on "All Files"', async () => {
    render(<FileManagerPage />);
    await expand('Parts');
    const root = await rootRow();
    const dataTransfer = dragOver(await treeRow('Brackets'), root);

    expect(dataTransfer.dropEffect).toBe('move');
    fireEvent.drop(root, { dataTransfer });
    await waitFor(() => expect(puts).toEqual([{ id: '2', body: { parent_id: 0 } }]));
  });

  it.each([
    ['an external folder', 'NAS'],
    ['its own subfolder', 'Small'],
    ['the parent it already has', 'Parts'],
  ])('marks %s as not allowed and moves nothing', async (_what, name) => {
    render(<FileManagerPage />);
    await expand('Parts');
    await expand('Brackets');
    const target = await treeRow(name);
    const dataTransfer = dragOver(await treeRow('Brackets'), target);

    expect(dataTransfer.dropEffect).toBe('none');
    expect(target).toHaveAttribute('data-drop-state', 'illegal');

    fireEvent.drop(target, { dataTransfer });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(puts).toEqual([]);
  });

  it('says the sentence of the server’s refusal', async () => {
    refusal = { status: 400, detail: { error: 'folder_parent_external', message: 'server prose' } };
    render(<FileManagerPage />);
    await expand('Parts');
    const target = await treeRow('Spares');
    const dataTransfer = dragOver(await treeRow('Brackets'), target);
    fireEvent.drop(target, { dataTransfer });

    expect(await screen.findByText('A folder cannot be moved into an external folder.')).toBeInTheDocument();
    expect(screen.queryByText('server prose')).not.toBeInTheDocument();
  });

  it('opens a collapsed folder the drag rests on', async () => {
    render(<FileManagerPage />);
    const parts = await treeRow('Parts');
    expect(screen.queryByText('Brackets')).not.toBeInTheDocument();

    dragOver(await treeRow('Spares'), parts);
    // Not at once — only after the pointer has rested there.
    expect(screen.queryByText('Brackets')).not.toBeInTheDocument();
    expect(await screen.findByText('Brackets', {}, { timeout: 2000 })).toBeInTheDocument();
  });

  it('is cancelled by Escape', async () => {
    render(<FileManagerPage />);
    await expand('Parts');
    const target = await treeRow('Spares');
    const dataTransfer = dragOver(await treeRow('Brackets'), target);

    fireEvent.keyDown(document, { key: 'Escape' });
    expect(target).not.toHaveAttribute('data-drop-state');
    fireEvent.drop(target, { dataTransfer });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(puts).toEqual([]);
  });

  it('is not offered for an external folder, nor without the right to move folders', async () => {
    const { unmount } = render(<FileManagerPage />);
    expect(await treeRow('Spares')).toHaveAttribute('draggable', 'true');
    expect(await treeRow('NAS')).not.toHaveAttribute('draggable');
    unmount();

    // N56: the owner moves their own folder too; neither right, nothing moves.
    auth.denied = new Set(['library:update_all', 'library:update_own']);
    render(<FileManagerPage />);
    expect(await treeRow('Spares')).not.toHaveAttribute('draggable');
  });

  it('is not offered on a touch device', async () => {
    const original = window.matchMedia;
    window.matchMedia = ((query: string) => ({
      ...original(query),
      matches: query === '(pointer: coarse)',
    })) as typeof window.matchMedia;
    try {
      render(<FileManagerPage />);
      expect(await treeRow('Spares')).not.toHaveAttribute('draggable');
      expect(await fileItem('a.stl')).not.toHaveAttribute('draggable');
    } finally {
      window.matchMedia = original;
    }
  });
});

describe.each(['grid', 'list'] as const)('dragging files in the %s view', (viewMode) => {
  beforeEach(() => localStorage.setItem('library-view-mode', viewMode));

  it('moves the dragged file into the folder', async () => {
    render(<FileManagerPage />);
    const target = await treeRow('Spares');
    const dataTransfer = dragOver(await fileItem('a.stl'), target);

    expect(dataTransfer.types).toEqual(['application/x-bamdude-files']);
    expect(dataTransfer.dropEffect).toBe('move');
    fireEvent.drop(target, { dataTransfer });

    await waitFor(() => expect(fileMoves).toEqual([{ file_ids: [11], folder_id: 4 }]));
  });

  it('moves the whole selection when the dragged file is part of it', async () => {
    render(<FileManagerPage />);
    await userEvent.click(within(await fileItem('a.stl')).getByRole('button', { name: 'Select file' }));
    await userEvent.click(within(await fileItem('b.stl')).getByRole('button', { name: 'Select file' }));

    const target = await treeRow('Spares');
    const dataTransfer = dragOver(await fileItem('b.stl'), target);
    fireEvent.drop(target, { dataTransfer });

    await waitFor(() => expect(fileMoves).toEqual([{ file_ids: [11, 12], folder_id: 4 }]));
  });

  it('does not offer the place the files already are', async () => {
    render(<FileManagerPage />);
    const root = await rootRow();
    const dataTransfer = dragOver(await fileItem('a.stl'), root);

    expect(dataTransfer.dropEffect).toBe('none');
    expect(root).toHaveAttribute('data-drop-state', 'illegal');
  });

  it('does not offer a folder that would change the files’ products without the right to', async () => {
    auth.denied = new Set(['products:update']);
    render(<FileManagerPage />);
    const target = await treeRow('Lamp files');
    const dataTransfer = dragOver(await fileItem('a.stl'), target);

    expect(dataTransfer.dropEffect).toBe('none');
    fireEvent.drop(target, { dataTransfer });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(fileMoves).toEqual([]);
  });

  it('offers that folder to someone who may change products — the server decides', async () => {
    render(<FileManagerPage />);
    const target = await treeRow('Lamp files');
    const dataTransfer = dragOver(await fileItem('a.stl'), target);

    expect(dataTransfer.dropEffect).toBe('move');
    fireEvent.drop(target, { dataTransfer });
    await waitFor(() => expect(fileMoves).toEqual([{ file_ids: [11], folder_id: 7 }]));
  });
});

describe('the upload drop zone beside it', () => {
  it('stays closed for a drag of our own, whatever else the browser adds to it', async () => {
    render(<FileManagerPage />);
    const item = await fileItem('a.stl');
    // A dragged thumbnail is an image: browsers add `Files` to such a drag.
    const dataTransfer = transfer(['Files']);
    fireEvent.dragStart(item, { dataTransfer });
    fireEvent.dragEnter(await fileItem('b.stl'), { dataTransfer });
    fireEvent.dragOver(await fileItem('b.stl'), { dataTransfer });

    expect(uploadOverlay()).not.toBeInTheDocument();
    fireEvent.drop(await fileItem('b.stl'), { dataTransfer });
    expect(screen.queryByTestId('upload-modal')).not.toBeInTheDocument();
    expect(fileMoves).toEqual([]);
  });

  it('still takes a file dropped from the operating system', async () => {
    render(<FileManagerPage />);
    const item = await fileItem('a.stl');
    const dropped = new File(['solid'], 'new-part.stl');
    const dataTransfer = transfer(['Files'], [dropped]);

    fireEvent.dragEnter(item, { dataTransfer });
    expect(uploadOverlay()).toBeInTheDocument();
    fireEvent.dragOver(item, { dataTransfer });
    expect(dataTransfer.dropEffect).toBe('copy');
    fireEvent.drop(item, { dataTransfer });

    expect(await screen.findByTestId('upload-modal')).toHaveTextContent('new-part.stl');
    expect(uploadOverlay()).not.toBeInTheDocument();
  });

  it('does not move anything when an operating-system file lands on a folder of the tree', async () => {
    render(<FileManagerPage />);
    const target = await treeRow('Spares');
    const dataTransfer = transfer(['Files'], [new File(['x'], 'x.stl')]);

    fireEvent.dragEnter(target, { dataTransfer });
    fireEvent.dragOver(target, { dataTransfer });
    expect(target).not.toHaveAttribute('data-drop-state');
    fireEvent.drop(target, { dataTransfer });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(puts).toEqual([]);
    expect(fileMoves).toEqual([]);
  });
});
