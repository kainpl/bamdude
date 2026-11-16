/**
 * A double click opens.
 *
 * A file's card or row opens in the viewer its type has — the same one its
 * menu offers — on a double click, or on Enter while it has the focus. A
 * single click stays what it was, and a double click that lands on one of the
 * card's own controls (the checkbox, the ⋮ trigger, a link) is that control's.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
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

// Keep WebGL / Three.js out of jsdom: the 3D viewer is somebody else's test.
vi.mock('../../components/ModelViewerModal', () => ({
  ModelViewerModal: ({ title }: { title: string }) => <div data-testid="model-viewer-modal">{title}</div>,
}));

const base = {
  file_path: '/library/x',
  file_size: 2048,
  product_ids: [],
  folder_id: null,
  thumbnail_path: null,
  print_time_seconds: null,
  duplicate_count: 0,
  print_count: 0,
  tags: [],
  created_at: '2024-01-01T00:00:00Z',
};

const mockFiles = [
  { ...base, id: 7, filename: 'Photo.PNG', file_type: 'png', file_tags: [], print_name: null },
  { ...base, id: 8, filename: 'lamp.3mf', file_type: '3mf', file_tags: ['3mf', 'project'], print_name: 'Lamp' },
  { ...base, id: 9, filename: 'notes.txt', file_type: 'txt', file_tags: [], print_name: null },
];

const mockFolders = [
  {
    id: 1,
    name: 'Functional Parts',
    parent_id: null,
    file_count: 5,
    products: [],
    children: [{ id: 2, name: 'Brackets', parent_id: 1, file_count: 3, products: [], children: [] }],
  },
];

let folderIdsAsked: Array<string | null> = [];

function mockLibrary() {
  server.use(
    http.get('/api/v1/library/folders', () => HttpResponse.json(mockFolders)),
    http.get('/api/v1/library/files', ({ request }) => {
      folderIdsAsked.push(new URL(request.url).searchParams.get('folder_id'));
      return HttpResponse.json({
        items: mockFiles,
        meta: { total: mockFiles.length, current_page: 1, per_page: 50, last_page: 1 },
      });
    }),
    http.get('/api/v1/library/stats', () =>
      HttpResponse.json({
        total_files: 3,
        total_folders: 2,
        total_size_bytes: 6144,
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
}

/** The card (grid) or the row (list) of the file titled `name`. */
async function itemOf(name: string): Promise<HTMLElement> {
  const title = await screen.findByText(name);
  const item = title.closest('[data-file-row], [data-file-card]') as HTMLElement | null;
  if (!item) throw new Error(`no card or row for ${name}`);
  return item;
}

const imageDialog = () => screen.queryByRole('dialog', { name: 'Photo.PNG' });
const modelViewer = () => screen.queryByTestId('model-viewer-modal');

describe.each(['grid', 'list'] as const)('a double click in the %s view', (viewMode) => {
  beforeEach(() => {
    localStorage.clear();
    localStorage.setItem('library-view-mode', viewMode);
    folderIdsAsked = [];
    mockLibrary();
  });

  it('opens an image in the image viewer', async () => {
    render(<FileManagerPage />);

    await userEvent.dblClick(await itemOf('Photo.PNG'));

    const dialog = await screen.findByRole('dialog', { name: 'Photo.PNG' });
    expect(within(dialog).getByRole('img', { name: 'Photo.PNG' }).getAttribute('src')).toContain(
      '/api/v1/library/files/7/image',
    );
  });

  it('opens a model in the 3D viewer', async () => {
    render(<FileManagerPage />);

    await userEvent.dblClick(await itemOf('Lamp'));

    expect(await screen.findByTestId('model-viewer-modal')).toHaveTextContent('Lamp');
    expect(imageDialog()).not.toBeInTheDocument();
  });

  it('opens nothing for a type no viewer shows, and nothing on a single click', async () => {
    render(<FileManagerPage />);

    await userEvent.dblClick(await itemOf('notes.txt'));
    await userEvent.click(await itemOf('Photo.PNG'));
    await userEvent.click(await itemOf('Lamp'));

    expect(imageDialog()).not.toBeInTheDocument();
    expect(modelViewer()).not.toBeInTheDocument();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('leaves a double click on the checkbox or on the ⋮ trigger to that control', async () => {
    render(<FileManagerPage />);
    const item = await itemOf('Photo.PNG');

    // fireEvent, not userEvent: the event under test is the dblclick itself,
    // without the two clicks that would toggle the control on the way.
    fireEvent.doubleClick(within(item).getByRole('button', { name: 'Select file' }));
    const trigger = within(item)
      .getAllByRole('button')
      .find((b) => b.querySelector('.lucide-ellipsis-vertical, .lucide-more-vertical'));
    expect(trigger).toBeTruthy();
    fireEvent.doubleClick(trigger!);
    // …and on the icon inside the trigger, which is what the pointer really hits.
    fireEvent.doubleClick(trigger!.querySelector('svg')!);

    expect(imageDialog()).not.toBeInTheDocument();
  });

  it('opens on Enter while the card has the focus — not while a control inside it has', async () => {
    render(<FileManagerPage />);
    const item = await itemOf('Photo.PNG');

    const checkbox = within(item).getByRole('button', { name: 'Select file' });
    fireEvent.keyDown(checkbox, { key: 'Enter' });
    expect(imageDialog()).not.toBeInTheDocument();

    expect(item.tabIndex).toBe(0);
    item.focus();
    fireEvent.keyDown(item, { key: 'Enter' });
    expect(await screen.findByRole('dialog', { name: 'Photo.PNG' })).toBeInTheDocument();
  });

  it('gives no tab stop to a file nothing opens', async () => {
    render(<FileManagerPage />);

    expect((await itemOf('notes.txt')).hasAttribute('tabindex')).toBe(false);
  });
});

describe('a double click on a folder', () => {
  beforeEach(() => {
    localStorage.clear();
    folderIdsAsked = [];
    mockLibrary();
  });

  it('enters it — and stays in it', async () => {
    render(<FileManagerPage />);
    const folder = (await screen.findAllByText('Functional Parts'))[0];

    await userEvent.dblClick(folder);

    await waitFor(() => expect(folderIdsAsked.at(-1)).toBe('1'));
    // Both clicks of the pair land on the same folder: the second must not
    // leave it again.
    expect(folderIdsAsked.filter((id) => id === '1').length).toBeGreaterThan(0);
    expect(folderIdsAsked.at(-1)).toBe('1');
  });
});
