/**
 * The stream overlay's second design (`?design=2`) and its see-through ground.
 *
 * Classic stays the default: only `design=2` selects Version 2, and an address
 * pasted into a scene before the parameter existed draws Classic, unchanged
 * (`StreamOverlayLegacy.test.tsx` pins the markup).
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
  current_print: 'Benchy.gcode.3mf',
  remaining_time: 82,
  layer_num: 150,
  total_layers: 300,
  stg_cur_name: null,
  temperatures: { nozzle: 219.6, nozzle_target: 220, bed: 55, bed_target: 60 },
};

const everything = 'show=printer,model,filename,status,progress,layers,eta,nozzle,bed';

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

const v2 = () => document.querySelector<HTMLElement>('[data-overlay-design="2"]');

describe('StreamOverlayPage — design', () => {
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
        HttpResponse.json({ ...printing, model: 'P1S', camera_rotation: 0, gcode_file: 'plate_1.gcode', time_format: 'system' }),
      ),
    );
  });

  afterEach(() => {
    document.title = originalTitle;
    document.body.style.backgroundColor = '';
    vi.unstubAllGlobals();
  });

  describe('which design is drawn', () => {
    it.each(['', '?design=1', '?design=classic', '?design=banana', '?design=', '?design=22', '?design=2x', '?artwork=2'])(
      'draws Classic for "%s"',
      async (query) => {
        renderOverlay(query);

        await waitFor(() => expect(screen.getByText('45%')).toBeInTheDocument());
        expect(v2()).toBeNull();
      },
    );

    it('draws Version 2 for design=2', async () => {
      renderOverlay('?design=2');

      await waitFor(() => expect(v2()).not.toBeNull());
    });

    it('draws Version 2 in OBS too', async () => {
      renderOverlay(`?token=obs-tok&design=2&${everything}`);

      await waitFor(() => expect(v2()).not.toBeNull());
      expect(screen.getByText('P1S')).toBeInTheDocument();
    });
  });

  // `size=` reaches the page from an address anybody can hand to a streamer.
  describe('a size the address made up', () => {
    const hostile = [
      'huge',
      'LARGE',
      'small large',
      '"><script>window.__pwned=1</script>',
      "x' onmouseover='window.__pwned=1",
      'large]{position:fixed}',
      '__proto__',
    ];

    it.each(hostile)('Version 2 draws medium for size=%s, and takes nothing from it', async (size) => {
      const { container } = renderOverlay(`?design=2&size=${encodeURIComponent(size)}`);

      await waitFor(() => expect(v2()).not.toBeNull());
      // One of the three sizes the stylesheet knows — never the address's text.
      expect(v2()).toHaveAttribute('data-size', 'medium');
      expect(container.querySelector('script')).toBeNull();
      expect(container.innerHTML).not.toContain('__pwned');
      expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
      expect(screen.getByText('45%')).toBeInTheDocument();
    });

    it.each(hostile)('Classic draws medium for size=%s', async (size) => {
      const { container } = renderOverlay(`?size=${encodeURIComponent(size)}`);
      const medium = renderOverlay('?size=medium');

      await waitFor(() => expect(screen.getAllByText('45%')).toHaveLength(2));
      const classes = (root: HTMLElement) => Array.from(root.querySelectorAll('[class]'), (el) => el.getAttribute('class'));
      expect(classes(container)).toEqual(classes(medium.container));
      expect(container.innerHTML).not.toContain('__pwned');
    });

    it.each(['small', 'medium', 'large'])('Version 2 keeps the real size %s', async (size) => {
      renderOverlay(`?design=2&size=${size}`);

      await waitFor(() => expect(v2()).not.toBeNull());
      expect(v2()).toHaveAttribute('data-size', size);
    });
  });

  describe('Version 2', () => {
    it('draws the identity, the job, a status pill, a labelled bar and the readings', async () => {
      renderOverlay(`?design=2&${everything}`);

      await waitFor(() => expect(v2()).not.toBeNull());
      expect(screen.getByText('Workhorse')).toBeInTheDocument();
      expect(screen.getByText('P1S')).toBeInTheDocument();
      expect(screen.getByRole('heading', { name: 'Benchy' })).toBeInTheDocument();
      expect(screen.getByText('Printing')).toBeInTheDocument();
      expect(v2()!.getAttribute('data-state')).toBe('RUNNING');

      const bar = screen.getByRole('progressbar', { name: 'Progress' });
      expect(bar.getAttribute('aria-valuenow')).toBe('45');
      expect(screen.getByText('45%')).toBeInTheDocument();

      expect(screen.getByText('Layers')).toBeInTheDocument();
      expect(screen.getByText('150 / 300')).toBeInTheDocument();
      expect(screen.getByText('Remaining')).toBeInTheDocument();
      expect(screen.getByText('Finish')).toBeInTheDocument();
      // A reading at its target shows one number; one still climbing shows both.
      expect(screen.getByText('220°C')).toBeInTheDocument();
      expect(screen.getByText('55°C', { exact: false })).toBeInTheDocument();
      expect(screen.getByText('/ 60°C')).toBeInTheDocument();
    });

    it('draws only what the URL asks for', async () => {
      renderOverlay('?design=2&show=progress');

      await waitFor(() => expect(screen.getByRole('progressbar')).toBeInTheDocument());
      expect(screen.queryByText('Workhorse')).not.toBeInTheDocument();
      expect(screen.queryByRole('heading')).not.toBeInTheDocument();
      expect(screen.queryByText('Printing')).not.toBeInTheDocument();
      expect(screen.queryByText('Layers')).not.toBeInTheDocument();
    });

    it('wears the BamDude mark and nobody else\'s', async () => {
      renderOverlay('?design=2');

      await waitFor(() => expect(v2()).not.toBeNull());
      const mark = screen.getByAltText('BamDude');
      expect(mark.getAttribute('src')).toBe('/img/brand/lockup-compact-on-dark.svg');
      expect(mark.closest('a')).toHaveAttribute('href', 'https://github.com/kainpl/bamdude');
      expect(document.body.innerHTML.toLowerCase()).not.toContain('bambuddy');
    });

    it('takes the progress colours', async () => {
      renderOverlay('?design=2&progressFrom=ff0000&progressTo=0000ff');

      await waitFor(() => expect(screen.getByRole('progressbar')).toBeInTheDocument());
      const fill = screen.getByRole('progressbar').firstElementChild as HTMLElement;
      expect(fill.style.width).toBe('45%');
      expect(fill.style.backgroundImage).toBe('linear-gradient(to right, #ff0000, #0000ff)');
    });

    it('clamps a progress the printer reports out of range', async () => {
      server.use(http.get('/api/v1/printers/:id/status', () => HttpResponse.json({ ...printing, progress: 140 })));
      renderOverlay('?design=2');

      await waitFor(() => expect(screen.getByRole('progressbar')).toBeInTheDocument());
      expect(screen.getByRole('progressbar').getAttribute('aria-valuenow')).toBe('100');
    });

    it('says the printer is offline and withholds its frozen figures', async () => {
      server.use(
        http.get('/api/v1/printers/:id/status', () => HttpResponse.json({ ...printing, connected: false })),
      );
      renderOverlay(`?design=2&${everything}`);

      await waitFor(() => expect(screen.getByText('Printer offline')).toBeInTheDocument());
      expect(v2()!.getAttribute('data-state')).toBe('OFFLINE');
      // The job that was running is still named.
      expect(screen.getByRole('heading', { name: 'Benchy' })).toBeInTheDocument();
      expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
      expect(screen.queryByText('150 / 300')).not.toBeInTheDocument();
      expect(screen.queryByText('Remaining')).not.toBeInTheDocument();
      expect(screen.queryByText('220°C')).not.toBeInTheDocument();
      expect(screen.queryByText('Printing')).not.toBeInTheDocument();
    });

    it('shows an idle printer its status and no bar', async () => {
      server.use(
        http.get('/api/v1/printers/:id/status', () =>
          HttpResponse.json({ ...printing, state: 'IDLE', current_print: null, progress: 0 }),
        ),
      );
      renderOverlay('?design=2');

      await waitFor(() => expect(screen.getByText('Idle')).toBeInTheDocument());
      expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
    });
  });

  describe('Version 2 camera', () => {
    it('opens the stream on a mounted image and never as a prop', async () => {
      renderOverlay('?design=2&fps=7');

      await waitFor(() => expect(screen.getByAltText('Camera stream')).toBeInTheDocument());
      const image = screen.getByAltText('Camera stream') as HTMLImageElement;
      expect(image.getAttribute('src')).toContain('/api/v1/printers/1/camera/stream');
      expect(image.getAttribute('src')).toContain('fps=7');
    });

    it('draws no picture with camera=false', async () => {
      renderOverlay('?design=2&camera=false');

      await waitFor(() => expect(v2()).not.toBeNull());
      expect(screen.queryByAltText('Camera stream')).not.toBeInTheDocument();
    });

    it('lays a sideways camera out in its slot, sides swapped', async () => {
      server.use(http.get('/api/v1/printers/:id', () => HttpResponse.json({ ...printer, camera_rotation: 90 })));
      const restore = stubBoxSize((element) => element.classList.contains('so2__camera'), 1600, 900);
      try {
        renderOverlay('?design=2');

        await waitFor(() => expect(screen.getByAltText('Camera stream')).toBeInTheDocument());
        const image = screen.getByAltText('Camera stream') as HTMLImageElement;
        await waitFor(() => expect(image.style.width).toBe('900px'));
        expect(image.style.height).toBe('1600px');
        expect(image.style.transform).toContain('rotate(90deg)');
      } finally {
        restore();
      }
    });
  });

  describe('background transparency', () => {
    const alpha = () => v2()!.style.getPropertyValue('--so2-alpha');

    it('fades the dark grounds and lets the page show through', async () => {
      renderOverlay('?design=2&backgroundTransparency=40');

      await waitFor(() => expect(v2()).not.toBeNull());
      expect(Number(alpha())).toBeCloseTo(0.6);
      expect(document.body.style.backgroundColor).toBe('transparent');
    });

    it('gives the page its ground back when the overlay goes', async () => {
      document.body.style.backgroundColor = 'rgb(1, 2, 3)';
      const { unmount } = renderOverlay('?design=2&backgroundTransparency=100');

      await waitFor(() => expect(v2()).not.toBeNull());
      expect(document.body.style.backgroundColor).toBe('transparent');
      unmount();
      expect(document.body.style.backgroundColor).toBe('rgb(1, 2, 3)');
    });

    it.each(['-5', '150', 'abc', '', '1e1', '12.5', ' 40', '0x10', '040abc'])(
      'ignores backgroundTransparency=%j',
      async (value) => {
        renderOverlay(`?design=2&backgroundTransparency=${encodeURIComponent(value)}`);

        await waitFor(() => expect(v2()).not.toBeNull());
        expect(alpha()).toBe('1');
        expect(document.body.style.backgroundColor).toBe('');
      },
    );

    it('means nothing to Classic', async () => {
      renderOverlay('?backgroundTransparency=40');

      await waitFor(() => expect(screen.getByText('45%')).toBeInTheDocument());
      expect(document.body.style.backgroundColor).toBe('');
    });
  });
});
