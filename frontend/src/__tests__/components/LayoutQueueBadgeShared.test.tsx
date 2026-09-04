/**
 * One pending-queue question for the whole app, badge included.
 *
 * The sidebar badge is mounted on every screen and used to ask
 * `['queue', 'pending']` on a 5 s timer of its own — the same question the
 * shared `['queue', 'all', 'pending']` answers for the Queue page and the
 * order page. TanStack cannot tell two keys are one question, so an order page
 * held two cache entries and two polls for one payload, and the badge and the
 * panel could disagree for as long as their intervals differed.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { api } from '../../api/client';
import { Layout } from '../../components/Layout';
import { OrderQueue } from '../../components/projects/OrderQueue';

describe('the pending queue is asked once for the whole app', () => {
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
      http.get('/api/v1/auto-queue/', () => HttpResponse.json([])),
    );
  });

  it('keeps ONE pending entry, on the shared key and the shared interval', async () => {
    const getQueue = vi.spyOn(api, 'getQueue').mockResolvedValue([] as never);
    vi.spyOn(api, 'getOrder').mockResolvedValue({ id: 1, name: 'O', status: 'active', lines: [] } as never);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });

    render(
      <QueryClientProvider client={client}>
        <Layout />
        <OrderQueue orderId={1} />
      </QueryClientProvider>,
    );

    await waitFor(() => expect(getQueue).toHaveBeenCalledWith(undefined, 'pending'));

    const pending = client
      .getQueryCache()
      .findAll({ queryKey: ['queue'] })
      .filter((q) => q.queryKey[q.queryKey.length - 1] === 'pending');
    expect(pending.map((q) => q.queryKey)).toEqual([['queue', 'all', 'pending']]);
    // One entry watched by BOTH — the badge and the panel — and therefore one
    // fetch, not one each.
    expect(pending[0].observers.length).toBe(2);
    expect(getQueue.mock.calls.filter(([, status]) => status === 'pending')).toHaveLength(1);
    // The cadence is the hook's, and it is the badge's: every screen notices
    // queued work within ten seconds, not within the Queue page's thirty.
    expect(pending[0].observers[0].options.refetchInterval).toBe(10_000);
  });
});
