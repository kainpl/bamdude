/**
 * Adding a line, and the kits it takes off the shelf (pass 8, Decision 4).
 *
 * The «From stock» box exists only when there IS stock, defaults to
 * `min(kits_available, quantity)` — the operator's usual answer — and is
 * editable down to 0, which is the other answer and must not be sent as a
 * reservation of nothing. What the server actually reserved can be SMALLER
 * than what was asked, and the row is about to reset, so the honest number is
 * said out loud rather than left in a box nobody will look at again.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, fireEvent, waitFor } from '@testing-library/react';
import { render } from '../../utils';
import { api } from '../../../api/client';
import type { Order, ProductStock } from '../../../api/client';
import { AddLineRow } from '../../../components/projects/AddLineRow';

const stock: ProductStock = {
  kits_available: 5,
  balances: [{ part_id: 1, name: 'Flask', qty_per_unit: 1, balance: 5 }],
  movements: [],
};

/** What `addOrderLine` answers with — the WHOLE order, so the new line's real
 *  `from_stock_units` comes back with it. Line 12 is the newly created one. */
function savedOrder(fromStock: number): Order {
  return {
    id: 1,
    lines: [
      { id: 11, product_id: 3, product_name: 'Cap', quantity: 1, from_stock_units: 0, parts: [] },
      { id: 12, product_id: 3, product_name: 'Cap', quantity: 3, from_stock_units: fromStock, parts: [] },
    ],
  } as unknown as Order;
}

function mount() {
  render(
    <table>
      <tbody>
        <AddLineRow orderId={1} />
      </tbody>
    </table>,
  );
}

describe('AddLineRow · from stock', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, 'getProducts').mockResolvedValue([{ id: 3, name: 'Cap', is_active: true }] as never);
  });

  it('asks nothing about stock until a product is picked', async () => {
    const get = vi.spyOn(api, 'getProductStock').mockResolvedValue(stock);
    mount();

    // ⚠️ A DISABLED query in TanStack v5 is pending and NOT fetching — the row
    // must not ask the server about a product nobody chose.
    expect(await screen.findByRole('button', { name: 'Cap' })).toBeInTheDocument();
    expect(get).not.toHaveBeenCalled();
    expect(screen.queryByLabelText(/from stock/i)).not.toBeInTheDocument();
  });

  it('defaults to min(kits, quantity) and follows the quantity until it is touched', async () => {
    vi.spyOn(api, 'getProductStock').mockResolvedValue(stock);
    mount();
    fireEvent.click(await screen.findByRole('button', { name: 'Cap' }));

    // Five on the shelf, one asked for: one kit, not five.
    const box = (await screen.findByTestId('add-line-from-stock')) as HTMLInputElement;
    expect(box.value).toBe('1');

    // The default follows the quantity while the operator has not overruled it.
    fireEvent.change(screen.getByLabelText(/quantity/i), { target: { value: '3' } });
    expect((screen.getByTestId('add-line-from-stock') as HTMLInputElement).value).toBe('3');

    // …and never exceeds the shelf.
    fireEvent.change(screen.getByLabelText(/quantity/i), { target: { value: '9' } });
    expect((screen.getByTestId('add-line-from-stock') as HTMLInputElement).value).toBe('5');
  });

  it('sends the reservation with the new line', async () => {
    vi.spyOn(api, 'getProductStock').mockResolvedValue(stock);
    const add = vi.spyOn(api, 'addOrderLine').mockResolvedValue(savedOrder(3));
    mount();
    fireEvent.click(await screen.findByRole('button', { name: 'Cap' }));
    // ⚠️ The shelf has to have ARRIVED before the quantity is typed: until it
    // does there are no kits to offer, and a click that beat the answer would
    // send a line reserving nothing while the box on screen said otherwise.
    await screen.findByTestId('add-line-from-stock');
    fireEvent.change(screen.getByLabelText(/quantity/i), { target: { value: '3' } });
    fireEvent.click(screen.getByRole('button', { name: /add line/i }));

    await waitFor(() =>
      expect(add).toHaveBeenCalledWith(1, {
        product_id: 3,
        quantity: 3,
        material: null,
        color: null,
        note: null,
        from_stock_units: 3,
      }),
    );
  });

  it('omits the field entirely when the operator lowers it to zero', async () => {
    vi.spyOn(api, 'getProductStock').mockResolvedValue(stock);
    const add = vi.spyOn(api, 'addOrderLine').mockResolvedValue(savedOrder(0));
    mount();
    fireEvent.click(await screen.findByRole('button', { name: 'Cap' }));
    fireEvent.change(await screen.findByTestId('add-line-from-stock'), { target: { value: '0' } });
    fireEvent.click(screen.getByRole('button', { name: /add line/i }));

    // ⚠️ Absent, not `from_stock_units: 0`: the server defaults it to zero, and
    // a field nobody typed riding on every request would run the reservation
    // path for products that hold no stock at all.
    await waitFor(() =>
      expect(add).toHaveBeenCalledWith(1, {
        product_id: 3,
        quantity: 1,
        material: null,
        color: null,
        note: null,
      }),
    );
  });

  it('says so when the shelf emptied and less was reserved than asked', async () => {
    vi.spyOn(api, 'getProductStock').mockResolvedValue(stock);
    // Asked for three, the server could only hold one back.
    vi.spyOn(api, 'addOrderLine').mockResolvedValue(savedOrder(1));
    mount();
    fireEvent.click(await screen.findByRole('button', { name: 'Cap' }));
    // ⚠️ The shelf has to have ARRIVED before the quantity is typed: until it
    // does there are no kits to offer, and a click that beat the answer would
    // send a line reserving nothing while the box on screen said otherwise.
    await screen.findByTestId('add-line-from-stock');
    fireEvent.change(screen.getByLabelText(/quantity/i), { target: { value: '3' } });
    fireEvent.click(screen.getByRole('button', { name: /add line/i }));

    expect(await screen.findByText(/only 1 could be reserved/i)).toBeInTheDocument();
  });
});
