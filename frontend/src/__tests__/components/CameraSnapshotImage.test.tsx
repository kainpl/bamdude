import { StrictMode } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { CameraSnapshotImage } from '../../components/CameraSnapshotImage';
import { printerSource } from '../../utils/cameraSource';

interface Pending {
  url: string;
  signal: AbortSignal;
  respond: (response: Response) => void;
}

let requests: Pending[];
let queryClient: QueryClient;

function wall(count = 1, strict = false) {
  const tiles = Array.from({ length: count }, (_, id) => <CameraSnapshotImage
    key={id} source={printerSource(id + 1)} name={`Camera ${id + 1}`} intervalMs={1000} streamToken="kiosk-test"
  />);
  return render(<QueryClientProvider client={queryClient}>
    {strict ? <StrictMode>{tiles}</StrictMode> : tiles}
  </QueryClientProvider>);
}

async function flush() {
  await act(async () => { await Promise.resolve(); });
}

async function reply(index: number, status = 200) {
  await act(async () => { requests[index].respond(new Response('jpeg', { status })); });
}

beforeEach(() => {
  requests = [];
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  vi.useFakeTimers();
  vi.stubGlobal('fetch', vi.fn((url: string, init: RequestInit) => new Promise<Response>((resolve, reject) => {
    const signal = init.signal as AbortSignal;
    signal.addEventListener('abort', () => reject(signal.reason), { once: true });
    requests.push({ url, signal, respond: resolve });
  })));
  let nextUrl = 0;
  vi.stubGlobal('URL', class extends URL {
    static createObjectURL = vi.fn(() => `blob:frame-${++nextUrl}`);
    static revokeObjectURL = vi.fn();
  });
  // jsdom has no image decoder. Browser acceptance covers the real decoder.
  Object.defineProperty(HTMLImageElement.prototype, 'decode', {
    configurable: true, value: vi.fn().mockResolvedValue(undefined),
  });
});

afterEach(async () => {
  cleanup();
  await flush();
  queryClient.clear();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('CameraSnapshotImage', () => {
  it('keeps the same image and previous frame until the new JPEG is decoded', async () => {
    wall();
    await flush();
    expect(requests[0].url).toContain('/camera/snapshot?');
    expect(requests[0].url).toContain('token=kiosk-test');
    // The tile declares its cadence, so the server keeps the chamber light
    // between two frames instead of blinking it (services/camera_light).
    expect(requests[0].url).toContain('poll=1000');
    await reply(0);
    const image = screen.getByAltText('Camera 1');
    expect(image).toHaveAttribute('src', 'blob:frame-1');
    const timestamp = document.querySelector('time')!;
    const firstTime = timestamp.dateTime;

    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(requests).toHaveLength(2);
    expect(image).toHaveAttribute('src', 'blob:frame-1');
    let decoded!: () => void;
    vi.mocked(HTMLImageElement.prototype.decode).mockImplementationOnce(() => new Promise(resolve => { decoded = resolve; }));
    await reply(1);
    expect(image).toHaveAttribute('src', 'blob:frame-1');
    expect(timestamp.dateTime).toBe(firstTime);
    await act(async () => { decoded(); });
    expect(screen.getByAltText('Camera 1')).toBe(image);
    expect(image).toHaveAttribute('src', 'blob:frame-2');
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:frame-1');
    expect(timestamp.dateTime).not.toBe(firstTime);
  });

  it('retains the last frame on HTTP errors and automatically recovers', async () => {
    wall();
    await flush();
    await reply(0);
    const firstTime = document.querySelector('time')!.dateTime;
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    await reply(1, 503);
    expect(document.querySelector('time')!.dateTime).toBe(firstTime);
    expect(screen.getByAltText('Camera 1')).toHaveAttribute('src', 'blob:frame-1');
    expect(screen.getByRole('status')).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    await reply(2);
    expect(screen.getByAltText('Camera 1')).toHaveAttribute('src', 'blob:frame-2');
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('never opens more than two snapshot requests and grants queued tiles a turn after timeout', async () => {
    wall(4);
    await flush();
    expect(requests).toHaveLength(2);
    await act(async () => { await vi.advanceTimersByTimeAsync(19_999); });
    expect(requests).toHaveLength(2);
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(requests[0].signal.aborted).toBe(true);
    expect(requests[1].signal.aborted).toBe(true);
    expect(requests).toHaveLength(4);
    expect(requests[2].url).toContain('/printers/3/');
    expect(requests[3].url).toContain('/printers/4/');
  });

  it('also aborts a stalled body after the response headers arrived', async () => {
    wall(3);
    await flush();
    const response = new Response();
    vi.spyOn(response, 'blob').mockImplementation(() => new Promise((_, reject) => {
      requests[0].signal.addEventListener('abort', () => reject(requests[0].signal.reason), { once: true });
    }));
    await act(async () => { requests[0].respond(response); });
    await act(async () => { await vi.advanceTimersByTimeAsync(20_000); });
    expect(requests[0].signal.aborted).toBe(true);
    expect(requests[2].url).toContain('/printers/3/');
  });

  it('cancels pending HTTP and queued work on navigation; no requests start after unmount', async () => {
    const { unmount } = wall(4);
    await flush();
    await act(async () => { unmount(); });
    expect(requests.every(request => request.signal.aborted)).toBe(true);
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(requests).toHaveLength(2);
    wall();
    await flush();
    expect(requests).toHaveLength(3); // Every occupied slot was released.
  });

  it('survives Strict Mode setup/cleanup replay and releases the displayed blob on unmount', async () => {
    const { unmount } = wall(1, true);
    await flush();
    expect(requests).toHaveLength(1);
    await reply(0);
    expect(screen.getByAltText('Camera 1')).toHaveAttribute('src', 'blob:frame-1');
    unmount();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:frame-1');
  });
});
