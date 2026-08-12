/**
 * One slice, one "Sliced X" toast.
 *
 * `setInterval` does not await an async callback, so a tick fires whether or
 * not the previous one came back. Slicing a large project blocks the backend
 * for seconds at a time, and every tick that piled up during the stall had
 * already captured a snapshot naming the job as active. They resolved together,
 * each saw `completed`, and each ran the completion path — one toast and two
 * query invalidations apiece. A 20 s stall against the 1.5 s interval is a
 * dozen of them from a single slice.
 *
 * The test reproduces that shape rather than describing it: the poll promise is
 * held open while the timers advance, then released all at once.
 */
import { describe, expect, it, vi } from 'vitest';
import { act, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import { SliceJobTrackerProvider, useSliceJobTracker } from '../../contexts/SliceJobTrackerContext';
import { ToastProvider } from '../../contexts/ToastContext';
import { api } from '../../api/client';

function Harness({ onReady }: { onReady: (track: (id: number) => void) => void }) {
  const { trackJob } = useSliceJobTracker();
  onReady((id) => trackJob(id, 'libraryFile', 'Cube.3mf'));
  return null;
}

function renderTracker() {
  let track: ((id: number) => void) | null = null;
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <SliceJobTrackerProvider>
          <Harness onReady={(fn) => (track = fn)} />
        </SliceJobTrackerProvider>
      </ToastProvider>
    </QueryClientProvider>,
  );
  return (id: number) => track!(id);
}

describe('SliceJobTracker — a finished slice is reported once', () => {
  it('survives a stalled backend without repeating the completion', async () => {
    vi.useFakeTimers();
    // Every poll hangs until released — the saturated-backend shape.
    const release: ((v: unknown) => void)[] = [];
    const getSliceJob = vi
      .spyOn(api, 'getSliceJob')
      .mockImplementation(() => new Promise((res) => release.push(res)) as never);

    const track = renderTracker();
    act(() => track(1));

    // Thirteen intervals' worth of ticks while nothing comes back.
    await act(async () => {
      vi.advanceTimersByTime(1500 * 13);
    });

    // Only one round should ever have been in flight.
    expect(getSliceJob.mock.calls.length).toBe(1);

    // Release everything at once — the moment the pile-up used to surface.
    await act(async () => {
      release.forEach((r) => r({ id: 1, status: 'completed', progress: null }));
    });

    vi.useRealTimers();
    await waitFor(() => expect(screen.getAllByText(/Sliced/).length).toBeGreaterThan(0));
    expect(screen.getAllByText(/Sliced/)).toHaveLength(1);

    getSliceJob.mockRestore();
  });

  it('re-arms when the same id is tracked again', async () => {
    // The completion guard is a Set that would otherwise become a permanent
    // record of every job the session ever saw, and a re-run of the same id
    // would then finish silently.
    vi.useFakeTimers();
    const getSliceJob = vi
      .spyOn(api, 'getSliceJob')
      .mockResolvedValue({ id: 7, status: 'completed', progress: null } as never);

    const track = renderTracker();
    act(() => track(7));
    await act(async () => {
      vi.advanceTimersByTime(1500);
    });
    const afterFirst = getSliceJob.mock.calls.length;

    act(() => track(7));
    await act(async () => {
      vi.advanceTimersByTime(1500);
    });

    vi.useRealTimers();
    expect(getSliceJob.mock.calls.length).toBeGreaterThan(afterFirst);

    getSliceJob.mockRestore();
  });
});
