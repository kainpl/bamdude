/**
 * `render` from `__tests__/utils` wraps in a BrowserRouter with no route
 * option — route-aware tests set the URL with pushState first, the way
 * `ProjectsTabs.test.tsx` does.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { fireEvent, screen, within } from '@testing-library/react';
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
  it('draws a skeleton grid while the FIRST fetch is in flight, and never over data', async () => {
    // ⚠️ `isLoading`, not `isFetching`. Every order mutation invalidates
    // `['projects']`, so a background refetch is routine — replacing the grid
    // with grey boxes each time would be worse than figures one request old.
    let resolve: (rows: unknown) => void = () => {};
    vi.spyOn(api, 'getOrders').mockReturnValue(
      new Promise((r) => {
        resolve = r as (rows: unknown) => void;
      }) as never,
    );
    window.history.pushState({}, '', '/projects');
    render(<OrdersPage />);

    const skeleton = await screen.findByTestId('orders-skeleton');
    expect(skeleton).toBeInTheDocument();
    // ⚠️ The grey boxes are `aria-hidden`, so without this the wait is SILENCE
    // to a screen reader and the page reads as having no orders. The status
    // role carries one visually-hidden sentence; the cards stay hidden so
    // nobody hears six empty ones.
    expect(skeleton).toHaveAttribute('role', 'status');
    expect(skeleton).toHaveAttribute('aria-busy', 'true');
    expect(within(skeleton).getByText('Loading...')).toBeInTheDocument();
    // The empty-state sentence is not the loading state — it would read as
    // "you have no orders" over a list that is simply still on its way.
    expect(screen.queryByText(/no active orders/i)).not.toBeInTheDocument();

    resolve(rows);
    expect(await screen.findByText('A')).toBeInTheDocument();
    expect(screen.queryByTestId('orders-skeleton')).not.toBeInTheDocument();
  });
});
