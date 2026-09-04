/**
 * The `render` helper has no route/path option — the page reads `useParams`,
 * so the URL is set with pushState and the page is mounted under a matching
 * `<Route>` inside the helper's own BrowserRouter.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, fireEvent, waitFor, within } from '@testing-library/react';
import { Routes, Route } from 'react-router-dom';
import { QueryClientProvider, useQuery } from '@tanstack/react-query';
import { render } from '../../utils';
import { api } from '../../../api/client';
import { OrderPage } from '../../../pages/orders/OrderPage';
import { createAppQueryClient } from '../../../utils/appQueryClient';

const order = {
  id: 1,
  name: 'Ten flasks',
  customer_id: 2,
  customer_name: 'ACME',
  description: null,
  color: '#00ae42',
  status: 'active',
  notes: null,
  attachments: null,
  tags: null,
  due_date: null,
  priority: 'normal',
  price: 120,
  url: null,
  cover_image_filename: null,
  created_at: '2026-09-01T00:00:00Z',
  updated_at: '2026-09-01T00:00:00Z',
  procurement: [],
  other_archive_ids: [],
  lines: [
    {
      id: 10,
      product_id: 1,
      product_name: 'Flask',
      quantity: 2,
      material: 'PETG',
      color: null,
      note: null,
      sort_order: 0,
      units_printed: 2,
      progress: 1,
      archive_ids: [],
      parts: [],
    },
  ],
  figures: {
    ordered: 2,
    printed: 2,
    complete: 2,
    remaining: 0,
    total_time_seconds: 3600,
    total_filament_grams: 40,
    total_cost: 8,
    defective: 0,
    margin: 112,
    progress: 1,
    other_prints_count: 0,
    all_printed: true,
  },
};

afterEach(() => {
  window.history.pushState({}, '', '/');
});

/** A stand-in for CustomerPage's own query, mounted on the same client so an
 *  invalidation of the `['customer', …]` PREFIX shows up as a refetch. */
function CustomerProbe({ onFetch }: { onFetch: () => void }) {
  useQuery({
    queryKey: ['customer', 2],
    queryFn: async () => {
      onFetch();
      return null;
    },
  });
  return null;
}

