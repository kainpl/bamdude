import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { api, ApiError, type OverlayStatus } from '../../api/client';
import { StreamOverlayPage } from '../../pages/StreamOverlayPage';
import { printerSource } from '../../utils/cameraSource';

// An OBS browser source runs for a whole print. A stream the server ends
// cleanly fires no `error` on the image, so the broadcast kept a frozen frame
// until somebody reloaded the source. The overlay now learns it from the
// status it already polls with its own token, and re-opens.

const overlay: OverlayStatus = {
  id: 1, name: 'Kiosk', camera_rotation: 0, connected: true, state: 'IDLE', progress: 0,
  current_print: null, gcode_file: null, remaining_time: null, layer_num: null, total_layers: null,
  stg_cur_name: null, temperatures: {}, time_format: 'system',
};
const ALIVE = { active: true, stalled: false };
const ENDED = { active: false, stalled: false };
const VIEWER_ID = /^[A-Za-z0-9_-]{8,32}$/;
/** The id the image put on its stream request. */
const viewerOf = (img: HTMLImageElement) => new URL(img.src).searchParams.get('viewer');

const advance = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });
const image = () => screen.getByAltText('Camera stream') as HTMLImageElement;

function mount(search: string) {
  const client = new QueryClient({ defaultOptions: { queries: { gcTime: 0, retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/overlay/1${search}`]}>
        <Routes><Route path="/overlay/:printerId" element={<StreamOverlayPage />} /></Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal('WebSocket', function MockWebSocket(this: Record<string, unknown>) {
    this.close = vi.fn();
  });
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe('the stream overlay in OBS (token in the URL)', () => {
  it('re-opens a stream its own feed says has ended, closing the old image first', async () => {
    const feed = vi.spyOn(api, 'getOverlayStatus').mockResolvedValue({ ...overlay, camera: ENDED });
    const sessionStatus = vi.spyOn(api, 'getCameraSourceStatus');
    mount('?token=kiosk-token');
    await advance(100);
    const first = image();
    const firstUrl = first.src;
    expect(firstUrl).toContain('token=kiosk-token');

    await advance(5000);
    expect(image()).toBe(first); // one bad read is a blip
    await advance(5000);
    expect(screen.queryByAltText('Camera stream')).toBeNull();
    expect(first.src).toMatch(/^data:image\/gif;/);

    await advance(2000);
    expect(image()).not.toBe(first);
    expect(image().src).toContain('/api/v1/printers/1/camera/stream');
    expect(image().src).toContain('token=kiosk-token');
    expect(image().src).not.toBe(firstUrl);
    // The token cannot read the session-only status route, and never tries.
    expect(sessionStatus).not.toHaveBeenCalled();
    // The feed is asked with the token and — once a stream is open — its id; nothing else.
    for (const args of feed.mock.calls) {
      expect(args.slice(0, 2)).toEqual([1, 'kiosk-token']);
      if (args.length > 2) expect(args).toEqual([1, 'kiosk-token', expect.stringMatching(VIEWER_ID)]);
    }
  });

  // Two overlays of one printer (or an overlay and its owner's camera wall)
  // share one upstream: when the server ends THIS one, the camera stays "alive".
  it('re-opens its own stream when the server ended it and another viewer keeps the camera alive', async () => {
    const feed = vi.spyOn(api, 'getOverlayStatus').mockImplementation(async (_id, _token, viewer) => ({
      ...overlay,
      camera: viewer ? { ...ALIVE, viewer_attached: false } : ALIVE,
    }));
    mount('?token=kiosk-token');
    await advance(100);
    const first = image();
    const firstViewer = viewerOf(first);
    expect(firstViewer).toMatch(VIEWER_ID);
    expect(first.src.indexOf('viewer=')).toBeLessThan(first.src.indexOf('token='));

    await advance(5000);
    expect(image()).toBe(first); // one bad read is a blip
    await advance(5000);
    expect(screen.queryByAltText('Camera stream')).toBeNull();
    expect(first.src).toMatch(/^data:image\/gif;/);
    expect(feed).toHaveBeenCalledWith(1, 'kiosk-token', firstViewer);

    // The re-opened stream is attached; the feed says so about ITS id.
    feed.mockImplementation(async (_id, _token, viewer) => ({
      ...overlay,
      camera: viewer ? { ...ALIVE, viewer_attached: viewer !== firstViewer } : ALIVE,
    }));
    await advance(2000);
    const second = image();
    expect(viewerOf(second)).not.toBe(firstViewer);
    await advance(120_000);
    expect(image()).toBe(second);
    expect(feed).toHaveBeenLastCalledWith(1, 'kiosk-token', viewerOf(second));
  });

  it('does not hold an answer about the previous stream against the one just opened', async () => {
    // The feed stops answering right after it reported the FIRST stream gone:
    // that answer is about another viewer, and must not take the second down.
    const feed = vi.spyOn(api, 'getOverlayStatus').mockImplementation(async (_id, _token, viewer) => ({
      ...overlay,
      camera: viewer ? { ...ALIVE, viewer_attached: false } : ALIVE,
    }));
    mount('?token=kiosk-token');
    await advance(100);
    const first = image();
    await advance(10_000);
    expect(screen.queryByAltText('Camera stream')).toBeNull();
    feed.mockRejectedValue(new Error('network'));

    await advance(2000);
    const second = image();
    expect(second).not.toBe(first);
    await advance(120_000);

    expect(image()).toBe(second);
  });

  it('keeps a stream its feed says is alive', async () => {
    vi.spyOn(api, 'getOverlayStatus').mockResolvedValue({ ...overlay, camera: ALIVE });
    mount('?token=kiosk-token');
    await advance(100);
    const first = image();
    const url = first.src;

    await advance(300_000);

    expect(image()).toBe(first);
    expect(first.src).toBe(url);
  });

  it('does not count one stale answer twice when the feed stops answering', async () => {
    const feed = vi.spyOn(api, 'getOverlayStatus').mockResolvedValue({ ...overlay, camera: ENDED });
    mount('?token=kiosk-token');
    await advance(100);
    const first = image();
    // One bad answer was read; from now on the server is away.
    feed.mockRejectedValue(new Error('network'));

    await advance(120_000);

    expect(image()).toBe(first);
  });

  it('leaves the stream alone on a server that does not report it', async () => {
    vi.spyOn(api, 'getOverlayStatus').mockResolvedValue(overlay);
    mount('?token=kiosk-token');
    await advance(100);
    const first = image();

    await advance(120_000);

    expect(image()).toBe(first);
  });

  it('says No signal after its retries and keeps trying once a minute', async () => {
    vi.spyOn(api, 'getOverlayStatus').mockResolvedValue({ ...overlay, camera: ALIVE });
    mount('?token=kiosk-token');
    await advance(100);

    for (const wait of [2000, 4000, 8000, 16_000, 30_000]) {
      fireEvent.error(image());
      expect(screen.queryByAltText('Camera stream')).toBeNull();
      expect(screen.queryByText('No signal')).toBeNull();
      await advance(wait);
    }
    fireEvent.error(image());
    expect(screen.getByText('No signal')).toBeInTheDocument();
    expect(screen.queryByAltText('Camera stream')).toBeNull();
    // The rest of the overlay keeps working.
    expect(screen.getByText('Printer is idle')).toBeInTheDocument();

    await advance(59_000);
    expect(screen.queryByAltText('Camera stream')).toBeNull();
    await advance(1000);
    expect(image().src).toContain('/camera/stream');
    expect(screen.queryByText('No signal')).toBeNull();
  });

  it('asks for no camera at all with camera=false', async () => {
    vi.spyOn(api, 'getOverlayStatus').mockResolvedValue({ ...overlay, camera: ENDED });
    mount('?token=kiosk-token&camera=false');
    await advance(60_000);
    expect(screen.queryByAltText('Camera stream')).toBeNull();
    expect(screen.queryByText('No signal')).toBeNull();
  });
});

describe('the stream overlay in a signed-in browser', () => {
  it('stops asking the camera status once it is refused', async () => {
    vi.spyOn(api, 'getPrinter').mockResolvedValue({ id: 1, name: 'X1', camera_rotation: 0 } as never);
    vi.spyOn(api, 'getPrinterStatus').mockResolvedValue({ connected: true, state: 'IDLE', progress: 0 } as never);
    vi.spyOn(api, 'getUiPreferences').mockResolvedValue({ time_format: 'system' } as never);
    vi.spyOn(api, 'getWebSocketToken').mockResolvedValue({ token: 'ws' } as never);
    // A user without `camera:view`: the route answers 403 for as long as the page lives.
    const status = vi.spyOn(api, 'getCameraSourceStatus').mockRejectedValue(new ApiError('forbidden', 403));
    mount('');
    await advance(100);
    const first = image();

    await advance(300_000);

    expect(status).toHaveBeenCalledTimes(1);
    // The image's own `error` is still acted on.
    expect(image()).toBe(first);
    fireEvent.error(first);
    expect(screen.queryByAltText('Camera stream')).toBeNull();
  });

  it('asks the camera status route and re-opens an ended stream', async () => {
    vi.spyOn(api, 'getPrinter').mockResolvedValue({ id: 1, name: 'X1', camera_rotation: 0 } as never);
    vi.spyOn(api, 'getPrinterStatus').mockResolvedValue({ connected: true, state: 'IDLE', progress: 0 } as never);
    vi.spyOn(api, 'getUiPreferences').mockResolvedValue({ time_format: 'system' } as never);
    vi.spyOn(api, 'getWebSocketToken').mockResolvedValue({ token: 'ws' } as never);
    const status = vi.spyOn(api, 'getCameraSourceStatus').mockResolvedValue(ENDED as never);
    mount('');
    await advance(100);
    const first = image();
    const firstViewer = viewerOf(first);
    expect(firstViewer).toMatch(VIEWER_ID);

    await advance(10_000);
    expect(status).toHaveBeenCalledWith(printerSource(1), firstViewer);
    expect(screen.queryByAltText('Camera stream')).toBeNull();
    expect(first.src).toMatch(/^data:image\/gif;/);
    await advance(2000);
    expect(image()).not.toBe(first);
  });
});
