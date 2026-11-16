/**
 * The File Manager's columns view (Miller columns).
 *
 * The library root, then one column per folder on the path to the open folder;
 * a column lists its level's folders and then its files. The path is the
 * address's `folder`, so a link restores the columns. Every column asks the
 * server for ITS folder's direct files — with the page's sort and filters and a
 * page of its own — and never sorts, filters or counts by itself.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { FileManagerPage } from '../../pages/FileManagerPage';

vi.mock('react-router', async () => {
  const actual = await vi.importActual<typeof import('react-router')>('react-router');
  return { ...actual, useNavigate: () => vi.fn() };
});

vi.mock('../../components/ModelViewerModal', () => ({
  ModelViewerModal: ({ title }: { title: string }) => <div data-testid="model-viewer-modal">{title}</div>,
}));

vi.mock('../../components/LibraryFileDetailsModal', () => ({
  LibraryFileDetailsModal: ({ file }: { file: { filename: string } }) => (
    <div data-testid="details-modal">{file.filename}</div>
  ),
}));

type Folder = Record<string, unknown> & { id: number; name: string; children: Folder[] };

const folder = (id: number, name: string, parent_id: number | null, children: Folder[] = [], extra: Record<string, unknown> = {}): Folder => ({
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

// A > B > C > D > E, five levels deep, and a second root folder.
const tree: Folder[] = [
  folder(1, 'Alpha', null, [folder(2, 'Beta', 1, [folder(3, 'Gamma', 2, [folder(4, 'Delta', 3, [folder(5, 'Epsilon', 4)])])])], { file_count: 120 }),
  folder(6, 'Other', null, [], { products: [{ id: 9, name: 'Lamp' }] }),
];

const fileBase = {
  file_path: '/library/x',
  file_size: 2048,
  product_ids: [],
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
const file = (id: number, filename: string, folder_id: number | null, file_type = 'stl') => ({
  ...fileBase,
  id,
  filename,
  folder_id,
  file_type,
});

/** What each level holds; Alpha holds 120 files — more than one page. */
const filesByFolder: Record<string, ReturnType<typeof file>[]> = {
  root: [file(900, 'root-photo.png', null, 'png'), file(901, 'root-part.stl', null)],
  '1': Array.from({ length: 120 }, (_, i) => file(1000 + i, `alpha-${String(i).padStart(3, '0')}.stl`, 1)),
  '2': [file(200, 'beta.stl', 2)],
  '5': [file(500, 'epsilon.stl', 5)],
};

let requests: URLSearchParams[] = [];
let fileMoves: unknown[] = [];

function mockLibrary() {
  server.use(
    http.get('/api/v1/library/folders', () => HttpResponse.json(tree)),
    http.get('/api/v1/library/files', ({ request }) => {
      const params = new URL(request.url).searchParams;
      requests.push(params);
      const all = filesByFolder[params.get('folder_id') ?? 'root'] ?? [];
      const q = params.get('q');
      const matching = q ? all.filter((f) => f.filename.includes(q)) : all;
      const page = Number(params.get('page') ?? 1);
      const perPage = Number(params.get('per_page') ?? 50);
      return HttpResponse.json({
        items: matching.slice((page - 1) * perPage, page * perPage),
        meta: { total: matching.length, current_page: page, per_page: perPage, last_page: Math.max(1, Math.ceil(matching.length / perPage)) },
      });
    }),
    http.post('/api/v1/library/files/move', async ({ request }) => {
      fileMoves.push(await request.json());
      return HttpResponse.json({ status: 'ok', moved: 1 });
    }),
    http.get('/api/v1/library/stats', () =>
      HttpResponse.json({ total_files: 3, total_folders: 6, total_size_bytes: 0, disk_free_bytes: 10737418240, disk_total_bytes: 107374182400 }),
    ),
    http.get('/api/v1/settings/', () => HttpResponse.json({ library_disk_warning_gb: 5 })),
    http.get('/api/v1/library/tags', () =>
      HttpResponse.json([{ id: 77, name: 'keep', is_system: false, code: null, file_count: 3 }]),
    ),
  );
}

const column = (name: string) => screen.findByRole('listbox', { name });
/** The whole column — its header, list and the lines under it. */
const columnBox = async (name: string) => (await column(name)).closest('[data-column]') as HTMLElement;
const columnNames = () => screen.getAllByRole('listbox').map((list) => list.getAttribute('aria-label'));
const foldersAsked = () => requests.map((params) => params.get('folder_id'));
const option = async (list: string, name: string) => within(await column(list)).findByRole('option', { name: new RegExp(name) });

