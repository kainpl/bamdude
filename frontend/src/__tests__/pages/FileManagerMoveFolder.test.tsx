/**
 * Moving a folder from its menu.
 *
 * "Move…" opens a tree of targets. What the server would refuse is shown
 * disabled with the reason — the folder itself, its own subfolders, every
 * external folder — and the parent it already has reads "already here". The
 * server stays the authority: each refusal code has its own sentence, and a
 * refused move leaves the dialog open.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
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
      canModify: () => true,
    }),
  };
});

vi.mock('react-router', async () => {
  const actual = await vi.importActual<typeof import('react-router')>('react-router');
  return { ...actual, useNavigate: () => vi.fn() };
});

type Folder = {
  id: number;
  name: string;
  parent_id: number | null;
  is_external: boolean;
  external_readonly: boolean;
  external_path: string | null;
  file_count: number;
  latest_activity_at: null;
  products: { id: number; name: string }[];
  children: Folder[];
};

const folder = (id: number, name: string, parent_id: number | null, children: Folder[] = [], extra: Partial<Folder> = {}): Folder => ({
  id,
  name,
  parent_id,
  is_external: false,
  external_readonly: false,
  external_path: null,
  file_count: 0,
  latest_activity_at: null,
  products: [],
  children,
  ...extra,
});

const initialTree = (): Folder[] => [
  folder(1, 'Parts', null, [folder(2, 'Brackets', 1, [folder(3, 'Small', 2)])]),
  folder(4, 'Spares', null, [], { products: [{ id: 9, name: 'Lamp' }] }),
  folder(5, 'NAS', null, [folder(6, 'Scans', 5, [], { is_external: true })], { is_external: true, external_path: '/mnt/nas' }),
];

let tree: Folder[] = [];
let puts: Array<{ id: string; body: Record<string, unknown> }> = [];
let refusal: { status: number; detail: unknown } | null = null;

/** Detach `id` and hang it under `parent` (null = root) — what the server does. */
function moveInTree(id: number, parent: number | null) {
  let moved: Folder | null = null;
  const detach = (items: Folder[]): Folder[] =>
    items.flatMap((item) => {
      if (item.id === id) {
        moved = item;
        return [];
      }
      return [{ ...item, children: detach(item.children) }];
    });
  let next = detach(tree);
  if (!moved) return;
  const node: Folder = { ...(moved as Folder), parent_id: parent };
  if (parent === null) next = [...next, node];
  else {
    const attach = (items: Folder[]): Folder[] =>
      items.map((item) => (item.id === parent ? { ...item, children: [...item.children, node] } : { ...item, children: attach(item.children) }));
    next = attach(next);
  }
  tree = next;
}

function mockLibrary() {
  server.use(
    http.get('/api/v1/library/folders', () => HttpResponse.json(tree)),
    http.put('/api/v1/library/folders/:id', async ({ request, params }) => {
      const body = (await request.json()) as Record<string, unknown>;
      puts.push({ id: String(params.id), body });
      if (refusal) return HttpResponse.json({ detail: refusal.detail }, { status: refusal.status });
      if (typeof body.parent_id === 'number') moveInTree(Number(params.id), body.parent_id || null);
      return HttpResponse.json({ id: Number(params.id), name: 'x', parent_id: body.parent_id || null, products: [] });
    }),
    http.get('/api/v1/library/files', () =>
      HttpResponse.json({ items: [], meta: { total: 0, current_page: 1, per_page: 50, last_page: 1 } }),
    ),
    http.get('/api/v1/library/stats', () =>
      HttpResponse.json({ total_files: 0, total_folders: 6, total_size_bytes: 0, disk_free_bytes: 10737418240, disk_total_bytes: 107374182400 }),
    ),
    http.get('/api/v1/settings/', () => HttpResponse.json({ library_disk_warning_gb: 5 })),
    http.get('/api/v1/library/tags', () => HttpResponse.json([])),
  );
}

