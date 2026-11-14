/**
 * Tests for the CameraPage component.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { fireEvent, screen, waitFor, render as rtlRender } from '@testing-library/react';
import { CameraPage } from '../../pages/CameraPage';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import { MemoryRouter, Route, Routes } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ThemeProvider } from '../../contexts/ThemeContext';
import { ToastProvider } from '../../contexts/ToastContext';
import { AuthProvider } from '../../contexts/AuthContext';
import { I18nextProvider } from 'react-i18next';
import i18n from '../../i18n';
import { holdsImage, stubBoxSize } from '../stubBoxSize';

// Mock navigator.sendBeacon which isn't available in jsdom
vi.stubGlobal('navigator', {
  ...navigator,
  sendBeacon: vi.fn().mockReturnValue(true),
});

const mockPrinter = {
  id: 1,
  name: 'X1 Carbon',
  ip_address: '192.168.1.100',
  serial_number: '00M09A350100001',
  access_code: '12345678',
  model: 'X1C',
  enabled: true,
};

// Custom render for CameraPage which needs specific route params
function renderCameraPage(printerId: number, search = '') {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
      mutations: { retry: false },
    },
  });

  return rtlRender(
    <QueryClientProvider client={queryClient}>
      <I18nextProvider i18n={i18n}>
        <MemoryRouter initialEntries={[`/cameras/${printerId}${search}`]}>
          <ThemeProvider>
            <AuthProvider>
              <ToastProvider>
                <Routes>
                  <Route path="/cameras/:printerId" element={<CameraPage />} />
                </Routes>
              </ToastProvider>
            </AuthProvider>
          </ThemeProvider>
        </MemoryRouter>
      </I18nextProvider>
    </QueryClientProvider>
  );
}

describe('CameraPage', () => {
  const originalTitle = document.title;

  beforeEach(() => {
    server.use(
      http.get('/api/v1/printers/:id', () => {
        return HttpResponse.json(mockPrinter);
      }),
      http.get('/api/v1/printers/:id/status', () => {
        return HttpResponse.json({
          connected: true,
          state: 'IDLE',
          progress: 0,
        });
      }),
      http.post('/api/v1/printers/:id/camera/stop', () => {
        return HttpResponse.json({ success: true });
      }),
      http.get('/api/v1/printers/:id/camera/status', () => {
        return HttpResponse.json({ active: true, stalled: false });
      }),
      // Camera stream-token gate: <img src> can't send Authorization headers,
      // so the camera URL goes via a short-lived query-param token. Tests
      // need this mocked or `streamTokenValue` stays null and the <img> src
      // is empty (#1131 fps assertions can't see the URL otherwise).
      http.post('/api/v1/printers/camera/stream-token', () => {
        return HttpResponse.json({ token: 'test-stream-token' });
      })
    );
  });

  afterEach(() => {
    document.title = originalTitle;
  });

  describe('rendering', () => {
    it('renders camera page for printer', async () => {
      renderCameraPage(1);

      // Camera page should load - look for the header with camera icon
      await waitFor(() => {
        expect(screen.getByRole('heading')).toBeInTheDocument();
      });
    });

    it('shows live and snapshot mode buttons', async () => {
      renderCameraPage(1);

      await waitFor(() => {
        // Check for translation key or translated text
        expect(screen.getByText(/Live|camera\.live/)).toBeInTheDocument();
        expect(screen.getByText(/Snapshot|camera\.snapshot/)).toBeInTheDocument();
      });
    });

    it('shows printer name in header', async () => {
      renderCameraPage(1);

      await waitFor(() => {
        expect(screen.getByText('X1 Carbon')).toBeInTheDocument();
      });
    });

    it('shows the camera diagnostic (stethoscope) button in the control bar (#1395)', async () => {
      // The diagnostic shipped wired into the embedded viewer only; window mode
      // (this page) was missing it. The control-bar button must be present here.
      renderCameraPage(1);

      await waitFor(() => {
        expect(screen.getByText('X1 Carbon')).toBeInTheDocument();
      });
      expect(screen.getByTitle('Diagnose')).toBeInTheDocument();
    });
  });

  // The page swapped a sideways picture against the viewport; the header and
  // the padding leave the video area smaller, so the picture ran under them.
  describe('camera turned sideways', () => {
    it('fits the picture to the video area, not the viewport', async () => {
      const restore = stubBoxSize(holdsImage('Camera stream'), 800, 500);
      server.use(http.get('/api/v1/printers/:id', () => HttpResponse.json({ ...mockPrinter, camera_rotation: 270 })));

      renderCameraPage(1);

      const img = await screen.findByAltText('Camera stream');
      await waitFor(() => expect(img.style.transform).toContain('rotate(270deg)'));
      await waitFor(() => expect(img).toHaveStyle({ maxWidth: '500px', maxHeight: '800px' }));
      restore();
    });

    it('leaves an upright picture at the area size', async () => {
      const restore = stubBoxSize(holdsImage('Camera stream'), 800, 500);
      server.use(http.get('/api/v1/printers/:id', () => HttpResponse.json({ ...mockPrinter, camera_rotation: 180 })));

      renderCameraPage(1);

      const img = await screen.findByAltText('Camera stream');
      await waitFor(() => expect(img.style.transform).toContain('rotate(180deg)'));
      expect(img.style.maxWidth).toBe('');
      restore();
    });
  });

  // A web app started from the iOS home screen has no browser chrome, so a
  // camera page without navigation of its own is a dead end.
  describe('home-screen web app', () => {
    const setHomeScreenApp = (value: boolean) =>
      Object.defineProperty(window.navigator, 'standalone', { configurable: true, value });
    let historyLength: { mockRestore: () => void } | undefined;
    const setHistoryLength = (length: number) => {
      historyLength = vi.spyOn(window.history, 'length', 'get').mockReturnValue(length);
    };

    afterEach(() => {
      Reflect.deleteProperty(window.navigator, 'standalone');
      historyLength?.mockRestore();
    });

    it('offers Back when there is a page to go back to', async () => {
      setHomeScreenApp(true);
      setHistoryLength(3);
      renderCameraPage(1);

      expect(await screen.findByRole('button', { name: 'Back' })).toBeInTheDocument();
      expect(screen.queryByRole('link', { name: 'Printers' })).not.toBeInTheDocument();
    });

    it('offers the way to Printers when the camera was opened cold', async () => {
      setHomeScreenApp(true);
      setHistoryLength(1);
      renderCameraPage(1);

      expect(await screen.findByRole('link', { name: 'Printers' })).toHaveAttribute('href', '/');
      expect(screen.queryByRole('button', { name: 'Back' })).not.toBeInTheDocument();
    });

    it('adds nothing to the camera window of an ordinary browser', async () => {
      setHistoryLength(3);
      renderCameraPage(1);

      await waitFor(() => expect(screen.getByText('X1 Carbon')).toBeInTheDocument());
      expect(screen.queryByRole('button', { name: 'Back' })).not.toBeInTheDocument();
      expect(screen.queryByRole('link', { name: 'Printers' })).not.toBeInTheDocument();
    });
  });

  describe('camera controls', () => {
    it('renders without crashing', async () => {
      renderCameraPage(1);

      // Just verify no crash during render
      await waitFor(() => {
        expect(document.body).toBeInTheDocument();
      });
    });
  });

  describe('fps URL parameter (#1131)', () => {
    // Stream URL only renders once a stream token is available. The page's
    // `useQuery` for the token is gated on `!!user`, which is unauthenticated
    // in tests; the `getStreamToken()` module fallback wins instead, so set
    // it directly per fps test. Cleared in afterEach so other tests aren't
    // contaminated.
    beforeEach(async () => {
      const { setStreamToken } = await import('../../api/client');
      setStreamToken('test-stream-token');
    });
    afterEach(async () => {
      const { setStreamToken } = await import('../../api/client');
      setStreamToken(null);
    });

    it('defaults to fps=15 when no query parameter is provided', async () => {
      renderCameraPage(1);

      await waitFor(() => {
        const src = (document.querySelector('img') as HTMLImageElement | null)?.getAttribute('src') || '';
        expect(src).toContain('fps=15');
      });
    });

    it('honors fps query parameter from URL', async () => {
      renderCameraPage(1, '?fps=5');

      await waitFor(() => {
        const src = (document.querySelector('img') as HTMLImageElement | null)?.getAttribute('src') || '';
        expect(src).toContain('fps=5');
      });
    });

    it('clamps fps above 30 to 30', async () => {
      renderCameraPage(1, '?fps=60');

      await waitFor(() => {
        const src = (document.querySelector('img') as HTMLImageElement | null)?.getAttribute('src') || '';
        expect(src).toContain('fps=30');
      });
    });

    it('clamps fps below 1 to 1', async () => {
      renderCameraPage(1, '?fps=0');

      await waitFor(() => {
        const src = (document.querySelector('img') as HTMLImageElement | null)?.getAttribute('src') || '';
        expect(src).toContain('fps=1');
      });
    });

    it('falls back to 15 for non-numeric fps', async () => {
      renderCameraPage(1, '?fps=invalid');

      await waitFor(() => {
        const src = (document.querySelector('img') as HTMLImageElement | null)?.getAttribute('src') || '';
        expect(src).toContain('fps=15');
      });
    });
  });

  // The page empties its own image before it re-opens the stream. An emptied
  // image fires `error`, and the page read that as the camera failing: its own
  // refresh started a "Connection lost" countdown.
  describe('the page emptying its own image', () => {
    // Signed in: the page fetches its stream token itself and keeps it. (A token
    // set by hand lasts one render — the page's own token sync replaces it.)
    beforeEach(async () => {
      const { setAuthToken } = await import('../../api/client');
      setAuthToken('test-admin-token');
    });
    afterEach(async () => {
      const { setAuthToken } = await import('../../api/client');
      setAuthToken(null);
    });

    const streamImage = async () => {
      await waitFor(() => {
        expect(screen.getByAltText('Camera stream').getAttribute('src')).toContain('/camera/stream');
      });
      return screen.getByAltText('Camera stream') as HTMLImageElement;
    };

    it('does not start a reconnect countdown when the view goes fullscreen', async () => {
      renderCameraPage(1);
      const img = await streamImage();
      fireEvent.load(img);

      fireEvent(document, new Event('fullscreenchange'));
      // What the browser fires for the `src = ''` the page has just written.
      expect(img.getAttribute('src')).toBe('');
      fireEvent.error(img);

      expect(screen.queryByText('Connection lost')).not.toBeInTheDocument();
      // The stream is opened again, on a new image.
      await waitFor(() => expect(screen.getByAltText('Camera stream')).not.toBe(img));
      expect((await streamImage()).getAttribute('src')).toContain('/camera/stream');
    });

    it('still reconnects when the camera itself fails', async () => {
      renderCameraPage(1);
      const img = await streamImage();

      fireEvent.error(img);

      expect(screen.getByText('Connection lost')).toBeInTheDocument();
    });

    // The page shares its camera's upstream with every other view of it: the
    // server can end THIS viewer while the upstream stays healthy.
    it('asks about the stream it opened, and reconnects when the server says that one is gone', async () => {
      const asked: (string | null)[] = [];
      server.use(
        http.get('/api/v1/printers/:id/camera/status', ({ request }) => {
          asked.push(new URL(request.url).searchParams.get('viewer'));
          return HttpResponse.json({ active: true, stalled: false, viewer_attached: false });
        }),
      );
      renderCameraPage(1);
      const img = await streamImage();
      const viewer = new URL(img.src).searchParams.get('viewer');
      expect(viewer).toMatch(/^[A-Za-z0-9_-]{8,32}$/);
      expect(img.src.indexOf('viewer=')).toBeLessThan(img.src.indexOf('token='));
      fireEvent.load(img);

      // Two reads in a row, five seconds apart.
      await waitFor(() => expect(screen.getByText('Connection lost')).toBeInTheDocument(), { timeout: 14_000 });
      expect(asked.length).toBeGreaterThanOrEqual(2);
      for (const id of asked) expect(id).toBe(viewer);
    }, 20_000);

    it('closes the stream of the image it shows now, not only the first one', async () => {
      const view = renderCameraPage(1);
      const first = await streamImage();
      fireEvent.load(first);

      fireEvent.click(screen.getByTitle('Restart stream'));
      await waitFor(() => expect(screen.getByAltText('Camera stream')).not.toBe(first));
      const second = await streamImage();
      expect(first.src).toMatch(/^data:image\/gif;/);

      view.unmount();
      expect(second.src).toMatch(/^data:image\/gif;/);
    });
  });

  describe('invalid printer', () => {
    it('shows invalid printer message for ID 0', async () => {
      renderCameraPage(0);

      await waitFor(() => {
        // Check for translation key or translated text
        expect(screen.getByText(/Invalid printer ID|camera\.invalidPrinterId/)).toBeInTheDocument();
      });
    });
  });
});
