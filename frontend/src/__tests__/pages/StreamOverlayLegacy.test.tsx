/**
 * Overlay addresses pasted into an OBS scene before the optional parameters
 * existed (`model`, progress colours, `design`, `layout`) draw EXACTLY what
 * they drew.
 *
 * ⚠️ The snapshot beside this file was recorded from the overlay page as it
 * was before those parameters (commit f5ef94224), not from the page under
 * test. Do not refresh it to make a change pass: a difference here is a
 * visible change to somebody's running broadcast, and is the owner's call.
 *
 * Re-derived 2026-11-14: this test and its snapshot were run against a copy of
 * the tree at f5ef94224 (`git archive`), and passed there byte for byte.
 *
 * One thing is taken out of the markup before it is compared: the `viewer=`
 * correlation id in the camera stream's address. It is random per stream,
 * draws nothing, and did not exist at f5ef94224 — with it removed the markup
 * is what that commit drew.
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

// The clock time a print ends at depends on the machine's zone and locale.
vi.mock('../../utils/date', async (original) => ({
  ...(await original<typeof import('../../utils/date')>()),
  formatETA: () => '14:35',
}));

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
  gcode_file: 'Metadata/plate_3.gcode',
  remaining_time: 82,
  layer_num: 150,
  total_layers: 300,
  stg_cur_name: null,
  temperatures: { nozzle: 219.6, nozzle_target: 220, bed: 55, bed_target: 60, chamber: 38, chamber_target: 0 },
};

const idle = { ...printing, state: 'IDLE', progress: 0, current_print: null, gcode_file: null, remaining_time: null };

/** The markup, without the stream's random correlation id (see the note above). */
function drawn(container: HTMLElement): string {
  const html = container.innerHTML;
  const viewers = html.match(/&amp;viewer=[A-Za-z0-9_-]{8,32}(?=&amp;|")/g) ?? [];
  // At most the one camera stream carries one, and nothing else is touched.
  expect(viewers.length).toBeLessThanOrEqual(1);
  return viewers.reduce((text, viewer) => text.replace(viewer, ''), html);
}

function renderOverlay(query: string) {
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

describe('StreamOverlayPage — addresses from before the optional parameters', () => {
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
      http.get('/api/v1/printers/:id/overlay-status', () =>
        HttpResponse.json({ ...printing, model: 'P1S', camera_rotation: 0, time_format: '24h', camera: { active: true, stalled: false } }),
      ),
    );
  });

  afterEach(() => {
    document.title = originalTitle;
    vi.unstubAllGlobals();
  });

  it.each([
    ['a bare address', ''],
    ['every field, large, 10 fps', '?show=printer,filename,status,progress,layers,eta,nozzle,bed,chamber&size=large&fps=10'],
    ['status only, small, no camera', '?show=status,progress&size=small&camera=false'],
    ['OBS with a token', '?token=obs-tok&show=printer,filename,progress,layers,eta,nozzle'],
    ['OBS, no camera', '?token=obs-tok&camera=0&size=large'],
  ])('%s', async (_name, query) => {
    const { container } = renderOverlay(query);

    await waitFor(() => expect(screen.getByText('45%')).toBeInTheDocument());
    expect(drawn(container)).toMatchSnapshot();
  });

  it('an idle printer', async () => {
    server.use(http.get('/api/v1/printers/:id/status', () => HttpResponse.json(idle)));
    const { container } = renderOverlay('?show=printer,filename,status,progress,layers,eta,nozzle');

    await waitFor(() => expect(screen.getByText('Printer is idle')).toBeInTheDocument());
    expect(drawn(container)).toMatchSnapshot();
  });
});