/** The sidebar row of the folder named `name`. */
async function treeRow(name: string): Promise<HTMLElement> {
  const label = await screen.findByText(name, { selector: 'span' });
  const row = label.closest('[data-folder-row]') as HTMLElement | null;
  if (!row) throw new Error(`no tree row for ${name}`);
  return row;
}

async function openMenu(name: string) {
  const row = await treeRow(name);
  const trigger = within(row)
    .getAllByRole('button')
    .find((b) => b.querySelector('.lucide-ellipsis-vertical, .lucide-more-vertical'));
  if (!trigger) throw new Error('no ⋮ trigger');
  await userEvent.click(trigger);
}

async function openMoveDialog(name: string) {
  await openMenu(name);
  await userEvent.click(await screen.findByRole('button', { name: 'Move…' }));
  return screen.findByRole('dialog', { name: `Move “${name}”` });
}

const target = (dialog: HTMLElement, name: string) =>
  within(dialog)
    .getAllByRole('button')
    .find((b) => b.querySelector('span.truncate')?.textContent === name) as HTMLButtonElement;

async function expand(name: string) {
  const row = await treeRow(name);
  await userEvent.click(within(row).getAllByRole('button')[0]);
}

beforeEach(() => {
  localStorage.clear();
  auth.denied = new Set();
  tree = initialTree();
  puts = [];
  refusal = null;
  window.history.pushState({}, '', '/files');
  mockLibrary();
});

