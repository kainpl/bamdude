/**
 * `?layout=landscape|portrait`: the overlay drawn on a fixed canvas and scaled
 * to the browser source, with a portrait arrangement of both designs.
 *
 * Without the parameter the overlay is sized by its window, as it always was.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { screen, waitFor, render as rtlRender } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { MemoryRouter, Route, Routes } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { StreamOverlayPage } from '../../pages/StreamOverlayPage';
import { server } from '../mocks/server';
import { ThemeProvider } from '../../contexts/ThemeContext';
import { ToastProvider } from '../../contexts/ToastContext';
import { stubBoxSize } from '../stubBoxSize';

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
  current_print: 'A_rather_long_part_name_that_does_not_fit_one_line.gcode.3mf',
  remaining_time: 82,
  layer_num: 150,
  total_layers: 300,
  stg_cur_name: null,
  temperatures: {},
};

const fileName = 'A_rather_long_part_name_that_does_not_fit_one_line';

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

const host = () => document.querySelector<HTMLElement>('[data-overlay-canvas]');
const canvas = () => host()?.firstElementChild as HTMLElement | undefined;
const v2 = () => document.querySelector<HTMLElement>('[data-overlay-design="2"]');
const isHost = (element: HTMLElement) => element.hasAttribute('data-overlay-canvas');

function viewportIs(orientation: 'portrait' | 'landscape') {
  vi.mocked(window.matchMedia).mockImplementation((query: string) => ({
    matches: query.includes('portrait') ? orientation === 'portrait' : orientation === 'landscape',
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));
}

describe('StreamOverlayPage — layout', () => {
  const originalTitle = document.title;

  beforeEach(() => {
    vi.stubGlobal(
      'WebSocket',
      function MockWebSocket(this: Record<string, unknown>) {
        this.close = vi.fn();
        this.onmessage = null;
        this.onerror = null;
      },
    );
    viewportIs('landscape');
    server.use(
      http.get('/api/v1/printers/:id', () => HttpResponse.json(printer)),
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json(printing)),
      http.get('/api/v1/printers/:id/overlay-status', () =>
        HttpResponse.json({ ...printing, model: 'P1S', camera_rotation: 0, gcode_file: null, time_format: 'system' }),
      ),
    );
  });

  afterEach(() => {
    document.title = originalTitle;
    vi.unstubAllGlobals();
  });

  describe('the canvas', () => {
    it.each(['', '?layout=', '?layout=sideways', '?layout=PORTRAIT', '?layout=portrait%20', '?layout=1080x1920'])(
      'is not used for "%s": the overlay is sized by its window',
      async (query) => {
        renderOverlay(query);

        await waitFor(() => expect(screen.getByText('45%')).toBeInTheDocument());
        expect(host()).toBeNull();
        expect(screen.getByText('45%').closest('.min-h-screen')).not.toBeNull();
      },
    );

    it.each([
      ['landscape', '1920px', '1080px'],
      ['portrait', '1080px', '1920px'],
    ])('draws layout=%s at %s by %s', async (layout, width, height) => {
      renderOverlay(`?layout=${layout}`);

      await waitFor(() => expect(host()).not.toBeNull());
      expect(host()!.getAttribute('data-overlay-canvas')).toBe(layout);
      expect(canvas()!.style.width).toBe(width);
      expect(canvas()!.style.height).toBe(height);
      // The overlay fills the canvas, not the window.
      expect(screen.getByText('45%').closest('.min-h-screen')).toBeNull();
      expect(canvas()!.firstElementChild!.className).toContain('h-full');
    });

    it.each([
      ['landscape', 960, 540, 0.5],
      ['portrait', 540, 960, 0.5],
      // A source of another shape: the whole canvas still fits.
      ['portrait', 1920, 1080, 0.5625],
      ['landscape', 1080, 1920, 0.5625],
      ['landscape', 3840, 2160, 2],
    ])('scales a %s canvas into a %i by %i source', async (layout, width, height, scale) => {
      const restore = stubBoxSize(isHost, width, height);
      try {
        renderOverlay(`?layout=${layout}`);

        await waitFor(() => expect(host()).not.toBeNull());
        expect(canvas()!.style.transform).toContain(`scale(${scale})`);
      } finally {
        restore();
      }
    });

    it('works in OBS, with a token', async () => {
      renderOverlay('?token=obs-tok&layout=portrait&show=model,progress');

      await waitFor(() => expect(host()).not.toBeNull());
      expect(screen.getByText('P1S')).toBeInTheDocument();
    });
  });

  describe('Classic in portrait', () => {
    it('lets a long file name wrap instead of cutting it', async () => {
      renderOverlay('?layout=portrait');

      await waitFor(() => expect(screen.getByText(fileName)).toBeInTheDocument());
      expect(screen.getByText(fileName).className).not.toContain('truncate');
      expect(screen.getByText(fileName).className).toContain('break-words');
    });

    it('keeps one line in landscape', async () => {
      renderOverlay('?layout=landscape');

      await waitFor(() => expect(screen.getByText(fileName)).toBeInTheDocument());
      expect(screen.getByText(fileName).className).toContain('truncate');
    });

    it('draws larger text than landscape at the same size', async () => {
      const first = renderOverlay('?layout=landscape');
      await waitFor(() => expect(screen.getByText('45%')).toBeInTheDocument());
      const landscape = screen.getByText(fileName).className;
      first.unmount();

      renderOverlay('?layout=portrait');
      await waitFor(() => expect(screen.getByText('45%')).toBeInTheDocument());
      const portrait = screen.getByText(fileName).className;

      expect(landscape).toContain('text-xl');
      expect(portrait).toContain('text-4xl');
    });
  });

  describe('Version 2', () => {
    it('follows the shape of its window when the URL names no layout', async () => {
      viewportIs('portrait');
      renderOverlay('?design=2');

      await waitFor(() => expect(v2()).not.toBeNull());
      expect(v2()!.getAttribute('data-layout')).toBe('portrait');
      expect(host()).toBeNull();
      expect(v2()!.hasAttribute('data-canvas')).toBe(false);
    });

    it.each([
      ['portrait', 'landscape'],
      ['landscape', 'portrait'],
    ] as const)('draws layout=%s whatever the window is (%s)', async (layout, viewport) => {
      viewportIs(viewport);
      renderOverlay(`?design=2&layout=${layout}`);

      await waitFor(() => expect(v2()).not.toBeNull());
      expect(v2()!.getAttribute('data-layout')).toBe(layout);
      expect(host()!.getAttribute('data-overlay-canvas')).toBe(layout);
      // Sized off the canvas, not the window.
      expect(v2()!.hasAttribute('data-canvas')).toBe(true);
      expect(v2()!.style.getPropertyValue('--so2-unit')).toBe('10.8px');
    });

    it('leaves the bars around the canvas see-through with the ground', async () => {
      renderOverlay('?design=2&layout=landscape&backgroundTransparency=100');

      await waitFor(() => expect(host()).not.toBeNull());
      expect(host()!.style.background).toBe('transparent');
      document.body.style.backgroundColor = '';
    });
  });
});
