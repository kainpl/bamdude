/**
 * The overlay's logo survives a blip (review M2).
 *
 * An overlay stays open for a whole broadcast. One failed request for the logo
 * — the server restarting, a network hiccup — used to mark that revision failed
 * for good, so the logo was gone until somebody replaced the file. A transient
 * failure is asked again after a pause that grows; a refusal (401 / 403 / 404)
 * and a picture that will not decode stay dropped.
 */

import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import { api, ApiError } from '../../api/client';
import { OVERLAY_LOGO_RETRY_MS, useOverlayLogo } from '../../hooks/useOverlayLogo';

const png = () => new Blob(['png'], { type: 'image/png' });
const settle = () => act(async () => { await vi.advanceTimersByTimeAsync(0); });
const wait = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });

describe('useOverlayLogo', () => {
  let withToken: MockInstance<typeof api.getOverlayLogoWithToken>;

  beforeEach(() => {
    vi.useFakeTimers();
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:overlay-logo');
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {});
    withToken = vi.spyOn(api, 'getOverlayLogoWithToken');
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it.each([
    ['a server error', () => new ApiError('HTTP 503', 503)],
    ['a network failure', () => new TypeError('Failed to fetch')],
  ])('asks again after %s, and draws the logo when it answers', async (_name, failure) => {
    withToken.mockRejectedValueOnce(failure()).mockResolvedValue(png());
    const { result } = renderHook(() => useOverlayLogo('rev1', 'tok'));
    await settle();
    expect(result.current).toBeNull();
    expect(withToken).toHaveBeenCalledTimes(1);

    await wait(OVERLAY_LOGO_RETRY_MS[0] - 1000);
    expect(withToken).toHaveBeenCalledTimes(1);
    await wait(1000);
    await settle();

    expect(withToken).toHaveBeenCalledTimes(2);
    expect(result.current?.src).toBe('blob:overlay-logo');
  });

  it('waits longer after each failure in a row, up to the last pause', async () => {
    withToken.mockRejectedValue(new ApiError('HTTP 502', 502));
    renderHook(() => useOverlayLogo('rev1', 'tok'));
    await settle();

    let calls = 1;
    for (const pause of [...OVERLAY_LOGO_RETRY_MS, OVERLAY_LOGO_RETRY_MS[OVERLAY_LOGO_RETRY_MS.length - 1]]) {
      await wait(pause - 1000);
      expect(withToken).toHaveBeenCalledTimes(calls);
      await wait(1000);
      calls += 1;
      expect(withToken).toHaveBeenCalledTimes(calls);
    }
    expect(OVERLAY_LOGO_RETRY_MS[1]).toBeGreaterThan(OVERLAY_LOGO_RETRY_MS[0]);
  });

  it.each([401, 403, 404])('does not ask again after a %s', async (status) => {
    withToken.mockRejectedValue(new ApiError(`HTTP ${status}`, status));
    const { result } = renderHook(() => useOverlayLogo('rev1', 'tok'));
    await settle();

    await wait(OVERLAY_LOGO_RETRY_MS.reduce((sum, pause) => sum + pause, 0) * 2);

    expect(withToken).toHaveBeenCalledTimes(1);
    expect(result.current).toBeNull();
  });

  it('does not ask again for a picture that would not decode', async () => {
    withToken.mockResolvedValue(png());
    const { result } = renderHook(() => useOverlayLogo('rev1', 'tok'));
    await settle();
    expect(result.current).not.toBeNull();

    act(() => result.current!.onError());
    await wait(OVERLAY_LOGO_RETRY_MS[0] * 3);

    expect(result.current).toBeNull();
    expect(withToken).toHaveBeenCalledTimes(1);
  });

  it('a new revision starts clean, and a pending retry of the old one is dropped', async () => {
    withToken.mockRejectedValueOnce(new ApiError('HTTP 503', 503)).mockResolvedValue(png());
    const { result, rerender } = renderHook(({ revision }) => useOverlayLogo(revision, 'tok'), {
      initialProps: { revision: 'rev1' },
    });
    await settle();

    rerender({ revision: 'rev2' });
    await settle();
    expect(result.current?.src).toBe('blob:overlay-logo');
    expect(withToken.mock.calls.map((call) => call[0])).toEqual(['rev1', 'rev2']);

    await wait(OVERLAY_LOGO_RETRY_MS[0] * 2);
    expect(withToken).toHaveBeenCalledTimes(2);
  });

  it('stops asking once it is unmounted', async () => {
    withToken.mockRejectedValue(new ApiError('HTTP 503', 503));
    const { unmount } = renderHook(() => useOverlayLogo('rev1', 'tok'));
    await settle();

    unmount();
    await wait(OVERLAY_LOGO_RETRY_MS[0] * 2);

    expect(withToken).toHaveBeenCalledTimes(1);
  });
});
