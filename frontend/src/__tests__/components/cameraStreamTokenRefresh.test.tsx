import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render as rawRender, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { MemoryRouter } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { server } from '../mocks/server';
import { spyOnStreamSrc } from '../streamImgSpy';
import { ThemeProvider } from '../../contexts/ThemeContext';
import { ToastProvider } from '../../contexts/ToastContext';
import { AuthProvider } from '../../contexts/AuthContext';
import { setAuthToken, setStreamToken } from '../../api/client';
import { CameraTile } from '../../components/CameraTile';
import { useStreamTokenSync } from '../../hooks/useCameraStreamToken';
import { printerSource } from '../../utils/cameraSource';

// The camera token is refreshed every 50 minutes, and the server ends a stream
// when the token it was opened with expires — so a refresh re-opens each live
// stream ONCE, with the new token. Two things wrote a stream image's address:
// the token sync stamped every camera <img> on the page, and the image's owner
// then gave it the same address again. Two restarts of one stream, against a
// printer whose firmware allows one camera socket.

function Sync() {
  useStreamTokenSync();
  return null;
}

let spy: ReturnType<typeof spyOnStreamSrc>;
let tokens: string[];

beforeEach(() => {
  spy = spyOnStreamSrc();
  tokens = ['first-token', 'second-token'];
  setAuthToken('test-admin-token');
  server.use(
    http.post('/api/v1/printers/camera/stream-token', () =>
      HttpResponse.json({ token: tokens.length > 1 ? tokens.shift() : tokens[0] })),
    http.post('/api/v1/auth/media-token', () => HttpResponse.json({ token: 'media' })),
    http.get('/api/v1/printers/:id/camera/status', () => HttpResponse.json({ active: true, stalled: false })),
    http.post('/api/v1/printers/:id/camera/stop', () => HttpResponse.json({ stopped: 0 })),
  );
});

afterEach(() => {
  spy.restore();
  setStreamToken(null);
  vi.restoreAllMocks();
});

const withToken = (token: string) => spy.records.filter((record) => record.src.includes(`token=${token}`));

describe('a camera-token refresh in the middle of a live stream', () => {
  it('re-opens the stream once with the new token, and never without one', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
    // A new element each time, so a re-render of the page re-renders the tile.
    const page = () => (
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <ThemeProvider>
            <AuthProvider>
              <ToastProvider>
                <Sync />
                <CameraTile source={printerSource(7)} name="H2C" mode="live" snapshotIntervalMs={5000} connected />
              </ToastProvider>
            </AuthProvider>
          </ThemeProvider>
        </MemoryRouter>
      </QueryClientProvider>
    );
    const view = rawRender(page());
    const image = () => screen.getByAltText('H2C') as HTMLImageElement;
    await waitFor(() => expect(image().src).toContain('token=first-token'));
    const opened = spy.records.length;
    const viewer = new URL(image().src).searchParams.get('viewer');

    await act(async () => {
      await client.invalidateQueries({ queryKey: ['camera-stream-token'] });
    });
    await waitFor(() => expect(image().src).toContain('token=second-token'));
    // Whatever re-renders the tile next (a status push, a parent) adds nothing.
    view.rerender(page());
    await act(async () => { await Promise.resolve(); });

    expect(withToken('second-token')).toHaveLength(1);
    expect(spy.records).toHaveLength(opened + 1);
    expect(spy.records.at(-1)!.src).toContain('/api/v1/printers/7/camera/stream');
    // The same stream of the same view: only its token changed.
    expect(new URL(image().src).searchParams.get('viewer')).toBe(viewer);
    expect(spy.detached()).toEqual([]);
    expect(spy.leaked()).toEqual([]);
  });
});
