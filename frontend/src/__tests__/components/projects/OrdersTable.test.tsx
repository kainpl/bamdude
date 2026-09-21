import { describe, it, expect } from 'vitest';
import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../../utils';
import { OrdersTable } from '../../../components/projects/OrdersTable';
import type { OrderForecast, OrderListItem } from '../../../api/client';

const row = (over: Partial<OrderListItem>): OrderListItem => ({
  id: 1, name: 'A', customer_id: null, customer_name: null, color: null, status: 'active', due_date: null, priority: 'normal',
  price: null, tags: null, cover_image_filename: null, created_at: '2026-09-01T00:00:00', lines_count: 1, ordered: 10, printed: 4,
  progress: 0.4, from_stock_units: 0, line_products: [], prints_in_progress: 2, prints_queued: 3, ...over,
});

const fc = (over: Partial<OrderForecast>): OrderForecast => ({
  project_id: 1, now_eta: null, now_seconds: null, after_eta: null, after_seconds: null, machine_seconds: null,
  unknown_prints: 0, unroutable_prints: 0, eta_complete: true, ahead_count: 0, assumptions: ['drying'], ...over,
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

  it('shows ready-at and machine hours from the forecast and sorts by ready-at soonest first', async () => {
    const forecasts = {
      1: fc({ project_id: 1, now_eta: '2026-09-07T10:00:00Z', now_seconds: 7200, after_eta: '2026-09-08T10:00:00Z', after_seconds: 93600, machine_seconds: 5400, ahead_count: 1 }),
      2: fc({ project_id: 2, now_eta: '2026-09-06T14:00:00Z', now_seconds: 3600, after_eta: '2026-09-06T14:00:00Z', after_seconds: 3600, machine_seconds: 3600 }),
    };
    render(<OrdersTable orders={[row({ id: 1, name: 'A' }), row({ id: 2, name: 'B' })]} forecasts={forecasts} />);
    expect(screen.getByTestId('order-1-machine-hours')).toHaveTextContent('1:30');
    expect(screen.getByTestId('order-1-after')).toHaveTextContent(/after 1 more urgent order/);
    expect(screen.queryByTestId('order-2-after')).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Ready' }));
    const names = screen.getAllByRole('row').slice(1).map((r) => within(r).getAllByRole('cell')[0].textContent);
    expect(names).toEqual(['B', 'A']);
  });

  it('renders «…» while the forecast is missing and «No estimate» for a null ETA', () => {
    render(<OrdersTable orders={[row({ id: 1, name: 'A' })]} forecasts={{ 1: fc({ unknown_prints: 3 }) }} />);
    expect(screen.getByTestId('order-1-ready')).toHaveTextContent('No estimate');
    render(<OrdersTable orders={[row({ id: 5, name: 'C' })]} />);
    expect(screen.getByTestId('order-5-ready')).toHaveTextContent('…');
  });

  it('does not call the placed subset ready when the forecast is incomplete', () => {
    render(
      <OrdersTable
        orders={[row({ id: 1, name: 'A' })]}
        forecasts={{ 1: fc({ now_eta: '2026-09-07T10:00:00Z', eta_complete: false, unroutable_prints: 1 }) }}
      />,
    );
    expect(screen.getByTestId('order-1-ready')).toHaveTextContent('Incomplete estimate');
    expect(screen.getByTestId('order-1-ready')).not.toHaveTextContent('Sep');
  });

  it('a failed fetch reads as an error, never as «No estimate»', () => {
    // ⚠️ Three states share these cells and only one is about the farm.
    // Mapping a dead request onto «No estimate» — which means «the simulation
    // could place nothing» — sends the operator hunting a scheduling problem
    // that is really a broken request.
    render(<OrdersTable orders={[row({ id: 1, name: 'A' })]} forecastError />);
    const ready = screen.getByTestId('order-1-ready');
    expect(ready).toHaveTextContent('—');
    expect(ready).not.toHaveTextContent('No estimate');
    expect(within(ready).getByTitle('Forecast unavailable')).toBeInTheDocument();
    expect(screen.getByTestId('order-1-machine-hours')).toHaveTextContent('—');
  });

  it('a closed order is dashed in both cells — closed means nothing is planned', () => {
    // The batch is not even asked about it (spec Decision 9), so `forecasts`
    // legitimately has no entry; without this branch the cell would read
    // «No estimate» and invite somebody to go looking for a printer.
    render(
      <OrdersTable
        orders={[row({ id: 1, name: 'A', status: 'completed' })]}
        forecasts={{ 2: fc({ project_id: 2 }) }}
      />,
    );
    const ready = screen.getByTestId('order-1-ready');
    expect(ready).toHaveTextContent('—');
    expect(ready).not.toHaveTextContent('No estimate');
    expect(screen.getByTestId('order-1-machine-hours')).toHaveTextContent('—');
  });
});
