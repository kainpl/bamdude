/** The sidebar reads compact counts; the order panel reads only its own order's queue (spec workshop-order-queue). */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { api } from '../../api/client';
import { farmPollJitterMs } from '../../api/farmReadBudget';
import { Layout } from '../../components/Layout';
import { OrderQueue } from '../../components/projects/OrderQueue';

describe('sidebar queue summary', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json([])),
      http.get('/api/v1/version', () => HttpResponse.json({ version: '0.0.0', build: 'test' })),
      http.get('/api/v1/settings/', () => HttpResponse.json({ check_updates: false })),
      http.get('/api/v1/external-links/', () => HttpResponse.json([])),
      http.get('/api/v1/smart-plugs/', () => HttpResponse.json([])),
      http.get('/api/v1/support/debug-logging', () => HttpResponse.json({ enabled: false })),
      http.get('/api/v1/auth/status', () => HttpResponse.json({ auth_enabled: false, requires_setup: false })),
      http.get('/api/v1/printers/developer-mode-warnings', () => HttpResponse.json([])),
      http.get('/api/v1/queue/summary', () => HttpResponse.json({ pending_count: 0, groups: [] })),
      http.get('/api/v1/auto-queue/summary', () => HttpResponse.json({ pending_count: 0 })),
      http.get('/api/v1/inbox/unread-count', () => HttpResponse.json({ unread_count: 0 })),
    );
  });

  it('mounts no full farm-wide pending reader — the badge reads counts, the order panel its own order', async () => {
    // spec workshop-order-queue: the order panel used to be the one owner of
    // the farm-wide pending rows; it now reads only its order's queue.
    const getQueue = vi.spyOn(api, 'getQueue').mockResolvedValue([] as never);
    const getOrderQueue = vi
      .spyOn(api, 'getOrderQueue')
      .mockResolvedValue({ printing: [], pending: [], awaiting: [] } as never);
    vi.spyOn(api, 'getOrder').mockResolvedValue({ id: 1, name: 'O', status: 'active', lines: [] } as never);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });

    render(
      <QueryClientProvider client={client}>
        <Layout />
        <OrderQueue orderId={1} />
      </QueryClientProvider>,
    );

    await waitFor(() => expect(getOrderQueue).toHaveBeenCalledWith(1));
    await waitFor(() => expect(client.getQueryData(['queue', 'summary'])).toEqual({ pending_count: 0, groups: [] }));

    const pending = client
      .getQueryCache()
      .findAll({ queryKey: ['queue'] })
      .filter((q) => q.queryKey[q.queryKey.length - 1] === 'pending');
    expect(pending).toHaveLength(0);
    expect(getQueue.mock.calls.filter(([, status]) => status === 'pending')).toHaveLength(0);
    // The order panel keeps the queue lists' fallback cadence for its own query.
    const own = client.getQueryCache().find({ queryKey: ['project-queue', 1] })!;
    const interval = own.observers[0].options.refetchInterval;
    expect(typeof interval === 'function' ? interval(own) : interval).toBe(10_000 + farmPollJitterMs);
  });

  it('does not present a partial badge count as a complete zero after one summary fails', async () => {
    server.use(
      http.get('/api/v1/queue/summary', () => HttpResponse.json({ detail: 'unavailable' }, { status: 503 })),
      http.get('/api/v1/auto-queue/summary', () => HttpResponse.json({ pending_count: 3 })),
    );
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
    render(<QueryClientProvider client={client}><Layout /></QueryClientProvider>);
    await waitFor(() => expect(client.getQueryState(['queue', 'summary'])?.status).toBe('error'), { timeout: 3000 });
    expect(document.querySelector('[title="Queue count unavailable. Open the queue for details."]')).toHaveTextContent('!');
  });

  it('keeps the navigation name in a collapsed status-error tooltip', async () => {
    localStorage.setItem('sidebarExpanded', 'false');
    server.use(
      http.get('/api/v1/queues/status-summary', () => HttpResponse.json({}, { status: 503 })),
      http.get('/api/v1/maintenance/summary', () => HttpResponse.json({}, { status: 503 })),
    );
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
    render(<QueryClientProvider client={client}><Layout /></QueryClientProvider>);
    await waitFor(() => expect(document.querySelector('a[href="/queue"]')).toHaveAttribute('title', 'Queue · Attention status unavailable'), { timeout: 3000 });
    await waitFor(() => expect(document.querySelector('a[href="/maintenance"]')).toHaveAttribute('title', 'Maintenance · Attention status unavailable'), { timeout: 3000 });
    localStorage.removeItem('sidebarExpanded');
  });
});
