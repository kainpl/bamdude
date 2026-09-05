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
import type { Permission } from '../../../api/client';
import { OrderPage } from '../../../pages/orders/OrderPage';
import { createAppQueryClient } from '../../../utils/appQueryClient';

/**
 * What the mocked `useAuth` grants.
 *
 * `null` is the admin the render helper's real `AuthProvider` resolves — every
 * test below except the viewer one wants that, so the default costs nothing
 * and the narrowing is visible where it matters.
 */
const auth = vi.hoisted(() => ({ granted: null as Set<string> | null }));

// Only the hook is replaced; the provider itself stays real, so the page mounts
// the way the app mounts it.
vi.mock('../../../contexts/AuthContext', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../../contexts/AuthContext')>();
  return {
    ...actual,
    useAuth: () => {
      const real = actual.useAuth();
      return { ...real, hasPermission: (p: Permission) => auth.granted?.has(p) ?? real.hasPermission(p) };
    },
  };
});

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

/** The catalog's own query, so an invalidation of `['products']` shows up as
 *  the refetch the operator actually gets. */
function ProductsProbe({ onFetch }: { onFetch: () => void }) {
  useQuery({
    queryKey: ['products', 'probe'],
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
    auth.granted = null;
    // The page mounts `PlanBlock`, which fetches its own plan. These tests are
    // about the page's composition, not the plan — an empty one keeps the block
    // quiet without letting the request escape to the network.
    vi.spyOn(api, 'getOrderPlan').mockResolvedValue({
      lines: [],
      totals: { prints: 0, print_time_seconds: 0, filament_used_grams: 0, cost: null },
    });
  });

  it('offers a read-only viewer no way to change a line', async () => {
    // The NEGATIVE half of the lines table's permission gate, which nothing
    // pinned: `projects:read` renders the order, `projects:update` renders the
    // affordances, and the page is the only place the two are joined
    // (`canEdit = hasPermission('projects:update')`). A default-true `canEdit`,
    // a gate dropped from a row's action cell, or the page passing `canEdit`
    // it never computed all show up here.
    //
    // ⚠️ Not "no print button": pass 3 took printing off the row entirely and
    // the plan block owns it now, so what a viewer must not see is edit,
    // reorder, delete and add-line.
    auth.granted = new Set(['projects:read']);
    vi.spyOn(api, 'getOrder').mockResolvedValue(order as never);

    window.history.pushState({}, '', '/projects/1');
    render(
      <Routes>
        <Route path="/projects/:id" element={<OrderPage />} />
      </Routes>,
    );

    // The line is on screen — this is a viewer, not an error page.
    expect(await screen.findByText('Flask')).toBeInTheDocument();
    // ...and it can still be opened to see its parts.
    expect(screen.getByTestId('line-10-expand')).toBeInTheDocument();

    for (const action of ['edit', 'up', 'down', 'delete', 'save']) {
      expect(screen.queryByTestId(`line-10-${action}`)).not.toBeInTheDocument();
    }
    expect(screen.queryByRole('button', { name: /add line/i })).not.toBeInTheDocument();
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
  /**
   * «Bank the surplus» (pass 8, Decision 2). The header renders the button and
   * the PAGE owns the call, the two invalidations and the sentence — only the
   * page knows which products the order's lines are for, and the response is
   * aggregated per PART, so it cannot say.
   */
  describe('banking the surplus', () => {
    const overprinted = {
      ...order,
      lines: [
        {
          ...order.lines[0],
          parts: [
            { part_id: 1, name: 'lid', qty_per_unit: 1, need: 2, usable: 7, in_progress: 0, remaining: 0, surplus: 5 },
          ],
        },
      ],
    };

    it('posts once and says what landed on which product shelf', async () => {
      vi.spyOn(api, 'getOrder').mockResolvedValue(overprinted as never);
      const bank = vi
        .spyOn(api, 'bankOrderSurplus')
        .mockResolvedValue({ moved: [{ part_id: 1, name: 'lids', delta: 5 }], nothing_to_bank: false });

      window.history.pushState({}, '', '/projects/1');
      render(
        <Routes>
          <Route path="/projects/:id" element={<OrderPage />} />
        </Routes>,
      );

      fireEvent.click(await screen.findByTestId('order-bank-surplus'));

      await waitFor(() => expect(bank).toHaveBeenCalledWith(1));
      // Data, not keys — the counts and part names are the server's, the
      // product is the one this order's line names.
      expect(await screen.findByText('5 lids → free stock of Flask')).toBeInTheDocument();
    });

    it('treats a second press as the success it is', async () => {
      vi.spyOn(api, 'getOrder').mockResolvedValue(overprinted as never);
      vi.spyOn(api, 'bankOrderSurplus').mockResolvedValue({ moved: [], nothing_to_bank: true });

      window.history.pushState({}, '', '/projects/1');
      render(
        <Routes>
          <Route path="/projects/:id" element={<OrderPage />} />
        </Routes>,
      );

      fireEvent.click(await screen.findByTestId('order-bank-surplus'));

      // ⚠️ Neutral, never an error: the surplus was already banked, which is
      // exactly what the operator wanted to be true.
      expect(await screen.findByText(/already on the shelf/i)).toBeInTheDocument();
    });

    it('refetches the product views as well as the order', async () => {
      // The shelf the surplus landed on belongs to a PRODUCT, and every screen
      // showing `kits_available` is now wrong — the catalog cards, the product
      // page's stock section, and the kits the next line dialog offers. An
      // order-only invalidation would leave the next order for the same product
      // offering kits that are already spoken for.
      const products = vi.fn();
      vi.spyOn(api, 'getOrder').mockResolvedValue(overprinted as never);
      vi.spyOn(api, 'bankOrderSurplus').mockResolvedValue({ moved: [], nothing_to_bank: true });

      window.history.pushState({}, '', '/projects/1');
      // ⚠️ One `render` call: the helper builds a fresh QueryClient per call, so
      // a probe mounted in a second call would watch a different cache and see
      // nothing this page invalidated.
      render(
        <>
          <ProductsProbe onFetch={products} />
          <Routes>
            <Route path="/projects/:id" element={<OrderPage />} />
          </Routes>
        </>,
      );
      await waitFor(() => expect(products).toHaveBeenCalledTimes(1));

      fireEvent.click(await screen.findByTestId('order-bank-surplus'));

      await waitFor(() => expect(products).toHaveBeenCalledTimes(2));
    });
  });
});