function open(path: string) {
  window.history.pushState({}, '', path);
  return render(<FileManagerPage />);
}

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem('library-view-mode', 'columns');
  requests = [];
  fileMoves = [];
  mockLibrary();
});

describe('choosing the view', () => {
  it('is the third mode of the switch, and is remembered', async () => {
    localStorage.setItem('library-view-mode', 'list');
    open('/files');
    await screen.findByText('root-part.stl');
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Columns' }));

    expect(await column('Library')).toBeInTheDocument();
    expect(localStorage.getItem('library-view-mode')).toBe('columns');
  });
});

describe('the columns of a path', () => {
  it('are restored from the address, one request per column', async () => {
    open('/files?folder=5');

    await within(await column('Epsilon')).findByRole('option', { name: /epsilon\.stl/ });
    await within(await column('Library')).findByRole('option', { name: /root-part\.stl/ });
    expect(columnNames()).toEqual(['Library', 'Alpha', 'Beta', 'Gamma', 'Delta', 'Epsilon']);

    // The root and the five folders of the path: six requests, each a first page of its own level.
    await waitFor(() => expect(requests.length).toBe(6));
    expect(foldersAsked().sort()).toEqual(['1', '2', '3', '4', '5', null].sort());
    for (const params of requests) {
      expect(params.get('page')).toBe('1');
      expect(params.get('per_page')).toBe('50');
      expect(params.get('include_root')).toBe('true');
      expect(params.get('recursive')).toBeNull();
    }
    // Nothing but those: no list of the other views behind the columns, none per folder row.
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(requests.length).toBe(6);
  });

  it('marks the path, lists folders before files, and shows the server’s totals', async () => {
    open('/files?folder=2');

    const library = await column('Library');
    const rows = await within(library).findAllByRole('option');
    await within(library).findByRole('option', { name: /root-part\.stl/ });
    expect(within(library).getAllByRole('option').map((row) => row.textContent)).toEqual([
      'Alpha120',
      'Other',
      'root-photo.png',
      'root-part.stl',
    ]);
    expect(rows[0]).toHaveAttribute('aria-selected', 'true');
    expect(await option('Alpha', 'Beta')).toHaveAttribute('aria-selected', 'true');
    // A linked folder keeps its product mark.
    expect(within(await option('Library', 'Other')).getByTitle('Lamp')).toBeInTheDocument();
    // 120 files in Alpha — counted by the server, 50 of them shown.
    const alpha = (await column('Alpha')).closest('[data-column]') as HTMLElement;
    expect(await within(alpha).findByText('120 files')).toBeInTheDocument();
  });

  it('opens the next column on a folder and drops the columns to its right', async () => {
    open('/files?folder=5');
    await column('Epsilon');

    await userEvent.click(await option('Library', 'Other'));

    await waitFor(() => expect(columnNames()).toEqual(['Library', 'Other']));
    expect(new URLSearchParams(window.location.search).get('folder')).toBe('6');
  });

  // The address names the open folder in EVERY view: a link copied after
  // leaving the columns opens the folder on screen, not the one the columns left there.
  it('keeps the address true after the view changes: a folder opened in the cards view is the address’s', async () => {
    open('/files?folder=1');
    await column('Alpha');

    await userEvent.click(screen.getByRole('button', { name: 'Cards' }));
    await waitFor(() => expect(screen.queryByRole('listbox')).not.toBeInTheDocument());
    expect(new URLSearchParams(window.location.search).get('folder')).toBe('1');

    // Another folder, from the tree.
    const steps = window.history.length;
    await userEvent.click(screen.getAllByText('Other')[0]);
    await waitFor(() => expect(new URLSearchParams(window.location.search).get('folder')).toBe('6'));
    // Outside the columns a folder click is no step of the browser's history.
    expect(window.history.length).toBe(steps);

    // And the top level names no folder at all.
    await userEvent.click(screen.getAllByText('Internal')[0]);
    await waitFor(() => expect(new URLSearchParams(window.location.search).get('folder')).toBeNull());
  });

  it('walks back along the path with the browser’s Back', async () => {
    open('/files');
    await userEvent.click(await option('Library', 'Alpha'));
    await userEvent.click(await option('Alpha', 'Beta'));
    await waitFor(() => expect(columnNames()).toEqual(['Library', 'Alpha', 'Beta']));

    window.history.back();
    await waitFor(() => expect(columnNames()).toEqual(['Library', 'Alpha']));
    window.history.back();
    await waitFor(() => expect(columnNames()).toEqual(['Library']));
  });

  it('shows just the root for a folder that is not in the tree', async () => {
    open('/files?folder=404');

    await column('Library');
    expect(columnNames()).toEqual(['Library']);
  });

  it('brings the newest column into view', async () => {
    const scrollWidth = vi.spyOn(HTMLElement.prototype, 'scrollWidth', 'get').mockReturnValue(1234);
    try {
      open('/files');
      await userEvent.click(await option('Library', 'Alpha'));
      await column('Alpha');
      expect(screen.getByTestId('library-columns').scrollLeft).toBe(1234);
    } finally {
      scrollWidth.mockRestore();
    }
  });
});

