/**
 * `render` from `__tests__/utils` wraps in a BrowserRouter with no route
 * option — route-aware tests set the URL with pushState first, the way
 * `OrdersPage.test.tsx` does.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { render } from '../../utils';
import { api } from '../../../api/client';
import { ProductsPage } from '../../../pages/products/ProductsPage';

const rows = [
  { id: 1, name: 'Flask', is_active: true, cover_image_filename: null, has_cover: true, parts_count: 2, plates_count: 1, lines_count: 3 },
  { id: 2, name: 'Old lid', is_active: false, cover_image_filename: null, has_cover: false, parts_count: 1, plates_count: 1, lines_count: 0 },
];

afterEach(() => {
  window.history.pushState({}, '', '/');
});

describe('ProductsPage', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    window.history.pushState({}, '', '/products');
  });

  it('asks for catalog products by default and for everything when the toggle is off', async () => {
    const get = vi.spyOn(api, 'getProducts').mockResolvedValue(rows as never);
    render(<ProductsPage />);
    await screen.findByText('Flask');
    expect(get).toHaveBeenLastCalledWith({ active: true });
    fireEvent.click(screen.getByLabelText(/in catalog/i));
    await waitFor(() => expect(get).toHaveBeenLastCalledWith({}));
  });

  it('searches with the typed text', async () => {
    const get = vi.spyOn(api, 'getProducts').mockResolvedValue(rows as never);
    render(<ProductsPage />);
    fireEvent.change(await screen.findByRole('searchbox'), { target: { value: 'lid' } });
    await waitFor(() => expect(get).toHaveBeenLastCalledWith({ active: true, q: 'lid' }));
  });

  it('shows the card figures and says nothing about a product no order uses', async () => {
    vi.spyOn(api, 'getProducts').mockResolvedValue(rows as never);
    render(<ProductsPage />);
    await screen.findByText('Flask');
    // `has_cover` decides per card: the effective cover for one, the neutral tile for the other.
    expect(screen.getAllByTestId('product-cover')).toHaveLength(1);
    expect(screen.getAllByTestId('product-cover-placeholder')).toHaveLength(1);
    expect(screen.getByText(/in 3 orders/i)).toBeInTheDocument();
    // `lines_count: 0` must not leak a bare "in 0 orders" row.
    expect(screen.queryByText(/in 0 orders/i)).not.toBeInTheDocument();
    expect(screen.getByText(/not in catalog/i)).toBeInTheDocument();
  });

  it('a 409 on delete becomes a toast, not a crash', async () => {
    vi.spyOn(api, 'getProducts').mockResolvedValue(rows as never);
    vi.spyOn(api, 'deleteProduct').mockRejectedValue(new Error('Product is used by an order line'));
    render(<ProductsPage />);
    fireEvent.click((await screen.findAllByTestId('product-menu'))[0]);
    fireEvent.click(await screen.findByRole('menuitem', { name: /delete/i }));
    fireEvent.click(await screen.findByRole('button', { name: /^delete$/i })); // ConfirmModal
    expect(await screen.findByText(/used by an order line/i)).toBeInTheDocument();
    // The grid survives the failure.
    expect(screen.getByText('Flask')).toBeInTheDocument();
  });
});
