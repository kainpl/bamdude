/**
 * The stream overlay's optional URL parameters.
 *
 * The page is opened by OBS with a token and no session: a parameter it does
 * not understand is ignored, never an error, and a URL pasted into a scene
 * before a parameter existed keeps rendering what it rendered.
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

const printer = {
  id: 1,
  name: 'Workhorse',
  ip_address: '192.168.1.100',
  serial_number: '00M09A350100001',
  access_code: '12345678',
  model: 'C12',
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
  temperatures: { nozzle: 219.6, nozzle_target: 220, bed: 60, bed_target: 60 },
};

const kioskFeed = {
  ...printing,
  model: 'P1S',
  camera_rotation: 0,
  gcode_file: 'plate_1.gcode',
  time_format: 'system',
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

describe('StreamOverlayPage — optional parameters', () => {
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
    server.use(
      http.get('/api/v1/printers/:id', () => HttpResponse.json(printer)),
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json(printing)),
      http.get('/api/v1/printers/:id/overlay-status', () => HttpResponse.json(kioskFeed)),
    );
  });

  afterEach(() => {
    document.title = originalTitle;
    vi.unstubAllGlobals();
  });

  describe('printer model (show=model)', () => {
    it('draws the model beside the name, by its display name', async () => {
      renderOverlay('?show=printer,model');

      await waitFor(() => expect(screen.getByText('Workhorse')).toBeInTheDocument());
      // The row stores the internal code; the overlay never shows a code.
      expect(screen.getByText('P1S')).toBeInTheDocument();
      expect(screen.queryByText('C12')).not.toBeInTheDocument();
      // One line: the two share a row.
      expect(screen.getByText('P1S').parentElement).toBe(screen.getByText('Workhorse').parentElement);
    });

    it('draws the model alone when the name is not asked for', async () => {
      renderOverlay('?show=model');

      await waitFor(() => expect(screen.getByText('P1S')).toBeInTheDocument());
      expect(screen.queryByText('Workhorse')).not.toBeInTheDocument();
    });

    it('reads it from the token feed in OBS', async () => {
      server.use(
        http.get('/api/v1/printers/:id/overlay-status', () => HttpResponse.json({ ...kioskFeed, model: 'H2D Pro' })),
      );
      renderOverlay('?token=obs-tok&show=printer,model');

      await waitFor(() => expect(screen.getByText('H2D Pro')).toBeInTheDocument());
      expect(screen.getByText('Workhorse')).toBeInTheDocument();
    });

    it('draws the name alone when the feed of an older server names no model', async () => {
      const { model: _model, ...older } = kioskFeed;
      void _model;
      server.use(http.get('/api/v1/printers/:id/overlay-status', () => HttpResponse.json(older)));
      renderOverlay('?token=obs-tok&show=printer,model');

      await waitFor(() => expect(screen.getByText('Workhorse')).toBeInTheDocument());
      expect(screen.getByText('Workhorse').parentElement?.textContent).toBe('Workhorse');
    });

    it('is not drawn unless the URL asks for it', async () => {
      renderOverlay('?show=printer,progress');

      await waitFor(() => expect(screen.getByText('Workhorse')).toBeInTheDocument());
      expect(screen.queryByText('P1S')).not.toBeInTheDocument();
    });
  });

  describe('progress colours (progressFrom / progressTo)', () => {
    // The filled part of the bar: the one element sized by the progress.
    const bar = () => document.querySelector<HTMLElement>('[style*="width: 45%"]');

    it('fills the bar with the two colours, left to right', async () => {
      renderOverlay('?show=progress&progressFrom=ff0000&progressTo=%230000FF');

      await waitFor(() => expect(bar()).not.toBeNull());
      expect(bar()!.style.backgroundImage).toBe('linear-gradient(to right, #ff0000, #0000ff)');
    });

    it('keeps its own colour when the URL names none', async () => {
      renderOverlay('?show=progress');

      await waitFor(() => expect(bar()).not.toBeNull());
      expect(bar()!.getAttribute('style')).toBe('width: 45%;');
      expect(bar()!.className).toContain('bg-bambu-green');
    });

    it.each([
      'progressFrom=red&progressTo=blue',
      'progressFrom=%23fff&progressTo=%23000',
      'progressFrom=ff0000%3Bbackground%3Aurl(%2F%2Fevil)&progressTo=url(x)',
      'progressFrom=&progressTo=',
    ])('ignores %s and still draws the bar', async (query) => {
      renderOverlay(`?show=progress&${query}`);

      await waitFor(() => expect(bar()).not.toBeNull());
      expect(bar()!.getAttribute('style')).toBe('width: 45%;');
      expect(screen.getByText('45%')).toBeInTheDocument();
    });

    it('colours the bar in OBS too', async () => {
      renderOverlay('?token=obs-tok&show=progress&progressFrom=112233&progressTo=445566');

      await waitFor(() => expect(bar()).not.toBeNull());
      expect(bar()!.style.backgroundImage).toBe('linear-gradient(to right, #112233, #445566)');
    });
  });
});
