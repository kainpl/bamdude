/**
 * The one floating camera window, owned by the app shell.
 *
 * Rendered WITHOUT the shared test wrapper on purpose: that wrapper carries a
 * camera provider of its own (so a page under test can open the window), and
 * these tests are about who mounts the real one and what it does off the
 * Printers page.
 */

import { useEffect, type ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider, useQueryClient } from '@tanstack/react-query';
import { Link, MemoryRouter, Route, Routes } from 'react-router';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import { spyOnStreamSrc } from '../streamImgSpy';
import { ThemeProvider } from '../../contexts/ThemeContext';
import { ToastProvider } from '../../contexts/ToastContext';
import { AuthProvider, useAuth } from '../../contexts/AuthContext';
import { EmbeddedCameraProvider, useEmbeddedCamera } from '../../contexts/EmbeddedCameraContext';
import { Layout } from '../../components/Layout';
import { setAuthToken } from '../../api/client';

const STORAGE = 'openEmbeddedCameras';
const printers = [
  { id: 1, name: 'X1 Carbon', model: 'X1C', enabled: true },
  { id: 2, name: 'P1S Backup', model: 'P1S', enabled: true },
];
const account = (permissions: string[] | null) => ({
  id: 7, username: 'operator', role: permissions ? 'user' : 'admin', is_active: true, is_admin: !permissions,
  groups: [], permissions: permissions ?? [], created_at: '2024-01-01T00:00:00Z',
});

let cache!: QueryClient;
function CaptureCache() {
  const client = useQueryClient();
  useEffect(() => { cache = client; }, [client]);
  return null;
}

function Bare({ children, at = '/' }: { children: ReactNode; at?: string }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } } });
  return (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[at]}>
        <ThemeProvider>
          <AuthProvider>
            <ToastProvider><CaptureCache />{children}</ToastProvider>
          </AuthProvider>
        </ThemeProvider>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

/** A page that is not the Printers page, with the doors a page has. */
function OtherPage() {
  const camera = useEmbeddedCamera();
  const { refreshUser, logout } = useAuth();
  return (
    <div>
      <p>some other page</p>
      <button onClick={() => camera.open({ kind: 'printer', id: 2, name: 'P1S Backup' })}>open printer 2</button>
      <button onClick={() => camera.open({ kind: 'camera', id: 4, name: 'Drying rack' })}>open camera 4</button>
      <button onClick={() => void refreshUser()}>read account again</button>
      <button onClick={logout}>sign out</button>
    </div>
  );
}

const stream = () => screen.getByAltText('Camera stream') as HTMLImageElement;
const remembered = () => JSON.parse(localStorage.getItem(STORAGE) ?? 'null');

