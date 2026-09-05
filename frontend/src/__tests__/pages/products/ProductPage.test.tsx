/**
 * The `render` helper has no route/path option — the page reads `useParams`,
 * so the URL is set with pushState and the page is mounted under a matching
 * `<Route>` inside the helper's own BrowserRouter.
 *
 * A brand-new product has nothing in it, and that is the state an operator
 * meets first: every one of the four sections has to say so rather than render
 * an empty frame. The catalog switch is asserted on the wire, because
 * `is_active` explicitly-null is a 422 and a toggle that sent the wrong shape
 * would look identical on screen until the server refused it.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, fireEvent, waitFor } from '@testing-library/react';
import { Routes, Route } from 'react-router-dom';
import { QueryClientProvider } from '@tanstack/react-query';
import { render } from '../../utils';
import { api } from '../../../api/client';
import type { Permission } from '../../../api/client';
import { ProductPage } from '../../../pages/products/ProductPage';
import { createAppQueryClient } from '../../../utils/appQueryClient';

const auth = vi.hoisted(() => ({ granted: null as Set<string> | null }));

// Only the hook is replaced, and only when a test asks: `null` falls through to
// the admin the render helper's real `AuthProvider` resolves, so every existing
// test below is untouched.
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

const product = {
  id: 1,
  name: 'Flask',
  is_active: true,
  cover_image_filename: null,
  has_cover: false,
  parts_count: 0,
  plates_count: 0,
  lines_count: 0,
  description: null,
  notes: null,
  designer: 'Ada',
  license: 'CC-BY',
  source_url: null,
  design_id: null,
  attachments: [],
  parts: [],
  library_file_ids: [],
  library_folder_ids: [],
  units_printed_total: 12,
  created_at: '2026-09-01T00:00:00Z',
  updated_at: '2026-09-01T00:00:00Z',
};

afterEach(() => {
  window.history.pushState({}, '', '/');
});

function mountAt(id = 1) {
  window.history.pushState({}, '', `/products/${id}`);
  render(
    <Routes>
      <Route path="/products/:id" element={<ProductPage />} />
    </Routes>,
  );
}

describe('ProductPage', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    auth.granted = null;
    vi.spyOn(api, 'getProduct').mockResolvedValue(product as never);
    vi.spyOn(api, 'getProductStock').mockResolvedValue({ balances: [], kits_available: 0, movements: [] });
    vi.spyOn(api, 'getProductPlates').mockResolvedValue([] as never);
    vi.spyOn(api, 'getLibraryFiles').mockResolvedValue([] as never);
    vi.spyOn(api, 'getFoldersByProduct').mockResolvedValue([] as never);
    vi.spyOn(api, 'getOrders').mockResolvedValue([] as never);
  });

  it('renders the product and the empty state of every section', async () => {
    mountAt();

    expect(await screen.findByRole('heading', { name: 'Flask' })).toBeInTheDocument();
    expect(screen.getByText('Ada')).toBeInTheDocument();

    // Composition: nothing to list, so the add row is the whole section.
    expect(await screen.findByTestId('add-part-row')).toBeInTheDocument();
    expect(await screen.findByText(/no plates yet/i)).toBeInTheDocument();
    expect(await screen.findByText(/nothing linked yet/i)).toBeInTheDocument();
    expect(await screen.findByText(/no order asks for this product/i)).toBeInTheDocument();

    // The gallery and the typed attachments are sections of their own, and both
    // must say they are empty rather than render an empty frame.
    expect(screen.getByTestId('product-gallery')).toBeInTheDocument();
    expect(screen.getByTestId('product-cover-placeholder')).toBeInTheDocument();
    expect(screen.getByTestId('attachment-section-bom_docs')).toBeInTheDocument();
  });

  it('opens its heading outline with the product’s own h1, before any h2', async () => {
    // ⚠️ The gallery is FIRST on screen by design (parent spec: what the thing
    // looks like, then what it is) and its `<h2>` used to be first in the
    // document too — so the page's outline began at level 2 and the product's
    // name, the `<h1>`, arrived after it. A screen reader reads the outline,
    // not the layout. The fix is DOM order plus `order-first`, not a demoted
    // title and not a second, hidden `<h1>`.
    mountAt();

    const first = (await screen.findAllByRole('heading'))[0];
    expect(first.tagName).toBe('H1');
    expect(first).toHaveTextContent('Flask');
  });

  it('names the all-time figure for what it is — units delivered against orders', async () => {
    mountAt();

    expect(await screen.findByText(/printed for orders/i)).toBeInTheDocument();
    expect(screen.getByTestId('product-units-printed-total')).toHaveTextContent('12');
  });

  it('keeps the rendered page when a background refetch fails', async () => {
    // TanStack v5 turns the query's status to "error" on ANY failed fetch and
    // keeps `data` while it does. Every mutation on this page invalidates
    // ['product', id], so a refetch is in flight routinely — one that fails
    // must not replace a product that is still cached with a load error.
    const get = vi
      .spyOn(api, 'getProduct')
      .mockResolvedValueOnce(product as never)
      .mockRejectedValue(new Error('Gateway timeout'));
    vi.spyOn(api, 'updateProduct').mockResolvedValue({ ...product, is_active: false } as never);
    mountAt();

    expect(await screen.findByRole('heading', { name: 'Flask' })).toBeInTheDocument();

    // The catalog toggle invalidates ['product', id]; the refetch it triggers fails.
    fireEvent.click(screen.getByRole('checkbox', { name: /in catalog/i }));
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));

    expect(screen.getByRole('heading', { name: 'Flask' })).toBeInTheDocument();
    expect(screen.queryByText(/could not load/i)).not.toBeInTheDocument();
  });

  it('names the error when there is no product to fall back on', async () => {
    vi.spyOn(api, 'getProduct').mockRejectedValue(new Error('Gateway timeout'));
    mountAt();

    expect(await screen.findByText(/could not load this product/i)).toBeInTheDocument();
    expect(screen.getByText(/gateway timeout/i)).toBeInTheDocument();
  });

  it('takes the product out of the catalog through the switch', async () => {
    const update = vi.spyOn(api, 'updateProduct').mockResolvedValue({ ...product, is_active: false } as never);
    mountAt();

    fireEvent.click(await screen.findByRole('checkbox', { name: /in catalog/i }));
    await waitFor(() => expect(update).toHaveBeenCalledWith(1, { is_active: false }));
  });
  it('says once that it could not refresh, and keeps the product on screen', async () => {
    // The page answers "do I have data?" before "did the fetch fail?", which is
    // right — a proxy hiccup must not throw away a product somebody is reading
    // — and silent. The cache says so, once, through `meta.refreshToast`.
    const client = createAppQueryClient();
    // The retry is the app's, the delay is not: an exponential backoff would put
    // the toast a second away for no gain. The QueryCache under test is the
    // app's own.
    client.setDefaultOptions({ queries: { retry: false, staleTime: 60_000 } });

    vi.spyOn(api, 'getProduct')
      .mockResolvedValueOnce(product as never)
      .mockRejectedValue(new Error('Gateway timeout'));
    vi.spyOn(api, 'updateProduct').mockResolvedValue({ ...product, is_active: false } as never);

    window.history.pushState({}, '', '/products/1');
    render(
      <QueryClientProvider client={client}>
        <Routes>
          <Route path="/products/:id" element={<ProductPage />} />
        </Routes>
      </QueryClientProvider>,
    );

    expect(await screen.findByRole('heading', { name: 'Flask' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('checkbox', { name: /in catalog/i }));

    expect(await screen.findByText(/could not refresh/i)).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Flask' })).toBeInTheDocument();
    expect(screen.getAllByText(/could not refresh/i)).toHaveLength(1);
  });

  it('still says it could not refresh while the card dialog is open over it', async () => {
    // ⚠️ The dialog watches `['product', id]` TOO. TanStack gives a query the
    // LAST observer's options, so a dialog declaring its own `useQuery` without
    // `meta` is one mount order away from taking `refreshToast` off this page —
    // for exactly as long as somebody is editing the product. Both now read
    // through `useProductDetail`, so the options are the same set whoever wins.
    //
    // ⚠️ This test pins the BEHAVIOUR and does not by itself prove the fix:
    // measured here, React runs the child's option effect before the parent's,
    // so the page happens to re-set its own `meta` last and this passes with the
    // duplicate observer too. What forbids the duplicate is the grep gate in
    // `__tests__/hooks/detailQueryKeys.test.ts`; this one makes sure the toast
    // still works once there is only one owner.
    const client = createAppQueryClient();
    client.setDefaultOptions({ queries: { retry: false, staleTime: 60_000 } });

    vi.spyOn(api, 'getProduct')
      .mockResolvedValueOnce(product as never)
      .mockRejectedValue(new Error('Gateway timeout'));
    vi.spyOn(api, 'updateProduct').mockResolvedValue({ ...product, is_active: false } as never);

    window.history.pushState({}, '', '/products/1');
    render(
      <QueryClientProvider client={client}>
        <Routes>
          <Route path="/products/:id" element={<ProductPage />} />
        </Routes>
      </QueryClientProvider>,
    );

    expect(await screen.findByRole('heading', { name: 'Flask' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /^edit$/i }));
    expect(await screen.findByRole('dialog')).toBeInTheDocument();

    // A background refetch of the same key, with the dialog mounted over it.
    await client.invalidateQueries({ queryKey: ['product', 1] });

    expect(await screen.findByText(/could not refresh/i)).toBeInTheDocument();
    expect(screen.getAllByText(/could not refresh/i)).toHaveLength(1);
    expect(screen.getByRole('heading', { name: 'Flask' })).toBeInTheDocument();
  });

  it('forgets the deleted product, so a Back inside staleTime cannot render it', async () => {
    const client = createAppQueryClient();
    const get = vi.spyOn(api, 'getProduct').mockResolvedValue(product as never);
    vi.spyOn(api, 'deleteProduct').mockResolvedValue(undefined as never);

    window.history.pushState({}, '', '/products/1');
    render(
      <QueryClientProvider client={client}>
        <Routes>
          <Route path="/products/:id" element={<ProductPage />} />
          <Route path="/products" element={<p>product list</p>} />
        </Routes>
      </QueryClientProvider>,
    );

    expect(await screen.findByRole('heading', { name: 'Flask' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /^delete$/i }));
    // The confirm dialog's own button carries the same word (`common.delete`),
    // so the header's is the first and the dialog's is the last.
    const deletes = await screen.findAllByRole('button', { name: /^delete$/i });
    fireEvent.click(deletes[deletes.length - 1]);

    expect(await screen.findByText('product list')).toBeInTheDocument();
    await waitFor(() => expect(client.getQueryData(['product', 1])).toBeUndefined());
    // Nothing was refetched on the way out — that would have been a 404 in the
    // query of a page already leaving.
    expect(get).toHaveBeenCalledTimes(1);
  });
  describe('free stock (pass 8)', () => {
    it('shows the shelf, right under the composition it is a shelf of', async () => {
      vi.spyOn(api, 'getProductStock').mockResolvedValue({
        balances: [{ part_id: 1, name: 'lid', qty_per_unit: 1, balance: 3 }],
        kits_available: 3,
        movements: [],
      });

      mountAt();

      expect(await screen.findByTestId('product-stock')).toBeInTheDocument();
      expect(await screen.findByTestId('stock-kits')).toHaveTextContent('3 kits');
      await waitFor(() => expect(api.getProductStock).toHaveBeenCalledWith(1));
    });

    it('is not rendered at all without projects:read', async () => {
      // Reading the shelf is `PROJECTS_READ` (Decision 7) — no new permission,
      // and the section is gated rather than merely emptied so a user who may
      // not see it does not fire its request either.
      auth.granted = new Set<string>();

      mountAt();

      expect(await screen.findByRole('heading', { name: 'Flask' })).toBeInTheDocument();
      expect(screen.queryByTestId('product-stock')).not.toBeInTheDocument();
      expect(api.getProductStock).not.toHaveBeenCalled();
    });
  });
});
