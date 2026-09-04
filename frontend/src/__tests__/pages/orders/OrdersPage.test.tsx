/**
 * `render` from `__tests__/utils` wraps in a BrowserRouter with no route
 * option — route-aware tests set the URL with pushState first, the way
 * `ProjectsTabs.test.tsx` does.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { fireEvent, screen } from '@testing-library/react';
import { render } from '../../utils';
import { api } from '../../../api/client';
import { OrdersPage } from '../../../pages/orders/OrdersPage';

const rows = [
  { id: 1, name: 'A', status: 'active', customer_id: 1, customer_name: 'ACME', ordered: 2, printed: 1, progress: 0.5, lines_count: 1, priority: 'normal', line_products: [] },
  { id: 2, name: 'B', status: 'completed', customer_id: null, customer_name: null, ordered: 1, printed: 1, progress: 1, lines_count: 1, priority: 'normal', line_products: [] },
];

afterEach(() => {
  window.history.pushState({}, '', '/');
});

describe('OrdersPage', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, 'getCustomers').mockResolvedValue([{ id: 1, name: 'ACME', figures: {} }] as never);
  });
  it('asks the server for the active tab by default and counts every tab from the full list', async () => {
    const get = vi.spyOn(api, 'getOrders').mockResolvedValue(rows as never);
    window.history.pushState({}, '', '/projects');
    render(<OrdersPage />);
    expect(await screen.findByText('A')).toBeInTheDocument();
    // the counts need the unfiltered list; the grid needs the filtered one — one request without status, filtered client-side
    expect(get).toHaveBeenCalledWith({});
    expect(get).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('tab', { name: /active/i }).textContent).toContain('1');
    expect(screen.getByRole('tab', { name: /completed/i }).textContent).toContain('1');
    expect(screen.queryByText('B')).not.toBeInTheDocument();
  });
  it('filters by customer and groups when asked', async () => {
    vi.spyOn(api, 'getOrders').mockResolvedValue(rows as never);
    window.history.pushState({}, '', '/projects');
    render(<OrdersPage />);
    await screen.findByText('A');
    fireEvent.click(screen.getByRole('tab', { name: /all/i }));
    fireEvent.click(screen.getByLabelText(/group by customer/i));
    expect(await screen.findByRole('heading', { name: 'ACME' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /no customer/i })).toBeInTheDocument();
  });
});
