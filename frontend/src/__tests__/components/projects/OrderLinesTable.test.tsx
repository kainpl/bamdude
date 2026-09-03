/**
 * The lines table is the only place an order's work is edited, so the three
 * things it must never get wrong are covered here: the order rows come in
 * (`sort_order`), the progress it shows is the server's (`units_printed`), and
 * a reorder is the TWO patches that swap two neighbours — not one patch and a
 * client-side renumbering nobody sent.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, fireEvent, waitFor } from '@testing-library/react';
import { render } from '../../utils';
import { api } from '../../../api/client';
import type { Order } from '../../../api/client';
import { OrderLinesTable } from '../../../components/projects/OrderLinesTable';

const order = {
  id: 1,
  status: 'active',
  lines: [
    {
      id: 10,
      product_id: 1,
      product_name: 'Flask',
      quantity: 2,
      material: 'PETG',
      color: null,
      note: null,
      sort_order: 0,
      units_printed: 1,
      progress: 0.5,
      archive_ids: [],
      parts: [
        { part_id: 1, name: 'flask', qty_per_unit: 1, need: 2, usable: 1, in_progress: 0, remaining: 1, surplus: 0 },
      ],
    },
    {
      id: 11,
      product_id: 2,
      product_name: 'Lid',
      quantity: 4,
      material: null,
      color: null,
      note: null,
      sort_order: 1,
      units_printed: 4,
      progress: 1,
      archive_ids: [],
      parts: [],
    },
  ],
} as unknown as Order;

describe('OrderLinesTable', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('renders lines in sort order with server progress and expands into parts', async () => {
    render(<OrderLinesTable order={order} canEdit onPrintPlate={() => {}} />);
    const rows = screen.getAllByRole('row').filter((r) => r.getAttribute('data-line'));
    expect(rows.map((r) => r.getAttribute('data-line'))).toEqual(['10', '11']);
    expect(screen.getByText('1 / 2')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('line-10-expand'));
    expect(screen.getByText('flask')).toBeInTheDocument();
    expect(screen.getByTestId('part-1-remaining').textContent).toBe('1');
  });

  it('moving a line down swaps sort_order with its neighbour through two PATCHes', async () => {
    const patch = vi.spyOn(api, 'updateOrderLine').mockResolvedValue(order);
    render(<OrderLinesTable order={order} canEdit onPrintPlate={() => {}} />);
    fireEvent.click(screen.getByTestId('line-10-down'));
    await waitFor(() => expect(patch).toHaveBeenCalledTimes(2));
    expect(patch).toHaveBeenCalledWith(1, 10, { sort_order: 1 });
    expect(patch).toHaveBeenCalledWith(1, 11, { sort_order: 0 });
  });

  it('adds a line with the picked product and an uppercased material', async () => {
    vi.spyOn(api, 'getProducts').mockResolvedValue([{ id: 3, name: 'Cap', is_active: true }] as never);
    const add = vi.spyOn(api, 'addOrderLine').mockResolvedValue(order);
    render(<OrderLinesTable order={order} canEdit onPrintPlate={() => {}} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Cap' }));
    fireEvent.change(screen.getByLabelText(/material/i), { target: { value: 'petg' } });
    fireEvent.blur(screen.getByLabelText(/material/i));
    fireEvent.click(screen.getByRole('button', { name: /add line/i }));
    await waitFor(() =>
      expect(add).toHaveBeenCalledWith(1, { product_id: 3, quantity: 1, material: 'PETG', color: null, note: null }),
    );
  });
});
