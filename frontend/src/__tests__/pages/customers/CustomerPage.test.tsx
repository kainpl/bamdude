/**
 * The `render` helper has no route/path option — the page reads `useParams`,
 * so the URL is set with pushState and the page is mounted under a matching
 * `<Route>` inside the helper's own BrowserRouter.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen } from '@testing-library/react';
import { Routes, Route } from 'react-router-dom';
import { render } from '../../utils';
import { api } from '../../../api/client';
import { CustomerPage } from '../../../pages/customers/CustomerPage';

afterEach(() => {
  window.history.pushState({}, '', '/');
});

describe('CustomerPage', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("shows the full figures and the customer's orders", async () => {
    vi.spyOn(api, 'getCustomer').mockResolvedValue({
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
    } as never);
    const getOrders = vi.spyOn(api, 'getOrders').mockResolvedValue([
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
    ] as never);

    window.history.pushState({}, '', '/customers/1');
    render(
      <Routes>
        <Route path="/customers/:id" element={<CustomerPage />} />
      </Routes>,
    );

    expect(await screen.findByText('Flasks')).toBeInTheDocument();
    expect(getOrders).toHaveBeenCalledWith({ customer_id: 1 });
    // printed / ordered from the detail figures, rendered by ProgressBar — never recomputed here
    expect(screen.getByText('7 / 12')).toBeInTheDocument();
    expect(screen.getByText('VIP')).toBeInTheDocument();
  });
});
