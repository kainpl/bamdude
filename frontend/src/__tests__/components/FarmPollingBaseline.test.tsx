import { StrictMode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, waitFor } from '@testing-library/react';
import { QueryClientProvider, QueryObserver } from '@tanstack/react-query';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { createAppQueryClient } from '../../utils/appQueryClient';
import { api, type PrinterQueue } from '../../api/client';
import { QueueCard } from '../../components/QueueCard';
import { FarmQueueScope } from '../../hooks/FarmQueueScope';
import { usePendingQueueItems, usePrintingQueueItems, useQueueSummary } from '../../hooks/useQueueItems';

const queue = (id: number): PrinterQueue => ({
  id,
  printer_id: id,
  printer_name: `Farm ${id}`,
  printer_model: 'P1S',
  status: 'idle',
  is_paused: false,
  auto_distribute_eligible: true,
  last_activity_at: null,
  current_item_id: null,
  pending_count: 0,
  completed_count: 0,
  failed_count: 0,
  cancelled_count: 0,
  skipped_count: 0,
  total_count: 0,
  created_at: '2026-09-25T00:00:00Z',
  updated_at: '2026-09-25T00:00:00Z',
});

describe('farm queue read ownership', () => {
  afterEach(() => vi.restoreAllMocks());

  it('keeps the existing status timer reset when live cache updates arrive', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    const client = createAppQueryClient();
    const read = vi.fn().mockResolvedValue({ state: 'RUNNING' });
    const observer = new QueryObserver(client, {
      queryKey: ['printerStatus', 1], queryFn: read, refetchInterval: 30_000,
    });
    const unsubscribe = observer.subscribe(() => {});
    try {
      await waitFor(() => expect(read).toHaveBeenCalledTimes(1));
      for (let tick = 0; tick < 4; tick += 1) {
        await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
        client.setQueryData(['printerStatus', 1], { state: 'RUNNING', progress: tick });
      }
      expect(read).toHaveBeenCalledTimes(1);
      await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
      await waitFor(() => expect(read).toHaveBeenCalledTimes(2));
    } finally {
      unsubscribe();
      client.clear();
      vi.useRealTimers();
    }
  });

  it.each([1, 50, 100])('does not start per-card item or closed-issues reads for %i mounted cards', async (count) => {
    const reads: Array<{ queueId: string | null; status: string | null }> = [];
    server.use(http.get('/api/v1/queue/', ({ request }) => {
      const params = new URL(request.url).searchParams;
      reads.push({ queueId: params.get('queue_id'), status: params.get('status') });
      return HttpResponse.json([]);
    }), http.get('/api/v1/queue/summary', () => HttpResponse.json({ pending_count: 0, groups: [] })));
    const getStatus = vi.spyOn(api, 'getPrinterStatus').mockResolvedValue({ connected: true, state: 'IDLE' } as never);
    const client = createAppQueryClient();
    function FarmView() {
      const { data: pending } = usePendingQueueItems();
      const { data: printing } = usePrintingQueueItems();
      const { data: summary } = useQueueSummary(true, false);
      return <FarmQueueScope rows={{ pending, printing, summary }}>
        {Array.from({ length: count }, (_, index) => (
          <QueueCard key={index} queue={queue(index + 1)} onEditItem={vi.fn()} />
        ))}
      </FarmQueueScope>;
    }
    const view = render(
      <QueryClientProvider client={client}>
        <FarmView />
      </QueryClientProvider>,
    );

    await waitFor(() => expect(getStatus.mock.calls.length).toBeGreaterThanOrEqual(count));
    await waitFor(() => expect(reads.length).toBe(2));
    expect(reads.filter(({ queueId }) => queueId !== null)).toHaveLength(0);

    view.unmount();
    client.clear();
  });

  it('keeps shared-only item reads under StrictMode and leaves no interval after unmount', async () => {
    const reads: Array<{ queueId: string | null; status: string | null }> = [];
    server.use(http.get('/api/v1/queue/', ({ request }) => {
      const params = new URL(request.url).searchParams;
      reads.push({ queueId: params.get('queue_id'), status: params.get('status') });
      return HttpResponse.json([]);
    }), http.get('/api/v1/queue/summary', () => HttpResponse.json({ pending_count: 0, groups: [] })));
    vi.spyOn(api, 'getPrinterStatus').mockResolvedValue({ connected: true, state: 'IDLE' } as never);
    const client = createAppQueryClient();
    function Fleet() {
      const { data: pending } = usePendingQueueItems();
      const { data: printing } = usePrintingQueueItems();
      const { data: summary } = useQueueSummary(true, false);
      return <FarmQueueScope rows={{ pending, printing, summary }}>
        {Array.from({ length: 50 }, (_, index) => <QueueCard key={index} queue={queue(index + 1)} onEditItem={vi.fn()} />)}
      </FarmQueueScope>;
    }
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    const view = render(<StrictMode><QueryClientProvider client={client}><Fleet /></QueryClientProvider></StrictMode>);
    try {
      await waitFor(() => {
        expect(client.getQueryState(['queue', 'all', 'pending'])?.status).toBe('success');
        expect(client.getQueryState(['queue', 'all', 'printing'])?.status).toBe('success');
      });
      // The development double mount neither repeats the shared reads nor
      // falls back to per-card ones.
      expect(reads).toEqual([{ queueId: null, status: 'pending' }, { queueId: null, status: 'printing' }]);
      expect(vi.getTimerCount()).toBeGreaterThan(0);
      view.unmount();
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      client.clear();
      vi.useRealTimers();
    }
  });

  it('turns off scoped readers when a farm owner takes over', async () => {
    const reads: string[] = [];
    const queueIds: Array<string | null> = [];
    server.use(
      http.get('/api/v1/queue/', ({ request }) => {
        const params = new URL(request.url).searchParams;
        reads.push(params.get('status') ?? '');
        queueIds.push(params.get('queue_id'));
        return HttpResponse.json([]);
      }),
      http.get('/api/v1/queue/summary', () => HttpResponse.json({ pending_count: 0, groups: [] })),
    );
    vi.spyOn(api, 'getPrinterStatus').mockResolvedValue({ connected: true, state: 'IDLE' } as never);
    const client = createAppQueryClient();
    const card = (farm: boolean) => <QueryClientProvider client={client}>
      <FarmQueueScope rows={farm ? { pending: [], printing: [], summary: { pending_count: 0, groups: [] } } : null}>
        <QueueCard queue={queue(1)} onEditItem={vi.fn()} />
      </FarmQueueScope>
    </QueryClientProvider>;
    const view = render(card(false));
    await waitFor(() => expect(reads).toEqual(expect.arrayContaining(['pending', 'printing'])));
    expect(queueIds.every(id => id === '1')).toBe(true); // one visible card does not request the farm-wide list
    const scopedReads = reads.length;
    view.rerender(card(true));
    await waitFor(() => expect(client.getQueryCache().find({ queryKey: ['queue', 1, 'pending'] })?.isActive()).toBe(false));
    expect(client.getQueryCache().find({ queryKey: ['queue', 1, 'printing'] })?.isActive()).toBe(false);
    expect(reads).toHaveLength(scopedReads);
    view.unmount();
    client.clear();
  });

  it('keeps 50 closed-issues cards at two shared item polls across ten quiet minutes', async () => {
    const reads: string[] = [];
    let issueReads = 0;
    server.use(
      http.get('/api/v1/queue/', ({ request }) => {
        reads.push(new URL(request.url).searchParams.get('status') ?? 'unknown');
        return HttpResponse.json([]);
      }),
      http.get('/api/v1/queue/summary', () => HttpResponse.json({ pending_count: 0, groups: [] })),
      http.get('/api/v1/queue/issues', () => { issueReads += 1; return HttpResponse.json({ items: [], next_cursor: null }); }),
    );
    vi.spyOn(api, 'getPrinterStatus').mockResolvedValue({ connected: true, state: 'IDLE' } as never);
    const client = createAppQueryClient();
    function Fleet() {
      const { data: pending } = usePendingQueueItems();
      const { data: printing } = usePrintingQueueItems();
      const { data: summary } = useQueueSummary(true, false);
      return <FarmQueueScope rows={{ pending, printing, summary }}>
        {Array.from({ length: 50 }, (_, index) => <QueueCard key={index} queue={queue(index + 1)} onEditItem={vi.fn()} />)}
      </FarmQueueScope>;
    }
    const tree = () => <QueryClientProvider client={client}><Fleet /></QueryClientProvider>;
    // Only owner intervals are virtual; MSW and waitFor retain real timers.
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    const view = render(tree());
    try {
      await waitFor(() => expect(reads).toHaveLength(2));
      for (let tick = 1; tick <= 60; tick += 1) {
        await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
        await waitFor(() => {
          expect(client.getQueryState(['queue', 'all', 'pending'])?.fetchStatus).toBe('idle');
          expect(client.getQueryState(['queue', 'all', 'printing'])?.fetchStatus).toBe('idle');
        });
      }
      const pendingReads = reads.filter(status => status === 'pending').length;
      const printingReads = reads.filter(status => status === 'printing').length;
      // Periodic work starts after the initial read and never exceeds one
      // per ten seconds/key. A stable ≤1 s tab jitter reduces the exact count.
      expect(pendingReads).toBeGreaterThanOrEqual(50);
      expect(pendingReads).toBeLessThanOrEqual(61);
      expect(printingReads).toBeGreaterThanOrEqual(50);
      expect(printingReads).toBeLessThanOrEqual(61);
      expect(issueReads).toBe(0);
    } finally {
      view.unmount();
      client.clear();
      vi.useRealTimers();
    }
  }, 30_000);
});
