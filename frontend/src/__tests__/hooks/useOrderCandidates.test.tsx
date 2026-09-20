/**
 * The one declaration of the `['order-candidates', file, plate]` query.
 *
 * Two properties matter and neither is visible from a dialog: the plate is part
 * of the KEY (plate 2 of a file answers a different question from plate 1, and
 * a shared key would show plate 1's orders beside plate 2's picker), and the
 * query stays disabled until the dialog that needs it is actually open — the
 * modal mounts for every silent member of a grouped run, and one request per
 * member for a list nobody looks at is the cost this `enabled` exists to avoid.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { api, type OrderCandidate } from '../../api/client';
import { useOrderCandidates } from '../../hooks/useOrderCandidates';

function wrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

/** One candidate, in the shape the endpoint sends it. */
const CANDIDATE: OrderCandidate = {
  project_id: 4,
  project_name: 'Kickstarter batch',
  project_line_id: 9,
  product_id: 2,
  product_name: 'Desk Lamp',
  outstanding_prints: 5,
  priority: 2,
  deadline: null,
  created_at: '2026-09-01T10:14:02',
  line_material: null,
};

describe('useOrderCandidates', () => {
  beforeEach(() => vi.restoreAllMocks());

  it('asks for the plate it was given', async () => {
    const get = vi.spyOn(api, 'getOrderCandidates').mockResolvedValue([]);

    const { result } = renderHook(() => useOrderCandidates(5, 2, true), { wrapper: wrapper() });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(get).toHaveBeenCalledWith(5, 2);
  });

  it('asks nothing while the dialog that needs it is not open', async () => {
    const get = vi.spyOn(api, 'getOrderCandidates').mockResolvedValue([]);

    const { result } = renderHook(() => useOrderCandidates(5, 0, false), { wrapper: wrapper() });

    expect(result.current.fetchStatus).toBe('idle');
    expect(get).not.toHaveBeenCalled();
  });

  it('asks nothing about a file it has not been given', async () => {
    const get = vi.spyOn(api, 'getOrderCandidates').mockResolvedValue([]);

    renderHook(() => useOrderCandidates(undefined, 0, true), { wrapper: wrapper() });

    expect(get).not.toHaveBeenCalled();
  });

  it('keeps one plate answer from being served for another', async () => {
    const get = vi
      .spyOn(api, 'getOrderCandidates')
      .mockImplementation(async (_file: number, plate: number) =>
        plate === 1 ? [] : [
          {
            project_id: 4,
            project_name: 'Kickstarter batch',
            project_line_id: 9,
            product_id: 2,
            product_name: 'Desk Lamp',
            outstanding_prints: 5,
            priority: 2,
            deadline: null,
            created_at: '2026-09-01T10:14:02',
            line_material: null,
          },
        ],
      );
    const wrap = wrapper();

    const first = renderHook(() => useOrderCandidates(5, 1, true), { wrapper: wrap });
    await waitFor(() => expect(first.result.current.data).toEqual([]));

    const second = renderHook(() => useOrderCandidates(5, 2, true), { wrapper: wrap });
    await waitFor(() => expect(second.result.current.data).toHaveLength(1));

    expect(get).toHaveBeenCalledTimes(2);
  });

  it('never retries, whatever the client around it is configured to do', async () => {
    // ⚠️ Pinned against the CLIENT's own default, not against nothing: a file
    // whose candidates cannot be fetched is a file printed without an order,
    // which is what the dialog did before this field existed. Three silent
    // backoffs first is a submit button disabled for seconds — the dialog waits
    // on this query — for an answer that is not coming.
    const client = new QueryClient({ defaultOptions: { queries: { retry: 3, gcTime: 0 } } });
    const wrap = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    vi.spyOn(api, 'getOrderCandidates').mockResolvedValue([]);

    const { result } = renderHook(() => useOrderCandidates(5, 0, true), { wrapper: wrap });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(client.getQueryCache().find({ queryKey: ['order-candidates', 5, 0] })?.options.retry).toBe(false);
  });

  it('keeps the previous plate on screen while the next plate is fetched', async () => {
    // ⚠️ Without this the field EMPTIES for the length of a round trip, and its
    // own rule ("no candidates → render nothing") makes the whole thing vanish
    // and come back — dropping the picker to «Without an order» in between,
    // where a fast operator can submit it.
    let release: (() => void) | null = null;
    vi.spyOn(api, 'getOrderCandidates').mockImplementation(async (_file: number, plate: number) => {
      if (plate === 1) return [CANDIDATE];
      await new Promise<void>((resolve) => {
        release = resolve;
      });
      return [];
    });
    const wrap = wrapper();

    const { result, rerender } = renderHook(
      ({ plate }: { plate: number }) => useOrderCandidates(5, plate, true),
      { wrapper: wrap, initialProps: { plate: 1 } },
    );
    await waitFor(() => expect(result.current.data).toEqual([CANDIDATE]));

    rerender({ plate: 2 });
    expect(result.current.data).toEqual([CANDIDATE]);
    expect(result.current.isPlaceholderData).toBe(true);

    release!();
    await waitFor(() => expect(result.current.data).toEqual([]));
  });

  it('never shows the candidates of one file under another file', async () => {
    // The guard `OrderPrints` applies to its own placeholder, for the same
    // reason: another file's orders are not a stale view of this file's, they
    // are an answer to a different question — and this one names the LINE a
    // print would be filed under.
    let release: (() => void) | null = null;
    vi.spyOn(api, 'getOrderCandidates').mockImplementation(async (file: number) => {
      if (file === 5) return [CANDIDATE];
      await new Promise<void>((resolve) => {
        release = resolve;
      });
      return [];
    });
    const wrap = wrapper();

    const { result, rerender } = renderHook(
      ({ file }: { file: number }) => useOrderCandidates(file, 0, true),
      { wrapper: wrap, initialProps: { file: 5 } },
    );
    await waitFor(() => expect(result.current.data).toEqual([CANDIDATE]));

    rerender({ file: 6 });
    expect(result.current.data).toBeUndefined();

    release!();
    await waitFor(() => expect(result.current.data).toEqual([]));
  });
});
