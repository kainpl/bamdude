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
import { render } from '../../utils';
import { api } from '../../../api/client';
import { ProductPage } from '../../../pages/products/ProductPage';

const product = {
  id: 1,
  name: 'Flask',
  is_active: true,
  cover_image_filename: null,
  parts_count: 0,
  plates_count: 0,
  lines_count: 0,
  description: null,
  notes: null,
  designer: 'Ada',
  license: 'CC-BY',
  source_url: null,
  design_id: null,
  attachments: null,
  parts: [],
  library_file_ids: [],
  library_folder_ids: [],
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
    vi.spyOn(api, 'getProduct').mockResolvedValue(product as never);
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
});