describe('the floating camera window of the app shell', () => {
  let spy: ReturnType<typeof spyOnStreamSrc>;

  beforeEach(() => {
    setAuthToken('test-admin-token');
    localStorage.removeItem(STORAGE);
    spy = spyOnStreamSrc();
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json(printers)),
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json({ connected: true, state: 'IDLE' })),
      http.get('/api/v1/printers/:id/camera/status', () => HttpResponse.json({ active: true, stalled: false })),
      http.post('/api/v1/printers/:id/camera/stop', () => HttpResponse.json({})),
      http.post('/api/v1/cameras/:id/stop', () => HttpResponse.json({})),
      http.get('/api/v1/cameras/', () => HttpResponse.json([
        { id: 4, name: 'Drying rack', camera_type: 'mjpeg', rotation: 0, enabled: true, location_id: null, location_name: null },
      ])),
      http.get('/api/v1/version', () => HttpResponse.json({ version: '0.1.6', build: 'test' })),
      http.get('/api/v1/settings/', () => HttpResponse.json({ check_updates: false, check_printer_firmware: false })),
      http.get('/api/v1/external-links/', () => HttpResponse.json([])),
      http.get('/api/v1/smart-plugs/', () => HttpResponse.json([])),
      http.get('/api/v1/support/debug-logging', () => HttpResponse.json({ enabled: false })),
      http.get('/api/v1/queue/', () => HttpResponse.json([])),
      http.get('/api/v1/updates/check', () => HttpResponse.json({ update_available: false })),
      http.get('/api/v1/auth/status', () => HttpResponse.json({ auth_enabled: false, requires_setup: false })),
      http.get('/api/v1/printers/developer-mode-warnings', () => HttpResponse.json([])),
      http.get('/api/v1/inbox/unread-count', () => HttpResponse.json({ unread_count: 0 })),
    );
  });

  afterEach(() => {
    spy.restore();
    localStorage.removeItem(STORAGE);
    setAuthToken('test-admin-token');
  });

  it('stays open across a route change with one stream request, and is restored on a page that is not Printers', async () => {
    localStorage.setItem(STORAGE, JSON.stringify([{ kind: 'printer', id: 1, name: 'X1 Carbon' }]));
    const view = render(
      <Bare at="/queue">
        <Routes>
          <Route element={<Layout />}>
            <Route path="queue" element={<Link to="/archives">go to archives</Link>} />
            <Route path="archives" element={<p>archives page</p>} />
          </Route>
        </Routes>
      </Bare>,
    );
    try {
      const first = await screen.findByAltText('Camera stream', {}, { timeout: 5000 }) as HTMLImageElement;
      expect(first.src).toContain('/printers/1/camera/stream?');
      expect(spy.records).toHaveLength(1);

      await userEvent.click(screen.getByText('go to archives'));
      expect(await screen.findByText('archives page')).toBeInTheDocument();

      // The same img, the same request: nothing was remounted or reopened.
      expect(stream()).toBe(first);
      expect(first.src).toContain('/printers/1/camera/stream?');
      expect(spy.records).toHaveLength(1);
      expect(spy.detached()).toEqual([]);
      // In the page (so a modal makes it inert) and below the modal layer.
      const frame = first.closest('.fixed') as HTMLElement;
      expect(frame.className).toContain('z-40');
      expect(document.body.contains(frame)).toBe(true);
    } finally {
      view.unmount();
    }
  }, 20000);

  // A middle-clicked link opens the app in a tab nobody is looking at. The
  // window is restored from storage there too — and must not open the camera
  // (and, behind it, the chamber-light lease) for a tab that was never shown.
  it('restored in a tab that is already hidden, it opens no stream until the tab is first shown', async () => {
    localStorage.setItem(STORAGE, JSON.stringify([{ kind: 'printer', id: 1, name: 'X1 Carbon' }]));
    let hidden = true;
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });
    const view = render(<Bare><EmbeddedCameraProvider><OtherPage /></EmbeddedCameraProvider></Bare>);
    try {
      // The window itself is there (its title bar), released.
      expect(await screen.findByText('Paused while this tab is in the background')).toBeInTheDocument();
      await new Promise((resolve) => setTimeout(resolve, 300));
      expect(screen.queryByAltText('Camera stream')).not.toBeInTheDocument();
      expect(spy.records).toEqual([]);

      hidden = false;
      act(() => { document.dispatchEvent(new Event('visibilitychange')); });
      expect((await screen.findByAltText('Camera stream') as HTMLImageElement).src).toContain('/printers/1/camera/stream?');
      await new Promise((resolve) => setTimeout(resolve, 100));
      expect(spy.records).toHaveLength(1);
    } finally {
      view.unmount();
      Reflect.deleteProperty(document, 'hidden');
    }
  });

  it('is not shown before its list has answered, on any page', async () => {
    localStorage.setItem(STORAGE, JSON.stringify([{ kind: 'printer', id: 2, name: 'P1S Backup' }]));
    let answer: () => void = () => {};
    const held = new Promise<void>((resolve) => { answer = resolve; });
    server.use(http.get('/api/v1/printers/', async () => { await held; return HttpResponse.json(printers); }));
    const view = render(<Bare><EmbeddedCameraProvider><OtherPage /></EmbeddedCameraProvider></Bare>);
    try {
      await screen.findByText('some other page');
      await new Promise((resolve) => setTimeout(resolve, 100));
      expect(screen.queryByAltText('Camera stream')).not.toBeInTheDocument();
      expect(remembered()).toHaveLength(1);
      answer();
      expect((await screen.findByAltText('Camera stream') as HTMLImageElement).src).toContain('/printers/2/');
    } finally {
      answer();
      view.unmount();
    }
  });

  it('another camera replaces the one in the window; Escape does not close it, its Close button does', async () => {
    localStorage.setItem(STORAGE, JSON.stringify([{ kind: 'printer', id: 1, name: 'X1 Carbon' }]));
    const view = render(<Bare><EmbeddedCameraProvider><OtherPage /></EmbeddedCameraProvider></Bare>);
    try {
      const first = await screen.findByAltText('Camera stream') as HTMLImageElement;
      await userEvent.click(screen.getByText('open camera 4'));
      await waitFor(() => expect(stream().getAttribute('src')).toContain('/cameras/4/stream?'));
      expect(first.src).toMatch(/^data:image\/gif;/);
      expect(screen.getAllByAltText('Camera stream')).toHaveLength(1);
      expect(remembered()).toEqual([{ kind: 'camera', id: 4, name: 'Drying rack' }]);

      await userEvent.click(screen.getByText('open printer 2'));
      await waitFor(() => expect(stream().getAttribute('src')).toContain('/printers/2/camera/stream?'));
      expect(screen.getAllByAltText('Camera stream')).toHaveLength(1);
      expect(spy.leaked()).toEqual([]);

      // Not a modal: Escape is not its key, and nothing around it is inert.
      const current = stream();
      fireEvent.keyDown(window, { key: 'Escape' });
      fireEvent.keyDown(document, { key: 'Escape' });
      expect(stream()).toBe(current);
      expect(screen.getByText('some other page').closest('[inert]')).toBeNull();

      const closeButton = screen.getByTitle('Close');
      expect(closeButton.tagName).toBe('BUTTON');
      await userEvent.click(closeButton);
      expect(screen.queryByAltText('Camera stream')).not.toBeInTheDocument();
      expect(current.src).toMatch(/^data:image\/gif;/);
      await waitFor(() => expect(localStorage.getItem(STORAGE)).toBeNull());
    } finally {
      view.unmount();
    }
  }, 15000);

  it('is closed and forgotten when its printer is deleted, off the Printers page', async () => {
    localStorage.setItem(STORAGE, JSON.stringify([{ kind: 'printer', id: 2, name: 'P1S Backup' }]));
    const view = render(<Bare><EmbeddedCameraProvider><OtherPage /></EmbeddedCameraProvider></Bare>);
    try {
      const open = await screen.findByAltText('Camera stream') as HTMLImageElement;
      server.use(http.get('/api/v1/printers/', () => HttpResponse.json(printers.filter((printer) => printer.id !== 2))));
      await act(async () => { await cache.invalidateQueries({ queryKey: ['printers'] }); });
      await waitFor(() => expect(screen.queryByAltText('Camera stream')).not.toBeInTheDocument());
      expect(open.src).toMatch(/^data:image\/gif;/);
      await waitFor(() => expect(localStorage.getItem(STORAGE)).toBeNull());
    } finally {
      view.unmount();
    }
  });

  it('is closed and forgotten when the user loses camera:view, off the Printers page', async () => {
    localStorage.setItem(STORAGE, JSON.stringify([{ kind: 'printer', id: 1, name: 'X1 Carbon' }]));
    server.use(http.get('/api/v1/auth/me', () => HttpResponse.json(account(['printers:read', 'camera:view']))));
    const view = render(<Bare><EmbeddedCameraProvider><OtherPage /></EmbeddedCameraProvider></Bare>);
    try {
      const open = await screen.findByAltText('Camera stream') as HTMLImageElement;
      server.use(http.get('/api/v1/auth/me', () => HttpResponse.json(account(['printers:read']))));
      await userEvent.click(screen.getByText('read account again'));
      await waitFor(() => expect(screen.queryByAltText('Camera stream')).not.toBeInTheDocument());
      expect(open.src).toMatch(/^data:image\/gif;/);
      await waitFor(() => expect(localStorage.getItem(STORAGE)).toBeNull());
    } finally {
      view.unmount();
    }
  });

  it('is closed and forgotten on sign-out', async () => {
    localStorage.setItem(STORAGE, JSON.stringify([{ kind: 'printer', id: 1, name: 'X1 Carbon' }]));
    server.use(http.post('/api/v1/auth/logout', () => HttpResponse.json({})));
    // What ProtectedRoute does: no user, no app shell.
    function Shell() {
      const { user } = useAuth();
      return user ? <EmbeddedCameraProvider><OtherPage /></EmbeddedCameraProvider> : <p>signed out</p>;
    }
    const view = render(<Bare><Shell /></Bare>);
    try {
      const open = await screen.findByAltText('Camera stream') as HTMLImageElement;
      await userEvent.click(screen.getByText('sign out'));
      expect(await screen.findByText('signed out')).toBeInTheDocument();
      expect(screen.queryByAltText('Camera stream')).not.toBeInTheDocument();
      expect(open.src).toMatch(/^data:image\/gif;/);
      expect(localStorage.getItem(STORAGE)).toBeNull();
    } finally {
      view.unmount();
    }
  });

  it('a provider inside another one does not open a second window', async () => {
    localStorage.setItem(STORAGE, JSON.stringify([{ kind: 'printer', id: 1, name: 'X1 Carbon' }]));
    const view = render(
      <Bare><EmbeddedCameraProvider><EmbeddedCameraProvider><OtherPage /></EmbeddedCameraProvider></EmbeddedCameraProvider></Bare>,
    );
    try {
      await screen.findByAltText('Camera stream');
      await new Promise((resolve) => setTimeout(resolve, 50));
      expect(screen.getAllByAltText('Camera stream')).toHaveLength(1);
      expect(spy.records).toHaveLength(1);
    } finally {
      view.unmount();
    }
  });

  // That the window does not exist outside the app shell (login, kiosk wall,
  // overlay, camera page) is pinned by `components/embeddedCameraWindowOwner.test.ts`,
  // which scans who mounts the provider and where those routes are declared. A
  // render of the bare login page here proved nothing: it mounts no provider
  // whatever the app does.
});
