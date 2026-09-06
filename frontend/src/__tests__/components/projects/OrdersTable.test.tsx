import { describe, it, expect } from 'vitest';
import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../../utils';
import { OrdersTable } from '../../../components/projects/OrdersTable';
import type { OrderListItem } from '../../../api/client';

const row = (over: Partial<OrderListItem>): OrderListItem => ({
  id: 1, name: 'A', customer_id: null, customer_name: null, color: null, status: 'active', due_date: null, priority: 'normal',
  price: null, tags: null, cover_image_filename: null, created_at: '2026-09-01T00:00:00', lines_count: 1, ordered: 10, printed: 4,
  progress: 0.4, from_stock_units: 0, line_products: [], prints_in_progress: 2, prints_queued: 3, ...over,
});

describe('OrdersTable', () => {
  it('renders the live counters, sorts most-first on a fresh numeric-column click, and flips on a second', async () => {
    render(<OrdersTable orders={[row({ id: 1, name: 'A', prints_queued: 3 }), row({ id: 2, name: 'B', prints_queued: 9 })]} />);
    const rows = () => screen.getAllByRole('row').slice(1).map((r) => within(r).getAllByRole('cell')[0].textContent);
    expect(rows()).toEqual(['A', 'B']);
    await userEvent.click(screen.getByRole('button', { name: 'Queued' }));
    expect(rows()).toEqual(['B', 'A']);
    expect(screen.getByTestId('order-1-queued')).toHaveTextContent('3');
    expect(screen.getByTestId('order-1-printing')).toHaveTextContent('2');

    // A second click on the same column flips it — back to ascending.
    await userEvent.click(screen.getByRole('button', { name: 'Queued' }));
    expect(rows()).toEqual(['A', 'B']);
  });

  it('a fresh click on the name header sorts ascending, overriding the due-based default', async () => {
    render(
      <OrdersTable
        orders={[row({ id: 1, name: 'B', due_date: '2026-09-01' }), row({ id: 2, name: 'A', due_date: '2026-09-02' })]}
      />,
    );
    const rows = () => screen.getAllByRole('row').slice(1).map((r) => within(r).getAllByRole('cell')[0].textContent);
    // Default sort is due, ascending — the sooner date (B) leads.
    expect(rows()).toEqual(['B', 'A']);

    await userEvent.click(screen.getByRole('button', { name: 'Order' }));
    expect(rows()).toEqual(['A', 'B']);
  });

  it('a fresh click on due sorts ascending — the soonest date leads', async () => {
    render(
      <OrdersTable
        orders={[
          row({ id: 1, name: 'A', due_date: '2026-09-10', prints_in_progress: 9 }),
          row({ id: 2, name: 'B', due_date: '2026-09-05', prints_in_progress: 1 }),
        ]}
      />,
    );
    const rows = () => screen.getAllByRole('row').slice(1).map((r) => within(r).getAllByRole('cell')[0].textContent);

    // Switch away from the due default first — a fresh numeric-column click sorts most-first.
    await userEvent.click(screen.getByRole('button', { name: 'Printing' }));
    expect(rows()).toEqual(['A', 'B']);

    // A fresh click on Due sorts ascending — the soonest date leads.
    await userEvent.click(screen.getByRole('button', { name: 'Due' }));
    expect(rows()).toEqual(['B', 'A']);
  });
});