describe('a column’s files', () => {
  it('come a page at a time, with "Show more" asking the server for the next', async () => {
    open('/files?folder=1');
    const alpha = (await column('Alpha')).closest('[data-column]') as HTMLElement;
    await within(alpha).findByRole('option', { name: /alpha-049/ });
    expect(within(alpha).queryByRole('option', { name: /alpha-050/ })).not.toBeInTheDocument();

    requests = [];
    await userEvent.click(within(alpha).getByRole('button', { name: 'Show more (50 of 120)' }));

    await within(alpha).findByRole('option', { name: /alpha-099/ });
    expect(requests.map((params) => [params.get('folder_id'), params.get('page')])).toEqual([['1', '2']]);
    expect(within(alpha).getByRole('button', { name: 'Show more (100 of 120)' })).toBeInTheDocument();

    await userEvent.click(within(alpha).getByRole('button', { name: 'Show more (100 of 120)' }));
    await within(alpha).findByRole('option', { name: /alpha-119/ });
    expect(within(alpha).queryByRole('button', { name: /Show more/ })).not.toBeInTheDocument();
  });

  it('are asked in the page’s sort order', async () => {
    localStorage.setItem('library-sort-field', 'date');
    localStorage.setItem('library-sort-direction', 'desc');
    open('/files?folder=2');
    await option('Beta', 'beta.stl');

    expect(requests.length).toBe(3);
    for (const params of requests) expect(params.get('sort_by')).toBe('date_desc');
  });

  it('are narrowed by the search inside each column, and the view says so', async () => {
    open('/files?folder=2');
    await option('Beta', 'beta.stl');
    expect(screen.queryByTestId('columns-filter-note')).not.toBeInTheDocument();

    requests = [];
    await userEvent.type(screen.getByPlaceholderText('Search files...'), 'part');

    // Every column asks again for its own folder with the same search — never a global one.
    await waitFor(() => expect(requests.filter((params) => params.get('q') === 'part').length).toBe(3));
    const searched = requests.filter((params) => params.get('q') === 'part');
    expect(searched.map((params) => params.get('folder_id')).sort()).toEqual(['1', '2', null].sort());
    for (const params of searched) expect(params.get('recursive')).toBeNull();

    // Folders stay so the path can be walked; files are the matches of the level.
    expect(await option('Library', 'root-part.stl')).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('option', { name: /root-photo/ })).not.toBeInTheDocument());
    expect(await option('Library', 'Alpha')).toBeInTheDocument();
    expect(await within(await columnBox('Beta')).findByText('No matching files here')).toBeInTheDocument();
    expect(screen.getByTestId('columns-filter-note')).toHaveTextContent('each column shows only the matching files of its own folder');
  });

  it('are narrowed by a tag inside each column', async () => {
    open('/files?folder=1');
    await column('Alpha');

    requests = [];
    await userEvent.click(await screen.findByRole('button', { name: 'keep' }));

    await waitFor(() => expect(requests.length).toBe(2));
    for (const params of requests) {
      expect(params.getAll('tag_ids')).toEqual(['77']);
      expect(params.get('folder_scope')).toBe('true');
    }
    expect(foldersAsked().sort()).toEqual(['1', null].sort());
  });

  it('never reach into subfolders: that search switch is off here, with the reason', async () => {
    open('/files?folder=1');
    await column('Alpha');
    await userEvent.type(screen.getByPlaceholderText('Search files...'), 'x');

    const toggle = await screen.findByRole('button', { name: 'This folder and subfolders' });
    expect(toggle).toBeDisabled();
    expect(toggle).toHaveAttribute('title', 'Columns show one folder each — a search here does not reach into subfolders.');
  });

  it('say when a folder is empty, and while they load', async () => {
    open('/files?folder=3');

    const gamma = (await column('Gamma')).closest('[data-column]') as HTMLElement;
    expect(within(gamma).getByRole('status')).toHaveTextContent('Loading files...');
    // Gamma has a subfolder and no files: not "empty". Delta's child Epsilon likewise.
    await waitFor(() => expect(within(gamma).queryByRole('status')).not.toBeInTheDocument());
    expect(within(gamma).queryByText('Folder is empty')).not.toBeInTheDocument();

    await userEvent.click(await option('Library', 'Other'));
    expect(await within(await columnBox('Other')).findByText('Folder is empty')).toBeInTheDocument();
  });
});

