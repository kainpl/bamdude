/**
 * The File Manager's way into a file's details, and the marks a card carries
 * for what the details hold.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { FileManagerPage } from '../../pages/FileManagerPage';

vi.mock('react-router', async () => {
  const actual = await vi.importActual<typeof import('react-router')>('react-router');
  return { ...actual, useNavigate: () => vi.fn() };
});

const base = {
  file_path: '/library/x',
  file_size: 2048,
  product_ids: [],
  folder_id: null,
  thumbnail_path: null,
  print_time_seconds: null,
  duplicate_count: 0,
  print_count: 0,
  notes_count: 0,
  tags: [],
  file_tags: [],
  print_name: null,
  created_by_id: 1,
  created_at: '2024-01-01T00:00:00Z',
};

const mockFiles = [
  { ...base, id: 7, filename: 'linked.stl', file_type: 'stl', external_url: 'https://www.printables.com/model/1' },
  { ...base, id: 8, filename: 'plain.stl', file_type: 'stl', external_url: null },
  { ...base, id: 10, filename: 'pictured.stl', file_type: 'stl', external_url: null, photos_count: 3 },
  { ...base, id: 9, filename: 'odd.stl', file_type: 'stl', external_url: 'javascript:alert(1)' },
];

function mockLibrary() {
  server.use(
    http.get('/api/v1/library/folders', () => HttpResponse.json([])),
    http.get('/api/v1/library/files', () =>
      HttpResponse.json({
        items: mockFiles,
        meta: { total: mockFiles.length, current_page: 1, per_page: 50, last_page: 1 },
      }),
    ),
    http.get('/api/v1/library/files/:id/photos', () => HttpResponse.json([])),
    http.get('/api/v1/library/files/:id', ({ params }) => {
      const file = mockFiles.find((f) => f.id === Number(params.id));
      return HttpResponse.json({ ...file, folder_name: null, file_hash: null, updated_at: '2024-01-01T00:00:00Z' });
    }),
    http.get('/api/v1/library/stats', () =>
      HttpResponse.json({
        total_files: 3,
        total_folders: 0,
        total_size_bytes: 8192,
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

/** The card (grid) or row (list) whose title is `name`. */
async function itemOf(name: string) {
  const title = await screen.findByText(name);
  return (title.closest('[data-file-row]') ?? title.closest('.group')) as HTMLElement;
}

async function openMenuOf(name: string) {
  const trigger = within(await itemOf(name))
    .getAllByRole('button')
    .find((b) => b.querySelector('.lucide-ellipsis-vertical, .lucide-more-vertical'));
  if (!trigger) throw new Error('no ⋮ trigger found');
  await userEvent.click(trigger);
}

describe.each(['grid', 'list'] as const)('file details from the %s view', (viewMode) => {
  beforeEach(() => {
    localStorage.clear();
    localStorage.setItem('library-view-mode', viewMode);
    mockLibrary();
  });

  it('opens the details of the file whose menu was used', async () => {
    render(<FileManagerPage />);

    await openMenuOf('plain.stl');
    await userEvent.click(await screen.findByRole('button', { name: 'Details' }));

    const dialog = await screen.findByRole('dialog', { name: 'File details' });
    expect(within(dialog).getByText('plain.stl')).toBeInTheDocument();

    await userEvent.click(within(dialog).getAllByRole('button', { name: 'Close' })[0]);
    expect(screen.queryByRole('dialog', { name: 'File details' })).not.toBeInTheDocument();
  });

  it('marks a file that has photos with their number, and the mark opens its details', async () => {
    render(<FileManagerPage />);

    const mark = within(await itemOf('pictured.stl')).getByRole('button', { name: 'Photos: 3' });
    expect(mark).toHaveTextContent('3');
    expect(within(await itemOf('plain.stl')).queryByRole('button', { name: /^Photos:/ })).not.toBeInTheDocument();

    await userEvent.click(mark);
    const dialog = await screen.findByRole('dialog', { name: 'File details' });
    expect(within(dialog).getByText('pictured.stl')).toBeInTheDocument();
  });

  it("marks a file that has a link, and the mark opens the link apart from the app", async () => {
    render(<FileManagerPage />);

    const mark = within(await itemOf('linked.stl')).getByRole('link', { name: 'Open link' });
    expect(mark).toHaveAttribute('href', 'https://www.printables.com/model/1');
    expect(mark).toHaveAttribute('target', '_blank');
    expect(mark).toHaveAttribute('rel', 'noopener noreferrer');

    expect(within(await itemOf('plain.stl')).queryByRole('link', { name: 'Open link' })).not.toBeInTheDocument();
    // An address that is not http(s) never becomes an href.
    expect(within(await itemOf('odd.stl')).queryByRole('link', { name: 'Open link' })).not.toBeInTheDocument();
  });
});
