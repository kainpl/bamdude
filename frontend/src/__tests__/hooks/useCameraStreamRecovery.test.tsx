import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useCameraStreamRecovery, type CameraStreamHealth } from '../../hooks/useCameraStreamRecovery';

// A stream the server ends cleanly fires no `error` on its <img>: the browser
// keeps the last frame. The views learn it from the server's status for that
// source, and re-open with a bounded backoff.

const ALIVE: CameraStreamHealth = { active: true, stalled: false };
const ENDED: CameraStreamHealth = { active: false, stalled: false };
const STALLED: CameraStreamHealth = { active: true, stalled: true };

let hidden = false;
const setHidden = (value: boolean) => {
  hidden = value;
  act(() => { document.dispatchEvent(new Event('visibilitychange')); });
};
const advance = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });

beforeEach(() => {
  vi.useFakeTimers();
  hidden = false;
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });
});
afterEach(() => {
  vi.useRealTimers();
  delete (document as unknown as { hidden?: boolean }).hidden;
});

function mount(readHealth?: () => Promise<CameraStreamHealth | null>, slowRetryMs?: number) {
  return renderHook(
    ({ enabled }: { enabled: boolean }) => useCameraStreamRecovery({ enabled, readHealth, slowRetryMs }),
    { initialProps: { enabled: true } },
  );
}

describe('useCameraStreamRecovery', () => {
  it.each([['ended', ENDED], ['stalled', STALLED]])(
    're-opens a stream the server says has %s on two reads in a row',
    async (_name, health) => {
      const read = vi.fn().mockResolvedValue(health);
      const { result } = mount(read);

      await advance(5000);
      expect(result.current.phase).toBe('live');
      await advance(5000);
      // The old image is taken down first…
      expect(result.current.phase).toBe('waiting');
      expect(result.current.generation).toBe(0);
      // …and the stream is opened again after the first, short wait.
      await advance(2000);
      expect(result.current.phase).toBe('live');
      expect(result.current.generation).toBe(1);
    },
  );

  it('does not act on one bad read, an unreadable one or an unknown one', async () => {
    const read = vi.fn()
      .mockResolvedValueOnce(ENDED)
      .mockResolvedValueOnce(ALIVE)
      .mockResolvedValueOnce(ENDED)
      .mockRejectedValueOnce(new Error('503'))
      .mockResolvedValueOnce(null)
      .mockResolvedValue(ALIVE);
    const { result } = mount(read);

    await advance(40_000);

    expect(read.mock.calls.length).toBeGreaterThanOrEqual(6);
    expect(result.current.phase).toBe('live');
    expect(result.current.generation).toBe(0);
  });

  it('backs off between attempts, then gives up until it is asked again', async () => {
    const { result } = mount();
    const waits = [2000, 4000, 8000, 16_000, 30_000];
    for (const [index, wait] of waits.entries()) {
      act(() => result.current.onError());
      expect(result.current.phase).toBe('waiting');
      await advance(wait - 1);
      expect(result.current.generation).toBe(index);
      await advance(1);
      expect(result.current.phase).toBe('live');
      expect(result.current.generation).toBe(index + 1);
    }

    act(() => result.current.onError());
    expect(result.current.phase).toBe('failed');
    await advance(600_000);
    expect(result.current.phase).toBe('failed');
    expect(result.current.generation).toBe(5);

    act(() => result.current.restart());
    expect(result.current.phase).toBe('live');
    expect(result.current.generation).toBe(6);
    // A fresh budget: the first wait is the short one again.
    act(() => result.current.onError());
    await advance(2000);
    expect(result.current.generation).toBe(7);
  });

  it('starts the backoff over once a frame has arrived', async () => {
    const { result } = mount();
    act(() => result.current.onError());
    await advance(2000);
    act(() => result.current.onError());
    await advance(4000);
    expect(result.current.generation).toBe(2);

    act(() => result.current.onLoad());
    act(() => result.current.onError());
    await advance(2000);
    expect(result.current.generation).toBe(3);
  });

  it('never has two re-opens pending', async () => {
    const { result } = mount();
    act(() => result.current.onError());
    act(() => result.current.onError());
    // A late report of the same loss neither adds a re-open nor pushes the pending one back.
    await advance(1500);
    act(() => result.current.onError());
    await advance(500);
    expect(result.current.generation).toBe(1);
    await advance(60_000);
    expect(result.current.generation).toBe(1);
  });

  it('keeps trying slowly after giving up, where nobody can press Retry', async () => {
    const { result } = mount(undefined, 60_000);
    for (const wait of [2000, 4000, 8000, 16_000, 30_000]) {
      act(() => result.current.onError());
      await advance(wait);
    }
    act(() => result.current.onError());
    expect(result.current.phase).toBe('failed');

    await advance(60_000);
    expect(result.current.phase).toBe('live');
    expect(result.current.generation).toBe(6);
    // Still failing: back to "No signal", and one more attempt a minute later.
    act(() => result.current.onError());
    expect(result.current.phase).toBe('failed');
    await advance(59_999);
    expect(result.current.generation).toBe(6);
    await advance(1);
    expect(result.current.generation).toBe(7);
  });

  it('neither asks nor re-opens while the page is hidden, and re-opens once when it is shown', async () => {
    const read = vi.fn().mockResolvedValue(ENDED);
    const { result } = mount(read);
    setHidden(true);

    await advance(60_000);
    expect(read).not.toHaveBeenCalled();
    expect(result.current.phase).toBe('live');

    // An image error while hidden takes the stream down and waits for the page.
    act(() => result.current.onError());
    await advance(600_000);
    expect(result.current.phase).toBe('waiting');
    expect(result.current.generation).toBe(0);

    setHidden(false);
    expect(result.current.phase).toBe('live');
    expect(result.current.generation).toBe(1);
    await advance(1000);
    expect(result.current.generation).toBe(1);
  });

  it('forgets everything when the view stops showing a stream', async () => {
    const { result, rerender } = mount();
    act(() => result.current.onError());
    expect(result.current.phase).toBe('waiting');

    rerender({ enabled: false });
    expect(result.current.phase).toBe('live');
    await advance(60_000);
    expect(result.current.generation).toBe(0);
  });
});