describe('a file in a column', () => {
  it('shows in a preview column with the page’s own card and Details', async () => {
    open('/files?folder=2');
    await userEvent.click(await option('Beta', 'beta.stl'));

    const preview = await screen.findByRole('region', { name: 'Preview: beta.stl' });
    expect(preview.querySelector('[data-file-card]')).toBeTruthy();
    expect(await option('Beta', 'beta.stl')).toHaveAttribute('aria-selected', 'true');

    await userEvent.click(within(preview).getByRole('button', { name: 'Details' }));
    expect(await screen.findByTestId('details-modal')).toHaveTextContent('beta.stl');
  });

  it('of an earlier column closes the columns to its right', async () => {
    open('/files?folder=2');
    await column('Beta');

    await userEvent.click(await option('Library', 'root-part.stl'));

    await screen.findByRole('region', { name: 'Preview: root-part.stl' });
    await waitFor(() => expect(columnNames()).toEqual(['Library']));
  });

  it('opens on a double click as in the other views', async () => {
    open('/files');
    await userEvent.dblClick(await option('Library', 'root-photo.png'));

    expect(await screen.findByRole('dialog', { name: 'root-photo.png' })).toBeInTheDocument();
  });
});

describe('selection across columns', () => {
  it('ticks files of several columns into one selection for the bulk bar', async () => {
    open('/files?folder=2');
    const rootRow = (await option('Library', 'root-part.stl')).closest('[data-file-row]') as HTMLElement;
    const betaRow = (await option('Beta', 'beta.stl')).closest('[data-file-row]') as HTMLElement;

    await userEvent.click(within(rootRow).getByRole('button', { name: 'Select file' }));
    await userEvent.click(within(betaRow).getByRole('button', { name: 'Select file' }));

    expect(await screen.findByText('2 selected')).toBeInTheDocument();
    expect(within(rootRow).getByRole('button', { name: 'Select file' })).toHaveAttribute('aria-pressed', 'true');
    expect(within(betaRow).getByRole('button', { name: 'Select file' })).toHaveAttribute('aria-pressed', 'true');
    // The bulk bar acts on them: the Move dialog is offered for both.
    await userEvent.click(screen.getByRole('button', { name: 'Move' }));
    expect(await screen.findByRole('dialog', { name: 'Move 2 files' })).toBeInTheDocument();
  });

  it('is cleared by opening another folder, as in the other views', async () => {
    open('/files?folder=2');
    const betaRow = (await option('Beta', 'beta.stl')).closest('[data-file-row]') as HTMLElement;
    await userEvent.click(within(betaRow).getByRole('button', { name: 'Select file' }));
    expect(await screen.findByText('1 selected')).toBeInTheDocument();

    await userEvent.click(await option('Library', 'Other'));

    await waitFor(() => expect(screen.queryByText('1 selected')).not.toBeInTheDocument());
  });
});

