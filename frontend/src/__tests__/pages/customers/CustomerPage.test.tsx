/**
 * The `render` helper has no route/path option — the page reads `useParams`,
 * so the URL is set with pushState and the page is mounted under a matching
 * `<Route>` inside the helper's own BrowserRouter.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, fireEvent, waitFor } from '@testing-library/react';
import { Routes, Route } from 'react-router-dom';
import { QueryClientProvider } from '@tanstack/react-query';
import { render } from '../../utils';
import { api } from '../../../api/client';
import { CustomerPage } from '../../../pages/customers/CustomerPage';
import { createAppQueryClient } from '../../../utils/appQueryClient';

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
    from_stock_units: 0,
    progress: 0.5,
    lines_count: 1,
    priority: 'normal',
    line_products: [],
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
  it('says once that it could not refresh, and keeps the customer on screen', async () => {
    const client = createAppQueryClient();
    // The retry is the app's, the delay is not: an exponential backoff would put
    // the toast a second away for no gain.
    client.setDefaultOptions({ queries: { retry: false, staleTime: 60_000 } });

    vi.spyOn(api, 'getCustomer')
      .mockResolvedValueOnce(customer as never)
      .mockRejectedValue(new Error('Gateway timeout'));
    vi.spyOn(api, 'getOrders').mockResolvedValue(orders as never);
    vi.spyOn(api, 'updateOrder').mockResolvedValue({ ...orders[0], status: 'completed' } as never);

    window.history.pushState({}, '', '/customers/1');
    render(
      <QueryClientProvider client={client}>
        <Routes>
          <Route path="/customers/:id" element={<CustomerPage />} />
        </Routes>
      </QueryClientProvider>,
    );

    expect(await screen.findByRole('heading', { name: 'ACME' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /actions/i }));
    fireEvent.click(await screen.findByText(/mark completed/i));

    expect(await screen.findByText(/could not refresh/i)).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'ACME' })).toBeInTheDocument();
    expect(screen.getAllByText(/could not refresh/i)).toHaveLength(1);
  });

  it('forgets the deleted customer, so a Back inside staleTime cannot render it', async () => {
    const client = createAppQueryClient();
    const get = vi.spyOn(api, 'getCustomer').mockResolvedValue(customer as never);
    vi.spyOn(api, 'getOrders').mockResolvedValue(orders as never);
    vi.spyOn(api, 'deleteCustomer').mockResolvedValue(undefined as never);

    window.history.pushState({}, '', '/customers/1');
    render(
      <QueryClientProvider client={client}>
        <Routes>
          <Route path="/customers/:id" element={<CustomerPage />} />
          <Route path="/customers" element={<p>customer list</p>} />
        </Routes>
      </QueryClientProvider>,
    );

    expect(await screen.findByRole('heading', { name: 'ACME' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /^delete$/i }));
    fireEvent.click(await screen.findByRole('button', { name: /^confirm$/i }));

    expect(await screen.findByText('customer list')).toBeInTheDocument();
    await waitFor(() => expect(client.getQueryData(['customer', 1])).toBeUndefined());
    expect(get).toHaveBeenCalledTimes(1);
  });
});