describe('OrderPage', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    // The page mounts `PlanBlock`, which fetches its own plan. These tests are
    // about the page's composition, not the plan — an empty one keeps the block
    // quiet without letting the request escape to the network.
    vi.spyOn(api, 'getOrderPlan').mockResolvedValue({
      lines: [],
      totals: { prints: 0, print_time_seconds: 0, filament_used_grams: 0, cost: null },
    });
  });

  it('plans what to print next, right under the lines', async () => {
    vi.spyOn(api, 'getOrder').mockResolvedValue(order as never);

    window.history.pushState({}, '', '/projects/1');
    render(
      <Routes>
        <Route path="/projects/:id" element={<OrderPage />} />
      </Routes>,
    );

    expect(await screen.findByTestId('plan-block')).toBeInTheDocument();
    await waitFor(() => expect(api.getOrderPlan).toHaveBeenCalledWith(1));
    // The interim picker is gone with its button — the plan block is the only
    // way from this page into the queue.
    expect(screen.queryByTestId('line-10-print')).not.toBeInTheDocument();
  });

  it('suggests closing an order whose lines are all printed, and closes it on demand', async () => {
    vi.spyOn(api, 'getOrder').mockResolvedValue(order as never);
    const update = vi.spyOn(api, 'updateOrder').mockResolvedValue({ ...order, status: 'completed' } as never);

    window.history.pushState({}, '', '/projects/1');
    render(
      <Routes>
        <Route path="/projects/:id" element={<OrderPage />} />
      </Routes>,
    );

    expect(await screen.findByRole('heading', { name: 'Ten flasks' })).toBeInTheDocument();
    const banner = await screen.findByTestId('close-suggestion');
    expect(within(banner).getByText(/all lines are printed/i)).toBeInTheDocument();

    fireEvent.click(await screen.findByTestId('close-suggestion-complete'));
    await waitFor(() => expect(update).toHaveBeenCalledWith(1, { status: 'completed' }));
  });

  it('keeps the banner away while work is left', async () => {
    vi.spyOn(api, 'getOrder').mockResolvedValue({
      ...order,
      figures: { ...order.figures, printed: 1, remaining: 1, all_printed: false },
    } as never);

    window.history.pushState({}, '', '/projects/1');
    render(
      <Routes>
        <Route path="/projects/:id" element={<OrderPage />} />
      </Routes>,
    );

    expect(await screen.findByRole('heading', { name: 'Ten flasks' })).toBeInTheDocument();
    expect(screen.queryByTestId('close-suggestion')).not.toBeInTheDocument();
  });

  it('names the error when there is no order to fall back on', async () => {
    vi.spyOn(api, 'getOrder').mockRejectedValue(new Error('Gateway timeout'));

    window.history.pushState({}, '', '/projects/1');
    render(
      <Routes>
        <Route path="/projects/:id" element={<OrderPage />} />
      </Routes>,
    );

    expect(await screen.findByText(/could not load this order/i)).toBeInTheDocument();
    expect(screen.getByText(/gateway timeout/i)).toBeInTheDocument();
    expect(screen.queryByText(/order not found/i)).not.toBeInTheDocument();
  });

  it('keeps the rendered order when a background refetch fails', async () => {
    // TanStack v5 turns the query's status to "error" on ANY failed fetch and
    // keeps `data` while it does. Every mutation on this page invalidates
    // ['project', id], so a refetch that fails must not replace an order that
    // is still cached with a load error.
    const get = vi
      .spyOn(api, 'getOrder')
      .mockResolvedValueOnce(order as never)
      .mockRejectedValue(new Error('Gateway timeout'));
    vi.spyOn(api, 'updateOrder').mockResolvedValue({ ...order, status: 'completed' } as never);

    window.history.pushState({}, '', '/projects/1');
    render(
      <Routes>
        <Route path="/projects/:id" element={<OrderPage />} />
      </Routes>,
    );

    expect(await screen.findByRole('heading', { name: 'Ten flasks' })).toBeInTheDocument();

    // Closing the order invalidates ['project', id]; the refetch it fires fails.
    fireEvent.click(await screen.findByTestId('close-suggestion-complete'));
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));

    expect(screen.getByRole('heading', { name: 'Ten flasks' })).toBeInTheDocument();
    expect(screen.queryByText(/could not load this order/i)).not.toBeInTheDocument();
  });

  it('refreshes the customer keys when an order is completed', async () => {
    // The customer tiles are computed from these orders, so completing one
    // moves them. With a 60 s staleTime a key nobody invalidated is not
    // refetched on navigation for a minute — long enough to read a fresh order
    // grid under totals that still count this order as active.
    vi.spyOn(api, 'getOrder').mockResolvedValue(order as never);
    vi.spyOn(api, 'updateOrder').mockResolvedValue({ ...order, status: 'completed' } as never);
    const probeFetch = vi.fn();

    window.history.pushState({}, '', '/projects/1');
    render(
      <>
        <CustomerProbe onFetch={probeFetch} />
        <Routes>
          <Route path="/projects/:id" element={<OrderPage />} />
        </Routes>
      </>,
    );

    expect(await screen.findByRole('heading', { name: 'Ten flasks' })).toBeInTheDocument();
    await waitFor(() => expect(probeFetch).toHaveBeenCalledTimes(1));

    fireEvent.click(await screen.findByTestId('close-suggestion-complete'));
    await waitFor(() => expect(probeFetch).toHaveBeenCalledTimes(2));
  });
  it('says once that it could not refresh, and keeps the order on screen', async () => {
    // The page deliberately answers "do I have data?" before "did the fetch
    // fail?", which is right — a proxy hiccup must not throw away an order
    // somebody is reading — and completely silent: the figures are now older
    // than they look. The cache says so, once, through `meta.refreshToast`.
    const client = createAppQueryClient();
    // The retry is the app's, the delay is not: an exponential backoff would
    // put the toast a second away for no gain. Everything under test — the
    // QueryCache and its `onError` — is the app's own.
    client.setDefaultOptions({ queries: { retry: false, staleTime: 60_000 } });

    vi.spyOn(api, 'getOrder')
      .mockResolvedValueOnce(order as never)
      .mockRejectedValue(new Error('Gateway timeout'));
    vi.spyOn(api, 'updateOrder').mockResolvedValue({ ...order, status: 'completed' } as never);

    window.history.pushState({}, '', '/projects/1');
    render(
      <QueryClientProvider client={client}>
        <Routes>
          <Route path="/projects/:id" element={<OrderPage />} />
        </Routes>
      </QueryClientProvider>,
    );

    expect(await screen.findByRole('heading', { name: 'Ten flasks' })).toBeInTheDocument();

    fireEvent.click(await screen.findByTestId('close-suggestion-complete'));

    expect(await screen.findByText(/could not refresh/i)).toBeInTheDocument();
    // The stale order is still there — the toast is the whole of the damage.
    expect(screen.getByRole('heading', { name: 'Ten flasks' })).toBeInTheDocument();
    expect(screen.queryByText(/could not load this order/i)).not.toBeInTheDocument();
    expect(screen.getAllByText(/could not refresh/i)).toHaveLength(1);
  });

  it('forgets the deleted order, so a Back inside staleTime cannot render it', async () => {
    // ⚠️ The entry is dropped AFTER `navigate`: while this page is still
    // mounted, removing it re-runs the `queryFn` on an order that is gone.
    const client = createAppQueryClient();
    const get = vi.spyOn(api, 'getOrder').mockResolvedValue(order as never);
    vi.spyOn(api, 'deleteOrder').mockResolvedValue(undefined as never);

    window.history.pushState({}, '', '/projects/1');
    render(
      <QueryClientProvider client={client}>
        <Routes>
          <Route path="/projects/:id" element={<OrderPage />} />
          <Route path="/projects" element={<p>order list</p>} />
        </Routes>
      </QueryClientProvider>,
    );

    expect(await screen.findByRole('heading', { name: 'Ten flasks' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /^delete$/i }));
    fireEvent.click(await screen.findByRole('button', { name: /^confirm$/i }));

    expect(await screen.findByText('order list')).toBeInTheDocument();
    await waitFor(() => expect(client.getQueryData(['project', 1])).toBeUndefined());
    // Nothing was refetched on the way out — that would have been a 404 in the
    // query of a page that is already leaving.
    expect(get).toHaveBeenCalledTimes(1);
  });
});
