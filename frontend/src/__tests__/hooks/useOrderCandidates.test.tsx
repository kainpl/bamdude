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
import { api } from '../../api/client';
import { useOrderCandidates } from '../../hooks/useOrderCandidates';

function wrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

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
});
