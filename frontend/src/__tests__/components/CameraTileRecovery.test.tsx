import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, screen } from '@testing-library/react';
import { render } from '../utils';
import { api } from '../../api/client';
import { CameraTile } from '../../components/CameraTile';
import { printerSource, standaloneSource } from '../../utils/cameraSource';

// The Cam Wall's one live tile. A stream the server ends cleanly fires no
// `error` on the image, so the tile kept its last frame under a red LIVE badge
// until the page was reloaded; and one `error` meant "No signal" for good.

const ALIVE = { active: true, stalled: false };
const ENDED = { active: false, stalled: false };
const VIEWER_ID = /^[A-Za-z0-9_-]{8,32}$/;
/** The id the image put on its stream request. */
const viewerOf = (img: HTMLImageElement) => new URL(img.src).searchParams.get('viewer');

const advance = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });
const image = () => screen.getByAltText('Live') as HTMLImageElement;
const tile = (source = printerSource(42), extra: Partial<Parameters<typeof CameraTile>[0]> = {}) => (
  <CameraTile source={source} name="Live" mode="live" snapshotIntervalMs={5000} connected {...extra} />
);

let hidden = false;
let fetchMock: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  vi.useFakeTimers();
  hidden = false;
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });
  fetchMock = vi.spyOn(global, 'fetch').mockImplementation(async () => new Response('{}', { status: 200 }));
});
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  delete (document as unknown as { hidden?: boolean }).hidden;
});

const stopCalls = () => fetchMock.mock.calls.filter((call: unknown[]) => String(call[0]).includes('/stop'));

describe('a live Cam Wall tile whose stream ends', () => {
  it('re-opens a stream the server says has ended, closing the old image first and stopping nobody', async () => {
    const status = vi.spyOn(api, 'getCameraSourceStatus').mockResolvedValue(ENDED as never);
    render(tile());
    await advance(0);
    const first = image();
    const firstUrl = first.src;
    fetchMock.mockClear();

    await advance(5000);
    expect(image()).toBe(first); // one bad read is a blip
    await advance(5000);
    // The dead image is gone and its request aborted before anything is re-opened.
    expect(screen.queryByAltText('Live')).toBeNull();
    expect(first.src).toMatch(/^data:image\/gif;/);
    expect(screen.getByRole('status')).toHaveTextContent('Reconnecting');

    await advance(2000);
    expect(image()).not.toBe(first);
    expect(image().src).toContain('/api/v1/printers/42/camera/stream');
    expect(image().src).not.toBe(firstUrl);
    expect(status).toHaveBeenCalledWith(printerSource(42), expect.stringMatching(VIEWER_ID));
    // A re-open is this tile's own business: the others watching keep their stream.
    expect(stopCalls()).toEqual([]);
  });

  it('asks a standalone camera about itself', async () => {
    const status = vi.spyOn(api, 'getCameraSourceStatus').mockResolvedValue(ALIVE as never);
    render(tile(standaloneSource(3)));
    await advance(5000);
    expect(status).toHaveBeenCalledWith(standaloneSource(3), expect.stringMatching(VIEWER_ID));
  });

  // The wall's tile and an OBS overlay of the same printer share one upstream.
  // When the server ends the tile's viewer alone, the upstream stays healthy.
  it('re-opens its own stream when the server ended it and somebody else keeps the camera alive', async () => {
    const status = vi.spyOn(api, 'getCameraSourceStatus')
      .mockResolvedValue({ ...ALIVE, viewer_attached: false } as never);
    render(tile());
    await advance(0);
    const first = image();
    const firstViewer = viewerOf(first);
    expect(firstViewer).toMatch(VIEWER_ID);
    // The id is part of the address before the token is added to it.
    expect(first.src.indexOf('viewer=')).toBeGreaterThan(0);
    expect(first.src.includes('token=') ? first.src.indexOf('viewer=') < first.src.indexOf('token=') : true).toBe(true);

    await advance(5000);
    expect(status).toHaveBeenLastCalledWith(printerSource(42), firstViewer);
    expect(image()).toBe(first); // one bad read is a blip
    await advance(5000);
    expect(screen.queryByAltText('Live')).toBeNull();
    expect(first.src).toMatch(/^data:image\/gif;/);

    status.mockResolvedValue({ ...ALIVE, viewer_attached: true } as never);
    await advance(2000);
    const second = image();
    // A re-opened stream is another viewer, and is asked about by its own id.
    expect(viewerOf(second)).toMatch(VIEWER_ID);
    expect(viewerOf(second)).not.toBe(firstViewer);
    await advance(5000);
    expect(status).toHaveBeenLastCalledWith(printerSource(42), viewerOf(second));
    await advance(60_000);
    expect(image()).toBe(second);
  });

  it('keeps a stream on a server that does not say whether this viewer is attached', async () => {
    vi.spyOn(api, 'getCameraSourceStatus').mockResolvedValue(ALIVE as never);
    render(tile());
    await advance(0);
    const first = image();

    await advance(120_000);

    expect(image()).toBe(first);
  });

  it('leaves a healthy stream alone', async () => {
    vi.spyOn(api, 'getCameraSourceStatus').mockResolvedValue(ALIVE as never);
    render(tile());
    await advance(0);
    const first = image();
    const url = first.src;

    await advance(120_000);

    expect(image()).toBe(first);
    expect(first.src).toBe(url);
  });

  it('retries an image error with a growing wait, then says No signal and offers Retry', async () => {
    vi.spyOn(api, 'getCameraSourceStatus').mockResolvedValue(ALIVE as never);
    render(tile());
    await advance(0);

    for (const wait of [2000, 4000, 8000, 16_000, 30_000]) {
      fireEvent.error(image());
      expect(screen.queryByAltText('Live')).toBeNull();
      expect(screen.queryByText('No signal')).toBeNull();
      await advance(wait - 1);
      expect(screen.queryByAltText('Live')).toBeNull();
      await advance(1);
      expect(image().src).toContain('/camera/stream');
    }
    fireEvent.error(image());
    expect(screen.getByText('No signal')).toBeInTheDocument();
    await advance(300_000);
    expect(screen.queryByAltText('Live')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(image().src).toContain('/camera/stream');
    expect(screen.queryByText('No signal')).toBeNull();
  });

  it('does not reconnect in a loop while the page is hidden', async () => {
    const status = vi.spyOn(api, 'getCameraSourceStatus').mockResolvedValue(ENDED as never);
    render(tile());
    await advance(0);
    const first = image();
    hidden = true;
    act(() => { document.dispatchEvent(new Event('visibilitychange')); });

    await advance(600_000);
    expect(status).not.toHaveBeenCalled();
    expect(image()).toBe(first);

    fireEvent.error(first);
    await advance(600_000);
    expect(screen.queryByAltText('Live')).toBeNull();

    hidden = false;
    act(() => { document.dispatchEvent(new Event('visibilitychange')); });
    expect(image().src).toContain('/camera/stream');
  });

  it('a kiosk tile, which may not ask the status, still retries an image error', async () => {
    const status = vi.spyOn(api, 'getCameraSourceStatus').mockResolvedValue(ENDED as never);
    render(tile(printerSource(42), { streamToken: 'kiosk-token' }));
    await advance(0);

    await advance(30_000);
    expect(status).not.toHaveBeenCalled();
    fireEvent.error(image());
    await advance(2000);
    expect(image().src).toContain('token=kiosk-token');
  });
});