describe('the keyboard', () => {
  it('keeps one tab stop per column', async () => {
    open('/files?folder=2');
    await option('Beta', 'beta.stl');
    await option('Library', 'root-part.stl');

    for (const list of screen.getAllByRole('listbox')) {
      const stops = within(list).getAllByRole('option').filter((row) => row.tabIndex === 0);
      expect(stops.length).toBe(1);
    }
    // …and it is the row on the path.
    expect(await option('Library', 'Alpha')).toHaveAttribute('tabindex', '0');
  });

  it('walks a column with Up, Down, Home and End', async () => {
    open('/files');
    const alpha = await option('Library', 'Alpha');
    await option('Library', 'root-part.stl');
    alpha.focus();

    await userEvent.keyboard('{ArrowDown}');
    expect(await option('Library', 'Other')).toHaveFocus();
    await userEvent.keyboard('{End}');
    expect(await option('Library', 'root-part.stl')).toHaveFocus();
    await userEvent.keyboard('{ArrowDown}');
    expect(await option('Library', 'root-part.stl')).toHaveFocus();
    await userEvent.keyboard('{ArrowUp}');
    expect(await option('Library', 'root-photo.png')).toHaveFocus();
    await userEvent.keyboard('{Home}');
    expect(alpha).toHaveFocus();
  });

  it('enters a folder with Right and returns to it with Left', async () => {
    open('/files');
    (await option('Library', 'Alpha')).focus();

    await userEvent.keyboard('{ArrowRight}');
    await waitFor(async () => expect(await option('Alpha', 'Beta')).toHaveFocus());
    expect(new URLSearchParams(window.location.search).get('folder')).toBe('1');

    await userEvent.keyboard('{Enter}');
    await column('Beta');
    await waitFor(() => expect(new URLSearchParams(window.location.search).get('folder')).toBe('2'));

    (await option('Alpha', 'Beta')).focus();
    await userEvent.keyboard('{ArrowLeft}');
    expect(await option('Library', 'Alpha')).toHaveFocus();
  });

  it('moves to the preview with Right on a file, opens it with Enter, ticks it with Space', async () => {
    open('/files');
    const photo = await option('Library', 'root-photo.png');
    photo.focus();

    await userEvent.keyboard(' ');
    expect(await screen.findByText('1 selected')).toBeInTheDocument();

    await userEvent.keyboard('{ArrowRight}');
    const preview = await screen.findByRole('region', { name: 'Preview: root-photo.png' });
    await waitFor(() => expect(preview).toHaveFocus());

    photo.focus();
    await userEvent.keyboard('{Enter}');
    expect(await screen.findByRole('dialog', { name: 'root-photo.png' })).toBeInTheDocument();
  });
});

describe('drag-and-drop onto a column’s folder', () => {
  it('moves the dragged file there and reads the columns again', async () => {
    open('/files?folder=2');
    const source = await option('Beta', 'beta.stl');
    const target = await option('Library', 'Other');
    await option('Library', 'root-part.stl');
    await waitFor(() => expect(requests.length).toBe(3));

    const types: string[] = [];
    const dataTransfer = {
      types,
      files: [],
      dropEffect: 'none',
      effectAllowed: 'all',
      setData: (type: string) => void types.push(type),
      getData: () => '',
    };
    fireEvent.dragStart(source, { dataTransfer });
    fireEvent.dragOver(target, { dataTransfer });
    expect(dataTransfer.dropEffect).toBe('move');
    expect(target).toHaveAttribute('data-drop-state', 'legal');
    fireEvent.drop(target, { dataTransfer });

    await waitFor(() => expect(fileMoves).toEqual([{ file_ids: [200], folder_id: 6 }]));
    // The move refreshes the list — and with it every column on screen.
    await waitFor(() => expect(requests.length).toBe(6));
  });
});

describe('on a phone', () => {
  const original = window.matchMedia;
  beforeEach(() => {
    window.matchMedia = ((query: string) => ({
      ...original(query),
      matches: query === '(max-width: 39.99rem)',
    })) as typeof window.matchMedia;
  });
  afterEach(() => {
    window.matchMedia = original;
  });

  it('shows one column — the open folder — with a way back up', async () => {
    open('/files?folder=2');

    await option('Beta', 'beta.stl');
    expect(columnNames()).toEqual(['Beta']);

    await userEvent.click(screen.getByRole('button', { name: 'Back to Alpha' }));
    await waitFor(() => expect(columnNames()).toEqual(['Alpha']));
    await userEvent.click(screen.getByRole('button', { name: 'Back to Library' }));
    await waitFor(() => expect(columnNames()).toEqual(['Library']));
    expect(screen.queryByRole('button', { name: /Back to/ })).not.toBeInTheDocument();
  });

  it('shows the preview in place of the column, and Back returns to it', async () => {
    open('/files?folder=2');
    await userEvent.click(await option('Beta', 'beta.stl'));

    await screen.findByRole('region', { name: 'Preview: beta.stl' });
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Back to Beta' }));
    expect(await column('Beta')).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: /Preview/ })).not.toBeInTheDocument();
  });
});