describe('the Move… dialog of a folder', () => {
  it('offers the legal targets and explains the others', async () => {
    render(<FileManagerPage />);
    await expand('Parts');
    const dialog = await openMoveDialog('Brackets');

    expect(target(dialog, 'Library root')).toBeEnabled();
    expect(target(dialog, 'Spares')).toBeEnabled();

    expect(target(dialog, 'Parts')).toBeDisabled();
    expect(target(dialog, 'Parts')).toHaveTextContent('already here');
    expect(target(dialog, 'Brackets')).toBeDisabled();
    expect(target(dialog, 'Brackets')).toHaveTextContent('this folder');
    expect(target(dialog, 'Small')).toBeDisabled();
    expect(target(dialog, 'Small')).toHaveTextContent('its own subfolder');
    expect(target(dialog, 'NAS')).toBeDisabled();
    expect(target(dialog, 'NAS')).toHaveTextContent('external');
    expect(target(dialog, 'Scans')).toBeDisabled();
    expect(target(dialog, 'Scans')).toHaveTextContent('external');
  });

  it('marks a root folder’s root entry as where it already is', async () => {
    render(<FileManagerPage />);
    const dialog = await openMoveDialog('Parts');

    expect(target(dialog, 'Library root')).toBeDisabled();
    expect(target(dialog, 'Library root')).toHaveTextContent('already here');
  });

  it('shows which folders are linked to products', async () => {
    render(<FileManagerPage />);
    const dialog = await openMoveDialog('Spares');

    // Both what is being moved and a linked target keep their product mark.
    expect(within(dialog).getAllByTitle('Lamp').length).toBeGreaterThanOrEqual(2);
  });

  it('moves under the chosen folder and closes', async () => {
    render(<FileManagerPage />);
    await expand('Parts');
    const dialog = await openMoveDialog('Brackets');

    await userEvent.click(target(dialog, 'Spares'));
    await userEvent.click(within(dialog).getByRole('button', { name: 'Move' }));

    await waitFor(() => expect(puts).toEqual([{ id: '2', body: { parent_id: 4 } }]));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('sends 0 for the library root', async () => {
    render(<FileManagerPage />);
    await expand('Parts');
    const dialog = await openMoveDialog('Brackets');

    await userEvent.click(target(dialog, 'Library root'));
    await userEvent.click(within(dialog).getByRole('button', { name: 'Move' }));

    await waitFor(() => expect(puts).toEqual([{ id: '2', body: { parent_id: 0 } }]));
  });

  it.each([
    ['folder_parent_not_found', 404, 'The target folder no longer exists.'],
    ['folder_parent_external', 400, 'A folder cannot be moved into an external folder.'],
    ['folder_external_fixed', 400, 'An external folder and its subfolders cannot be moved.'],
    ['folder_own_subtree', 400, 'A folder cannot be moved into itself or one of its own subfolders.'],
    ['folder_external_rename', 400, 'A subfolder of an external folder cannot be renamed here.'],
  ])('says the sentence of the refusal %s and stays open', async (code, status, sentence) => {
    // The server's own text is deliberately different: the sentence comes from the code.
    refusal = { status, detail: { error: code, message: 'server prose that must not be shown' } };
    render(<FileManagerPage />);
    await expand('Parts');
    const dialog = await openMoveDialog('Brackets');

    await userEvent.click(target(dialog, 'Spares'));
    await userEvent.click(within(dialog).getByRole('button', { name: 'Move' }));

    expect(await within(dialog).findByRole('alert')).toHaveTextContent(sentence);
    expect(within(dialog).queryByText(/server prose/)).not.toBeInTheDocument();
    expect(screen.getByRole('dialog', { name: 'Move “Brackets”' })).toBeInTheDocument();
  });

  it('keeps the server’s message for a refusal without a known code', async () => {
    refusal = { status: 409, detail: 'Somebody else is moving folders' };
    render(<FileManagerPage />);
    await expand('Parts');
    const dialog = await openMoveDialog('Brackets');

    await userEvent.click(target(dialog, 'Spares'));
    await userEvent.click(within(dialog).getByRole('button', { name: 'Move' }));

    expect(await within(dialog).findByRole('alert')).toHaveTextContent('Somebody else is moving folders');
  });
});

describe('who gets the Move… entry', () => {
  it('explains that an external folder cannot be moved', async () => {
    render(<FileManagerPage />);
    await openMenu('NAS');

    const entry = await screen.findByRole('button', { name: 'Move…' });
    expect(entry).toBeDisabled();
    expect(entry).toHaveAttribute('title', 'An external folder and its subfolders cannot be moved.');
  });

  it('explains the missing right', async () => {
    // N56: the owner moves their own folder with `library:update_own`; neither right, no move.
    auth.denied = new Set(['library:update_all', 'library:update_own']);
    render(<FileManagerPage />);
    await openMenu('Spares');

    const entry = await screen.findByRole('button', { name: 'Move…' });
    expect(entry).toBeDisabled();
    expect(entry).toHaveAttribute('title', 'You do not have permission to move folders');
  });
});

describe('the page after a move', () => {
  it('keeps the moved folder open and its branch expanded', async () => {
    window.history.pushState({}, '', '/files?folder=2');
    render(<FileManagerPage />);
    // Brackets is the open folder, so its own branch is open.
    expect(await screen.findByText('Small', { selector: 'span' })).toBeInTheDocument();

    const dialog = await openMoveDialog('Brackets');
    await userEvent.click(target(dialog, 'Spares'));
    await userEvent.click(within(dialog).getByRole('button', { name: 'Move' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());

    // The tree was read again: Brackets now hangs under Spares, still the open
    // folder, and the branch the operator had opened is still open.
    await waitFor(async () => {
      const spares = (await treeRow('Spares')).parentElement as HTMLElement;
      expect(within(spares).getByText('Brackets')).toBeInTheDocument();
    });
    expect((await treeRow('Brackets')).className).toContain('text-bambu-green');
    expect(screen.getByText('Small', { selector: 'span' })).toBeInTheDocument();
    expect(new URLSearchParams(window.location.search).get('folder')).toBe('2');
  });

  it('keeps a hand-opened branch open when its folder moves to the root', async () => {
    render(<FileManagerPage />);
    await expand('Parts');
    await expand('Brackets');
    expect(await screen.findByText('Small', { selector: 'span' })).toBeInTheDocument();

    const dialog = await openMoveDialog('Brackets');
    await userEvent.click(target(dialog, 'Library root'));
    await userEvent.click(within(dialog).getByRole('button', { name: 'Move' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());

    await waitFor(() => expect(puts.length).toBe(1));
    expect(await screen.findByText('Small', { selector: 'span' })).toBeInTheDocument();
  });
});
