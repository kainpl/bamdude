import { Suspense, type ReactElement } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render as rawRender, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { MemoryRouter, Route, Routes } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { server } from '../mocks/server';
import { render } from '../utils';
import { spyOnStreamSrc } from '../streamImgSpy';
import { ThemeProvider } from '../../contexts/ThemeContext';
import { ToastProvider } from '../../contexts/ToastContext';
import { AuthProvider } from '../../contexts/AuthContext';
import { CameraTile } from '../../components/CameraTile';
import { EmbeddedCameraViewer } from '../../components/EmbeddedCameraViewer';
import { CameraPage } from '../../pages/CameraPage';
import { StreamOverlayPage } from '../../pages/StreamOverlayPage';
import { printerSource } from '../../utils/cameraSource';

// An MJPEG <img> starts loading the moment it is given its URL — attached to
// the page or not. A URL passed as a prop reaches the element while React
// renders, so the img of a render React throws away (a suspended or
// interrupted transition; StrictMode's replay) holds a stream nobody can
// close: the printer's one camera socket and the chamber-light lease with it.
// Every surface gives its stream URL to a MOUNTED img only (upstream #3431).

const settle = () => act(async () => { await Promise.resolve(); });

/** Suspends its first render, so React throws away the render beside it. */
function suspendOnce() {
  let done = false;
  let resolve!: () => void;
  const promise = new Promise<void>((r) => {
    resolve = () => {
      done = true;
      r();
    };
  });
  function Gate() {
    if (!done) throw promise;
    return null;
  }
  return { Gate, release: () => act(async () => resolve()) };
}

function renderRoute(path: string, route: string, element: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return rawRender(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <ThemeProvider>
          <AuthProvider>
            <ToastProvider>
              <Routes>
                <Route path={route} element={element} />
              </Routes>
            </ToastProvider>
          </AuthProvider>
        </ThemeProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

let spy: ReturnType<typeof spyOnStreamSrc>;

beforeEach(() => {
  spy = spyOnStreamSrc();
  localStorage.removeItem('cameraWindowState');
  vi.stubGlobal('WebSocket', function MockWebSocket(this: Record<string, unknown>) {
    this.close = vi.fn();
  });
  server.use(
    http.get('/api/v1/printers/:id', () => HttpResponse.json({ id: 7, name: 'H2C', camera_rotation: 0 })),
    http.get('/api/v1/printers/:id/status', () => HttpResponse.json({ connected: true, state: 'IDLE', progress: 0 })),
    http.post('/api/v1/printers/:id/camera/stop', () => HttpResponse.json({ stopped: 0 })),
    http.get('/api/v1/printers/:id/camera/status', () => HttpResponse.json({ active: true, stalled: false })),
    http.post('/api/v1/printers/camera/stream-token', () => HttpResponse.json({ token: 'test-stream-token' })),
  );
});

afterEach(() => {
  spy.restore();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const expectOneAttachedStream = (path: string) => {
  expect(spy.detached()).toEqual([]);
  expect(spy.leaked()).toEqual([]);
  expect(spy.records.map((record) => record.src.split('?')[0])).toEqual([path]);
};

describe('a camera view opens one stream, on a mounted image', () => {
  it('the Cam Wall live tile', async () => {
    render(<CameraTile source={printerSource(7)} name="H2C" mode="live" snapshotIntervalMs={5000} connected />);
    await settle();

    expect((screen.getByAltText('H2C') as HTMLImageElement).src).toContain('/api/v1/printers/7/camera/stream');
    expectOneAttachedStream('/api/v1/printers/7/camera/stream');
  });

  it('the Cam Wall live tile opens nothing for a render React throws away', async () => {
    const { Gate, release } = suspendOnce();
    render(
      <Suspense fallback={null}>
        <CameraTile source={printerSource(7)} name="H2C" mode="live" snapshotIntervalMs={5000} connected />
        <Gate />
      </Suspense>,
    );
    await settle();
    // Nothing is on the page yet: the render beside the gate was discarded.
    expect(screen.queryByAltText('H2C')).toBeNull();
    expect(spy.records).toEqual([]);

    await release();
    await settle();
    expectOneAttachedStream('/api/v1/printers/7/camera/stream');
  });

  // The wall switches a mounted tile between snapshot and live by its props.
  // A selection is ONE request: the printer's firmware allows one camera
  // socket, and connect → abort → connect is how it gets stuck.
  it('a mounted Cam Wall tile switched from snapshot to live', async () => {
    const tile = (mode: 'live' | 'snapshot') => (
      <CameraTile source={printerSource(7)} name="H2C" mode={mode} snapshotIntervalMs={5000} connected />
    );
    const view = render(tile('snapshot'));
    await settle();
    expect(spy.records).toEqual([]);

    view.rerender(tile('live'));
    await settle();

    expectOneAttachedStream('/api/v1/printers/7/camera/stream');
  });

  it('a mounted Cam Wall tile switched live, to snapshot and live again opens one stream per selection', async () => {
    const tile = (mode: 'live' | 'snapshot') => (
      <CameraTile source={printerSource(7)} name="H2C" mode={mode} snapshotIntervalMs={5000} connected />
    );
    const view = render(tile('live'));
    await settle();
    const first = spy.records.map((record) => record.src);
    expect(first).toHaveLength(1);

    view.rerender(tile('snapshot'));
    await settle();
    expect(spy.records).toHaveLength(1);

    view.rerender(tile('live'));
    await settle();

    expect(spy.detached()).toEqual([]);
    expect(spy.leaked()).toEqual([]);
    expect(spy.records).toHaveLength(2);
    // A new selection is a new address, never the one the browser has just aborted.
    expect(spy.records[1].src).not.toBe(first[0]);
  });

  it('the floating camera window, also beside a render React throws away', async () => {
    const { Gate, release } = suspendOnce();
    render(
      <Suspense fallback={null}>
        <EmbeddedCameraViewer source={printerSource(7)} name="H2C" onClose={() => {}} />
        <Gate />
      </Suspense>,
    );
    await settle();
    expect(spy.records).toEqual([]);

    await release();
    await waitFor(() => expect(screen.getByAltText('Camera stream').getAttribute('src')).toContain('/camera/stream'));
    expectOneAttachedStream('/api/v1/printers/7/camera/stream');
  });

  it('the camera page', async () => {
    renderRoute('/camera/7', '/camera/:printerId', <CameraPage />);

    await waitFor(() => expect(screen.getByAltText('Camera stream').getAttribute('src')).toContain('/camera/stream'));
    await settle();
    expectOneAttachedStream('/api/v1/printers/7/camera/stream');
  });

  it('the stream overlay', async () => {
    renderRoute('/overlay/7', '/overlay/:printerId', <StreamOverlayPage />);

    await waitFor(() => expect(screen.getByAltText('Camera stream').getAttribute('src')).toContain('/camera/stream'));
    await settle();
    expectOneAttachedStream('/api/v1/printers/7/camera/stream');
  });

  it('the stream overlay closes its stream when it goes', async () => {
    const view = renderRoute('/overlay/7', '/overlay/:printerId', <StreamOverlayPage />);
    const img = (await screen.findByAltText('Camera stream')) as HTMLImageElement;
    await waitFor(() => expect(img.getAttribute('src')).toContain('/camera/stream'));

    view.unmount();

    expect(img.src).toMatch(/^data:image\/gif;/);
  });
});
