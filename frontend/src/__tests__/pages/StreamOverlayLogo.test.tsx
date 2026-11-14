/**
 * The installation's logo on the stream overlay (`?logo=1`).
 *
 * OBS opens the overlay with a token and no session, so the logo is asked for
 * under that same token, at the revision the status feed names — never probed.
 * Without the parameter, or without a saved logo, the overlay is drawn exactly
 * as before (`StreamOverlayLegacy.test.tsx` pins that markup).
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { act, fireEvent, screen, waitFor, render as rtlRender } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { MemoryRouter, Route, Routes } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { StreamOverlayPage } from '../../pages/StreamOverlayPage';
import { server } from '../mocks/server';
import { ThemeProvider } from '../../contexts/ThemeContext';
import { ToastProvider } from '../../contexts/ToastContext';

const printer = {
  id: 1,
  name: 'Workhorse',
  ip_address: '192.168.1.100',
  serial_number: '00M09A350100001',
  access_code: '12345678',
  model: 'C12',
  camera_rotation: 0,
  enabled: true,
};

const printing = {
  id: 1,
  name: 'Workhorse',
  connected: true,
  state: 'RUNNING',
  progress: 45,
  current_print: 'Benchy.gcode.3mf',
  remaining_time: 82,
  layer_num: 150,
  total_layers: 300,
  stg_cur_name: null,
  temperatures: {},
};

function renderOverlay(query = '') {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  });
  return rtlRender(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/overlay/1${query}`]}>
        <ThemeProvider>
          <ToastProvider>
            <Routes>
              <Route path="/overlay/:printerId" element={<StreamOverlayPage />} />
            </Routes>
          </ToastProvider>
        </ThemeProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const logo = () => document.querySelector<HTMLImageElement>('img[data-overlay-logo]');
const mark = () => document.querySelector<HTMLImageElement>('img[alt="BamDude"]');
const drawn = () => waitFor(() => expect(screen.getByText('45%')).toBeInTheDocument());

describe('StreamOverlayPage — the installation logo', () => {
  const originalTitle = document.title;
  let asked: string[];
  let feedLogo: { revision: string } | null;
  let refused: boolean;
  let sessionLogo: string | null;

  beforeEach(() => {
    asked = [];
    sessionLogo = 'rev1';
    feedLogo = { revision: 'rev1' };
    refused = false;
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:overlay-logo');
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {});
    vi.stubGlobal(
      'WebSocket',
      function MockWebSocket(this: Record<string, unknown>) {
        this.close = vi.fn();
        this.onmessage = null;
        this.onerror = null;
      },
    );
    server.use(
      http.get('/api/v1/printers/:id', () => HttpResponse.json(printer)),
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json(printing)),
      http.get('/api/v1/printers/:id/overlay-status', () =>
        HttpResponse.json({ ...printing, model: 'P1S', camera_rotation: 0, gcode_file: 'plate_1.gcode', time_format: 'system', logo: feedLogo }),
      ),
      http.get('/api/v1/settings/overlay-logo/info', () => {
        asked.push('info');
        return HttpResponse.json({ exists: sessionLogo !== null, revision: sessionLogo });
      }),
      http.get('/api/v1/settings/overlay-logo', () => {
        asked.push('picture');
        return new HttpResponse(new Blob(['png']), { headers: { 'Content-Type': 'image/png' } });
      }),
      http.get('/api/v1/overlay-branding/logo', ({ request }) => {
        // What was asked, exactly as it left: the order of the parameters matters.
        asked.push(new URL(request.url).search);
        if (refused) return HttpResponse.json({ detail: 'Valid overlay token required.' }, { status: 401 });
        return new HttpResponse(new Blob(['png']), { headers: { 'Content-Type': 'image/png' } });
      }),
    );
  });

  afterEach(() => {
    document.title = originalTitle;
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  describe.each([
    ['Classic', ''],
    ['Version 2', '&design=2'],
  ])('%s, opened by OBS with a token', (_name, design) => {
    it('draws the logo at the revision the feed names, under the same token', async () => {
      renderOverlay(`?token=obs-tok&logo=1&camera=false${design}`);
      await drawn();

      await waitFor(() => expect(logo()).not.toBeNull());
      // Fetched and shown from a blob: an <img> on the API would have its token
      // swapped for the signed-in user's media token in a signed-in browser.
      expect(logo()!.getAttribute('src')).toBe('blob:overlay-logo');
      // The overlay's token reads its own route, the revision in the address
      // BEFORE the token — and nothing is asked of the session's routes.
      expect(asked).toEqual(['?v=rev1&token=obs-tok']);
      // Beside the BamDude mark, never instead of it.
      expect(mark()).not.toBeNull();
    });

    it('draws no logo when the token may not read it', async () => {
      refused = true;
      renderOverlay(`?token=obs-tok&logo=1&camera=false${design}`);
      await drawn();

      await waitFor(() => expect(asked).toEqual(['?v=rev1&token=obs-tok']));
      expect(logo()).toBeNull();
      expect(mark()).not.toBeNull();
    });

    it('draws no logo when the installation has none, and asks for none', async () => {
      feedLogo = null;
      renderOverlay(`?token=obs-tok&logo=1&camera=false${design}`);
      await drawn();

      expect(logo()).toBeNull();
      expect(asked).toEqual([]);
    });

    it('draws no logo without the parameter', async () => {
      renderOverlay(`?token=obs-tok&camera=false${design}`);
      await drawn();

      expect(logo()).toBeNull();
    });

    it.each(['logo=true', 'logo=yes', 'logo=11', 'logo=', 'logo=0', 'logo=%201', 'Logo=1'])(
      'ignores "%s"',
      async (param) => {
        renderOverlay(`?token=obs-tok&camera=false&${param}${design}`);
        await drawn();

        expect(logo()).toBeNull();
      },
    );

    it('takes the logo down when it cannot be loaded', async () => {
      renderOverlay(`?token=obs-tok&logo=1&camera=false${design}`);
      await drawn();
      await waitFor(() => expect(logo()).not.toBeNull());

      fireEvent.error(logo()!);

      await waitFor(() => expect(logo()).toBeNull());
      expect(mark()).not.toBeNull();
    });

    it('follows a logo that was replaced', async () => {
      renderOverlay(`?token=obs-tok&logo=1&camera=false${design}`);
      await drawn();
      await waitFor(() => expect(logo()).not.toBeNull());

      feedLogo = { revision: 'rev2' };

      await waitFor(() => expect(asked).toEqual(['?v=rev1&token=obs-tok', '?v=rev2&token=obs-tok']), {
        timeout: 4000,
      });
      await waitFor(() => expect(logo()).not.toBeNull());
    });
  });

  describe('signed in, without a token', () => {
    it.each(['', '&design=2'])('draws the saved logo through the session (%s)', async (design) => {
      renderOverlay(`?logo=1&camera=false${design}`);
      await drawn();

      await waitFor(() => expect(logo()).not.toBeNull());
      expect(logo()!.getAttribute('src')).toBe('blob:overlay-logo');
      expect(asked).toEqual(['info', 'picture']);
    });

    // A signed-in overlay has no feed that names the logo: it asks the
    // session's route, and must keep asking — the page stays open for hours.
    it('notices a logo that was replaced, and one that was removed', async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      try {
        renderOverlay('?logo=1&camera=false');
        await drawn();
        await waitFor(() => expect(logo()).not.toBeNull());
        expect(asked).toEqual(['info', 'picture']);

        sessionLogo = 'rev2';
        await act(async () => { await vi.advanceTimersByTimeAsync(61_000); });
        await waitFor(() => expect(asked.filter((what) => what === 'picture')).toHaveLength(2));
        await waitFor(() => expect(logo()).not.toBeNull());

        sessionLogo = null;
        await act(async () => { await vi.advanceTimersByTimeAsync(61_000); });
        await waitFor(() => expect(logo()).toBeNull());
        expect(asked.filter((what) => what === 'picture')).toHaveLength(2);
      } finally {
        vi.useRealTimers();
      }
    }, 20000);

    it('asks nothing about a logo the address does not want', async () => {
      renderOverlay('?camera=false');
      await drawn();

      expect(logo()).toBeNull();
      expect(asked).toEqual([]);
    });
  });
});
