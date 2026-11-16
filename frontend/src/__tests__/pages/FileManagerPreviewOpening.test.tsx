/**
 * Opening a file from the File Manager.
 *
 * An image in the library had a thumbnail and nothing that could open it; it
 * now opens in the image viewer, from the same menus that offer the 3D view.
 * A PDF opens in the PDF viewer the same way.
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

// Keep WebGL / Three.js out of jsdom: the 3D viewer is somebody else's test.
vi.mock('../../components/ModelViewerModal', () => ({
  ModelViewerModal: ({ title }: { title: string }) => <div data-testid="model-viewer-modal">{title}</div>,
}));

// PDF.js stays out of this test: the viewer has its own.
vi.mock('../../components/PdfPreviewModal', () => ({
  PdfPreviewModal: ({ fileId, title, onClose }: { fileId: number; title: string; onClose: () => void }) => (
    <div data-testid="pdf-viewer" data-file-id={fileId}>
      {title}
      <button onClick={onClose}>close pdf</button>
    </div>
  ),
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
  { ...base, id: 10, filename: 'scan.tiff', file_type: 'tiff', file_tags: [], print_name: null },
  { ...base, id: 11, filename: 'manual.pdf', file_type: 'pdf', file_tags: [], print_name: null },
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
    http.get('/api/v1/library/stats', () =>
      HttpResponse.json({
        total_files: 4,
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

/** Open the ⋮ menu of the card (grid) or row (list) whose title is `name`. */
async function openMenuOf(name: string) {
  const title = await screen.findByText(name);
  const item = (title.closest('[data-file-row]') ?? title.closest('.group')) as HTMLElement;
  const trigger = within(item)
    .getAllByRole('button')
    .find((b) => b.querySelector('.lucide-ellipsis-vertical, .lucide-more-vertical'));
  if (!trigger) throw new Error('no ⋮ trigger found');
  await userEvent.click(trigger);
}

describe.each(['grid', 'list'] as const)('opening an image from the %s view', (viewMode) => {
  beforeEach(() => {
    localStorage.clear();
    localStorage.setItem('library-view-mode', viewMode);
    mockLibrary();
  });

  it('opens the picture in the viewer, from the full-image route', async () => {
    render(<FileManagerPage />);

    await openMenuOf('Photo.PNG');
    await userEvent.click(await screen.findByRole('button', { name: 'View image' }));

    const dialog = await screen.findByRole('dialog', { name: 'Photo.PNG' });
    const img = within(dialog).getByRole('img', { name: 'Photo.PNG' });
    expect(img.getAttribute('src')).toMatch(/^\/api\/v1\/library\/files\/7\/image(\?token=.*)?$/);

    await userEvent.click(within(dialog).getByRole('button', { name: 'Close' }));
    expect(screen.queryByRole('dialog', { name: 'Photo.PNG' })).not.toBeInTheDocument();
  });

  it('opens a PDF in the PDF viewer, by the id of the file', async () => {
    render(<FileManagerPage />);

    await openMenuOf('manual.pdf');
    await userEvent.click(await screen.findByRole('button', { name: 'View PDF' }));

    const viewer = await screen.findByTestId('pdf-viewer');
    expect(viewer).toHaveTextContent('manual.pdf');
    expect(viewer.dataset.fileId).toBe('11');

    await userEvent.click(within(viewer).getByRole('button', { name: 'close pdf' }));
    expect(screen.queryByTestId('pdf-viewer')).not.toBeInTheDocument();
  });

  it('offers the PDF viewer for a PDF only', async () => {
    render(<FileManagerPage />);

    for (const name of ['Lamp', 'notes.txt', 'Photo.PNG']) {
      await openMenuOf(name);
      await screen.findAllByRole('button', { name: /^Download$/ });
      expect(screen.queryByRole('button', { name: 'View PDF' })).not.toBeInTheDocument();
      await userEvent.keyboard('{Escape}');
    }
  });

  it('offers Generate thumbnail for a PDF', async () => {
    render(<FileManagerPage />);

    await openMenuOf('manual.pdf');
    expect(await screen.findByRole('button', { name: 'Generate Thumbnail' })).toBeInTheDocument();
  });

  it('offers the image viewer for pictures a browser draws, and for nothing else', async () => {
    render(<FileManagerPage />);

    for (const name of ['Lamp', 'notes.txt', 'scan.tiff', 'manual.pdf']) {
      await openMenuOf(name);
      // The menu is open (Download is always in it) and carries no image entry.
      await screen.findAllByRole('button', { name: /^Download$/ });
      expect(screen.queryByRole('button', { name: 'View image' })).not.toBeInTheDocument();
      await userEvent.keyboard('{Escape}');
    }
  });
});
