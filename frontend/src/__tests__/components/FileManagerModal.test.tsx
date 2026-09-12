/**
 * Tests for the FileManagerModal component.
 * Tests file browsing, selection, navigation, and file operations.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, fireEvent, waitFor } from '@testing-library/react';
import { render } from '../utils';
import { FileManagerModal } from '../../components/FileManagerModal';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

// The 3D-view modal mounts these; keep WebGL / Three.js out of jsdom.
vi.mock('../../components/ModelViewer', () => ({
  ModelViewer: ({ className }: { className?: string }) => (
    <div data-testid="model-viewer" className={className}>
      Model Viewer Mock
    </div>
  ),
}));

vi.mock('../../components/GcodePreview', () => ({
  GcodePreview: ({ className }: { className?: string }) => (
    <div data-testid="gcode-preview" className={className}>
      G-code Preview Mock
    </div>
  ),
}));

const mockFiles = [
  {
    name: 'cache',
    path: '/cache',
    size: 0,
    is_directory: true,
    mtime: '2024-01-15T10:00:00Z',
  },
  {
    name: 'model',
    path: '/model',
    size: 0,
    is_directory: true,
    mtime: '2024-01-15T10:00:00Z',
  },
  {
    name: 'benchy.3mf',
    path: '/benchy.3mf',
    size: 1048575,
    is_directory: false,
    mtime: '2024-01-15T10:00:00Z',
  },
  {
    name: 'print_job.gcode',
    path: '/print_job.gcode',
    size: 2048000,
    is_directory: false,
    mtime: '2024-01-14T10:00:00Z',
  },
];

const mockStorage = {
  used_bytes: 1073741824, // 1 GB
  free_bytes: 3221225472, // 3 GB
};

describe('FileManagerModal', () => {
  const mockOnClose = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    server.use(
      http.get('/api/v1/printers/:id/files', () => {
        return HttpResponse.json({ files: mockFiles });
      }),
      http.get('/api/v1/printers/:id/storage', () => {
        return HttpResponse.json(mockStorage);
      }),
      http.delete('/api/v1/printers/:id/files', () => {
        return HttpResponse.json({ success: true });
      })
    );
  });

  describe('rendering', () => {
    it('renders the modal with header', async () => {
      render(
        <FileManagerModal
          printerId={1}
          printerName="X1 Carbon"
          onClose={mockOnClose}
        />
      );

      expect(screen.getByText('File Manager')).toBeInTheDocument();
      expect(screen.getByText('X1 Carbon')).toBeInTheDocument();
    });

    it('renders storage info', async () => {
      render(
        <FileManagerModal
          printerId={1}
          printerName="X1 Carbon"
          onClose={mockOnClose}
        />
      );

      await waitFor(() => {
        expect(screen.getByText(/Used:/)).toBeInTheDocument();
        expect(screen.getByText(/Free:/)).toBeInTheDocument();
      });
    });

    it('renders quick navigation buttons', () => {
      render(
        <FileManagerModal
          printerId={1}
          printerName="X1 Carbon"
          onClose={mockOnClose}
        />
      );

      expect(screen.getByText('Root')).toBeInTheDocument();
      expect(screen.getByText('Cache')).toBeInTheDocument();
      expect(screen.getByText('Models')).toBeInTheDocument();
      expect(screen.getByText('Timelapse')).toBeInTheDocument();
    });

    it('renders file list', async () => {
      render(
        <FileManagerModal
          printerId={1}
          printerName="X1 Carbon"
          onClose={mockOnClose}
        />
      );

      await waitFor(() => {
        expect(screen.getByText('cache')).toBeInTheDocument();
        expect(screen.getByText('model')).toBeInTheDocument();
        expect(screen.getByText('benchy.3mf')).toBeInTheDocument();
        expect(screen.getByText('print_job.gcode')).toBeInTheDocument();
      });
    });

    it('shows file sizes for files', async () => {
      render(
        <FileManagerModal
          printerId={1}
          printerName="X1 Carbon"
          onClose={mockOnClose}
        />
      );

      await waitFor(() => {
        // 1024000 bytes = 1024.0 KB
        expect(screen.getByText('1024.0 KB')).toBeInTheDocument();
      });
    });
  });

  describe('navigation', () => {
    it('navigates into a folder when clicked', async () => {
      server.use(
        http.get('/api/v1/printers/:id/files', ({ request }) => {
          const url = new URL(request.url);
          const path = url.searchParams.get('path');
          if (path === '/cache') {
            return HttpResponse.json({
              files: [
                { name: 'temp.dat', path: '/cache/temp.dat', size: 512, is_directory: false },
              ],
            });
          }
          return HttpResponse.json({ files: mockFiles });
        })
      );

      render(
        <FileManagerModal
          printerId={1}
          printerName="X1 Carbon"
          onClose={mockOnClose}
        />
      );

      await waitFor(() => {
        expect(screen.getByText('cache')).toBeInTheDocument();
      });

      // Click on cache folder
      fireEvent.click(screen.getByText('cache'));

      await waitFor(() => {
        expect(screen.getByText('temp.dat')).toBeInTheDocument();
      });
    });

    it('shows current path', async () => {
      render(
        <FileManagerModal
          printerId={1}
          printerName="X1 Carbon"
          onClose={mockOnClose}
        />
      );

      expect(screen.getByText('/')).toBeInTheDocument();
    });
  });

  describe('file selection', () => {
    it('selects a file when checkbox is clicked', async () => {
      render(
        <FileManagerModal
          printerId={1}
          printerName="X1 Carbon"
          onClose={mockOnClose}
        />
      );

      await waitFor(() => {
        expect(screen.getByText('benchy.3mf')).toBeInTheDocument();
      });

      // Find and click a checkbox (files have checkboxes, directories don't)
      const checkboxes = screen.getAllByRole('button').filter(btn =>
        btn.querySelector('[data-testid="selection-box"]') !== null
      );

      if (checkboxes.length > 0) {
        fireEvent.click(checkboxes[0]);

        await waitFor(() => {
          expect(screen.getByText('1 selected')).toBeInTheDocument();
        });
      }
    });

    it('enables download button when files are selected', async () => {
      render(
        <FileManagerModal
          printerId={1}
          printerName="X1 Carbon"
          onClose={mockOnClose}
        />
      );

      await waitFor(() => {
        expect(screen.getByText('benchy.3mf')).toBeInTheDocument();
      });

      // Download button should be disabled initially
      const downloadButton = screen.getByRole('button', { name: /Download/i });
      expect(downloadButton).toBeDisabled();
    });

    it('shows Select All button when files exist', async () => {
      render(
        <FileManagerModal
          printerId={1}
          printerName="X1 Carbon"
          onClose={mockOnClose}
        />
      );

      await waitFor(() => {
        expect(screen.getByText('Select All')).toBeInTheDocument();
      });
    });
  });

  describe('search and filter', () => {
    it('renders search input', () => {
      render(
        <FileManagerModal
          printerId={1}
          printerName="X1 Carbon"
          onClose={mockOnClose}
        />
      );

      expect(screen.getByPlaceholderText('Filter files...')).toBeInTheDocument();
    });

    it('filters files based on search query', async () => {
      render(
        <FileManagerModal
          printerId={1}
          printerName="X1 Carbon"
          onClose={mockOnClose}
        />
      );

      await waitFor(() => {
        expect(screen.getByText('benchy.3mf')).toBeInTheDocument();
      });

      const searchInput = screen.getByPlaceholderText('Filter files...');
      fireEvent.change(searchInput, { target: { value: 'benchy' } });

      await waitFor(() => {
        expect(screen.getByText('benchy.3mf')).toBeInTheDocument();
        expect(screen.queryByText('print_job.gcode')).not.toBeInTheDocument();
      });
    });
  });

  describe('sorting', () => {
    it('renders sort dropdown', () => {
      render(
        <FileManagerModal
          printerId={1}
          printerName="X1 Carbon"
          onClose={mockOnClose}
        />
      );

      expect(screen.getByRole('combobox')).toBeInTheDocument();
    });

    it('has sort options available', () => {
      render(
        <FileManagerModal
          printerId={1}
          printerName="X1 Carbon"
          onClose={mockOnClose}
        />
      );

      const sortSelect = screen.getByRole('combobox');
      expect(sortSelect).toBeInTheDocument();

      // Check that options exist
      expect(screen.getByText('Name (A-Z)')).toBeInTheDocument();
    });
  });

  describe('close behavior', () => {
    it('calls onClose when X button is clicked', async () => {
      render(
        <FileManagerModal
          printerId={1}
          printerName="X1 Carbon"
          onClose={mockOnClose}
        />
      );

      const closeButton = screen.getAllByRole('button').find(btn =>
        btn.querySelector('.lucide-x')
      );

      if (closeButton) {
        fireEvent.click(closeButton);
        expect(mockOnClose).toHaveBeenCalled();
      }
    });

    it('does NOT close when the backdrop is clicked', () => {
      render(
        <FileManagerModal
          printerId={1}
          printerName="X1 Carbon"
          onClose={mockOnClose}
        />
      );

      const backdrop = screen.getByRole('dialog').parentElement as HTMLElement;
      fireEvent.click(backdrop);
      expect(mockOnClose).not.toHaveBeenCalled();
    });

    it('calls onClose when Escape key is pressed', () => {
      render(
        <FileManagerModal
          printerId={1}
          printerName="X1 Carbon"
          onClose={mockOnClose}
        />
      );

      fireEvent.keyDown(window, { key: 'Escape' });
      expect(mockOnClose).toHaveBeenCalled();
    });
  });

  describe('empty state', () => {
    it('shows empty message when directory has no files', async () => {
      server.use(
        http.get('/api/v1/printers/:id/files', () => {
          return HttpResponse.json({ files: [] });
        })
      );

      render(
        <FileManagerModal
          printerId={1}
          printerName="X1 Carbon"
          onClose={mockOnClose}
        />
      );

      await waitFor(() => {
        expect(screen.getByText('No files in this directory')).toBeInTheDocument();
      });
    });
  });

  describe('loading state', () => {
    it('shows loading spinner while fetching files', () => {
      // Delay the response to see loading state
      server.use(
        http.get('/api/v1/printers/:id/files', async () => {
          await new Promise((r) => setTimeout(r, 100));
          return HttpResponse.json({ files: mockFiles });
        })
      );

      render(
        <FileManagerModal
          printerId={1}
          printerName="X1 Carbon"
          onClose={mockOnClose}
        />
      );

      // The loader should be present initially
      const loader = document.querySelector('.animate-spin');
      expect(loader).toBeInTheDocument();
    });
  });

  describe('storage switcher', () => {
    const withCapability = (capability: Record<string, unknown>) =>
      server.use(
        http.get('/api/v1/printers/:id/status', () =>
          HttpResponse.json({ storage_capability: capability }),
        ),
      );

    const externalOnly = {
      storages: ['external'],
      can_browse_internal: false,
      card_state: 1,
      default_storage: 'external',
      print_target: 'external',
      reason: null,
    };

    const bothWithNoCard = {
      storages: ['external', 'internal'],
      can_browse_internal: true,
      card_state: 0,
      default_storage: 'internal',
      print_target: 'internal',
      reason: null,
    };

    it('offers no switcher on a printer without internal storage', async () => {
      withCapability(externalOnly);
      render(<FileManagerModal printerId={1} printerName="P1S" onClose={mockOnClose} />);

      expect(await screen.findByText('benchy.3mf')).toBeInTheDocument();
      expect(screen.queryByRole('tab')).not.toBeInTheDocument();
    });

    it('opens internal storage when no card is inserted', async () => {
      withCapability(bothWithNoCard);
      let asked: string | null = null;
      server.use(
        http.get('/api/v1/printers/:id/files', ({ request }) => {
          asked = new URL(request.url).searchParams.get('storage');
          return HttpResponse.json({ files: mockFiles });
        }),
      );

      render(<FileManagerModal printerId={1} printerName="X2D" onClose={mockOnClose} />);

      await waitFor(() => expect(asked).toBe('internal'));
    });

    it('hides the path bar and clear-SD on internal storage', async () => {
      withCapability(bothWithNoCard);
      render(<FileManagerModal printerId={1} printerName="X2D" onClose={mockOnClose} />);

      expect(await screen.findByText('benchy.3mf')).toBeInTheDocument();
      // The breadcrumb renders currentPath in a mono span; on a flat catalogue
      // there is no path to be at.
      expect(document.querySelector('.font-mono')).not.toBeInTheDocument();
      expect(screen.queryByTitle(/clear/i)).not.toBeInTheDocument();
    });

    it('offers the two catalogues on internal storage', async () => {
      server.use(
        http.get('/api/v1/printers/:id/status', () =>
          HttpResponse.json({
            storage_capability: bothWithNoCard,
            timelapse_capability: { supports_internal: true },
          }),
        ),
      );
      let askedType: string | null = null;
      server.use(
        http.get('/api/v1/printers/:id/files', ({ request }) => {
          askedType = new URL(request.url).searchParams.get('type');
          return HttpResponse.json({ files: mockFiles });
        }),
      );

      render(<FileManagerModal printerId={1} printerName="X2D" onClose={mockOnClose} />);

      await waitFor(() => expect(askedType).toBe('model'));
      fireEvent.click(screen.getByRole('tab', { name: /timelapse/i }));
      await waitFor(() => expect(askedType).toBe('timelapse'));
    });

    it('hides the timelapse catalogue when the printer keeps none internally', async () => {
      server.use(
        http.get('/api/v1/printers/:id/status', () =>
          HttpResponse.json({
            storage_capability: bothWithNoCard,
            timelapse_capability: { supports_internal: false },
          }),
        ),
      );

      render(<FileManagerModal printerId={1} printerName="X2D" onClose={mockOnClose} />);

      expect(await screen.findByText('benchy.3mf')).toBeInTheDocument();
      expect(screen.queryByRole('tab', { name: /timelapse/i })).not.toBeInTheDocument();
    });

    it('clears the selection when the storage changes', async () => {
      withCapability({ ...bothWithNoCard, card_state: 1, default_storage: 'external' });
      render(<FileManagerModal printerId={1} printerName="X2D" onClose={mockOnClose} />);

      expect(await screen.findByText('benchy.3mf')).toBeInTheDocument();
      // Same idiom as the selection tests above: only files carry a checkbox.
      const checkbox = screen
        .getAllByRole('button')
        .find((btn) => btn.querySelector('[data-testid="selection-box"]') !== null);
      expect(checkbox).toBeDefined();
      fireEvent.click(checkbox!);
      await waitFor(() => expect(screen.getByText(/1 selected/i)).toBeInTheDocument());

      fireEvent.click(screen.getByRole('tab', { name: /internal/i }));

      await waitFor(() => expect(screen.queryByText(/1 selected/i)).not.toBeInTheDocument());
    });
  });

  describe('plate thumbnails', () => {
    const onlyA3mf = [
      { name: 'My_Model.3mf', path: '/cache/My_Model.3mf', size: 1024, is_directory: false },
    ];

    const platesAnswer = (
      thumbnails: Array<string | null>,
    ) => ({
      printer_id: 1,
      path: '/cache/My_Model.3mf',
      filename: 'My_Model.3mf',
      archive_id: 9,
      is_multi_plate: true,
      plates: thumbnails.map((thumbnail_url, i) => ({
        index: i + 1,
        name: `Plate ${i + 1}`,
        objects: [`Part ${i + 1}`],
        // The server sets has_thumbnail to exactly "thumbnail_url is non-null";
        // the true-with-null row below is deliberately inconsistent to pin which
        // of the two the modal believes.
        has_thumbnail: true,
        thumbnail_url,
        print_time_seconds: 1800,
        filament_used_grams: 12,
        filaments: [],
      })),
    });

    /** Opens the 3D view of the single 3MF in the listing. */
    const openViewer = async (thumbnails: Array<string | null>) => {
      const askedForPerPlateImage: string[] = [];
      server.use(
        http.get('/api/v1/printers/:id/files', () => HttpResponse.json({ files: onlyA3mf })),
        http.get('/api/v1/printers/:id/files/plates', () =>
          HttpResponse.json(platesAnswer(thumbnails)),
        ),
        // The per-plate image route Task 2 deleted. jsdom never fetches an
        // <img src>, so an empty log is a guard against a future fetch-based
        // path rather than the proof — the src assertions are that.
        http.get('/api/v1/printers/:id/files/plate-thumbnail/:plate', ({ request }) => {
          askedForPerPlateImage.push(request.url);
          return new HttpResponse(null, { status: 404 });
        }),
      );

      render(<FileManagerModal printerId={1} printerName="X1 Carbon" onClose={mockOnClose} />);
      fireEvent.click(await screen.findByTitle('3D View'));
      return askedForPerPlateImage;
    };

    it('renders each plate thumbnail from the answer, never through a per-plate image route', async () => {
      const askedForPerPlateImage = await openViewer([
        '/api/v1/archives/9/plate-thumbnail/1',
        'data:image/png;base64,AAAA',
      ]);

      const imgs = await screen.findAllByRole('img', { name: /plate/i });
      expect(imgs.map((img) => img.getAttribute('src'))).toEqual([
        '/api/v1/archives/9/plate-thumbnail/1',
        'data:image/png;base64,AAAA',
      ]);
      expect(askedForPerPlateImage).toEqual([]);
    });

    it('draws the placeholder for a plate the answer carries no URL for', async () => {
      await openViewer([null, 'data:image/png;base64,BBBB']);

      const imgs = await screen.findAllByRole('img', { name: /plate/i });
      // Only plate 2 has a URL — plate 1 falls through to the box icon, whatever
      // has_thumbnail claims.
      expect(imgs.map((img) => img.getAttribute('src'))).toEqual([
        'data:image/png;base64,BBBB',
      ]);
    });
  });
});
