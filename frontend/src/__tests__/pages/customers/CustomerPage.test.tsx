/**
 * The `render` helper has no route/path option — the page reads `useParams`,
 * so the URL is set with pushState and the page is mounted under a matching
 * `<Route>` inside the helper's own BrowserRouter.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, fireEvent, waitFor } from '@testing-library/react';
import { Routes, Route } from 'react-router-dom';
import { render } from '../../utils';
import { api } from '../../../api/client';
import { CustomerPage } from '../../../pages/customers/CustomerPage';

const customer = {
  id: 1,
  name: 'ACME',
  contact: null,
  notes: 'VIP',
  figures: {
    projects: 2,
    active: 1,
    completed: 1,
    cancelled: 0,
    ordered: 12,
    printed: 7,
    total_cost: 30.5,
    total_price: 200,
  },
};

const orders = [
  {
    id: 5,
    name: 'Flasks',
    status: 'active',
    customer_id: 1,
    customer_name: 'ACME',
    ordered: 10,
    printed: 5,
    progress: 0.5,
    lines_count: 1,
    priority: 'normal',
    product_cover_filenames: [],
  },
];

function mountAt() {
  window.history.pushState({}, '', '/customers/1');
  render(
    <Routes>
      <Route path="/customers/:id" element={<CustomerPage />} />
    </Routes>,
  );
}

afterEach(() => {
  window.history.pushState({}, '', '/');
});

describe('CustomerPage', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("shows the full figures and the customer's orders", async () => {
    vi.spyOn(api, 'getCustomer').mockResolvedValue(customer as never);
    const getOrders = vi.spyOn(api, 'getOrders').mockResolvedValue(orders as never);

    mountAt();

    expect(await screen.findByText('Flasks')).toBeInTheDocument();
    expect(getOrders).toHaveBeenCalledWith({ customer_id: 1 });
    // printed / ordered from the detail figures, rendered by ProgressBar — never recomputed here
    expect(screen.getByText('7 / 12')).toBeInTheDocument();
    expect(screen.getByText('VIP')).toBeInTheDocument();
  });

  it('names the error when there is no customer to fall back on', async () => {
    vi.spyOn(api, 'getCustomer').mockRejectedValue(new Error('Gateway timeout'));
    vi.spyOn(api, 'getOrders').mockResolvedValue([] as never);

    mountAt();

    expect(await screen.findByText(/could not load this customer/i)).toBeInTheDocument();
    expect(screen.getByText(/gateway timeout/i)).toBeInTheDocument();
    expect(screen.queryByText(/customer not found/i)).not.toBeInTheDocument();
  });

  it('keeps the rendered customer when a background refetch fails', async () => {
    // TanStack v5 turns the query's status to "error" on ANY failed fetch and
    // keeps `data` while it does. Every order action on this page invalidates
    // ['customer', id], so a refetch that fails must not replace a customer
    // that is still cached with a load error.
    const get = vi
      .spyOn(api, 'getCustomer')
      .mockResolvedValueOnce(customer as never)
      .mockRejectedValue(new Error('Gateway timeout'));
    vi.spyOn(api, 'getOrders').mockResolvedValue(orders as never);
    vi.spyOn(api, 'updateOrder').mockResolvedValue({ ...orders[0], status: 'completed' } as never);

    mountAt();

    expect(await screen.findByRole('heading', { name: 'ACME' })).toBeInTheDocument();

    // Marking the order completed invalidates ['customer', id]; that refetch fails.
    fireEvent.click(screen.getByRole('button', { name: /actions/i }));
    fireEvent.click(await screen.findByText(/mark completed/i));
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));

    expect(screen.getByRole('heading', { name: 'ACME' })).toBeInTheDocument();
    expect(screen.queryByText(/could not load this customer/i)).not.toBeInTheDocument();
  });
});
