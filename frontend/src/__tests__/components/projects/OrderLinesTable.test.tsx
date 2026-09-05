/**
 * The lines table is the only place an order's work is edited, so the four
 * things it must never get wrong are covered here: the order rows come in
 * (`sort_order`), the progress it shows is the server's (`units_printed`), a
 * reorder is the TWO patches that swap two neighbours — not one patch and a
 * client-side renumbering nobody sent — and a HALF-applied swap refetches, so
 * the operator is never left looking at an order the server no longer holds.
 *
 * ⚠️ The fixture lists line 11 BEFORE line 10 on purpose. Their `sort_order`
 * values say otherwise, so a component that dropped its `.sort(...)` and just
 * rendered the array would fail the first test instead of passing it by
 * coincidence.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, fireEvent, waitFor, within } from '@testing-library/react';
import { useQuery } from '@tanstack/react-query';
import { render } from '../../utils';
import { api } from '../../../api/client';
import type { Order } from '../../../api/client';
import { OrderLinesTable } from '../../../components/projects/OrderLinesTable';

/**
 * A stand-in for `OrderPage`'s own `['project', id]` query.
 *
 * Invalidation is only observable as a REFETCH, and only while something is
 * watching the key — the table itself watches nothing. Mounting this beside it
 * is what turns "the cache was dropped" into an assertion about `api.getOrder`
 * being asked again, which is the behaviour the operator actually gets.
 */
function OrderProbe({ id }: { id: number }) {
  useQuery({ queryKey: ['project', id], queryFn: () => api.getOrder(id) });
  return null;
}

const order = {
  id: 1,
  status: 'active',
  lines: [
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
      from_stock_units: 0,
      progress: 1,
      archive_ids: [],
      parts: [],
    },
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
      from_stock_units: 2,
      progress: 0.5,
      archive_ids: [],
      parts: [
        { part_id: 1, name: 'flask', qty_per_unit: 1, need: 2, usable: 1, in_progress: 0, remaining: 1, surplus: 0 },
      ],
    },
  ],
} as unknown as Order;

describe('OrderLinesTable', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('renders lines in sort order with server progress and expands into parts', async () => {
    render(<OrderLinesTable order={order} canEdit />);
    const rows = screen.getAllByRole('row').filter((r) => r.getAttribute('data-line'));
    expect(rows.map((r) => r.getAttribute('data-line'))).toEqual(['10', '11']);
    expect(screen.getByText('1 / 2')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('line-10-expand'));
    expect(screen.getByText('flask')).toBeInTheDocument();
    expect(screen.getByTestId('part-1-remaining').textContent).toBe('1');
  });

  it('moving a line down swaps sort_order with its neighbour through two PATCHes', async () => {
    const patch = vi.spyOn(api, 'updateOrderLine').mockResolvedValue(order);
    render(<OrderLinesTable order={order} canEdit />);
    fireEvent.click(screen.getByTestId('line-10-down'));
    await waitFor(() => expect(patch).toHaveBeenCalledTimes(2));
    expect(patch).toHaveBeenCalledWith(1, 10, { sort_order: 1 });
    expect(patch).toHaveBeenCalledWith(1, 11, { sort_order: 0 });
  });

  it('adds a line with the picked product and an uppercased material', async () => {
    vi.spyOn(api, 'getProducts').mockResolvedValue([{ id: 3, name: 'Cap', is_active: true }] as never);
    const add = vi.spyOn(api, 'addOrderLine').mockResolvedValue(order);
    render(<OrderLinesTable order={order} canEdit />);
    fireEvent.click(await screen.findByRole('button', { name: 'Cap' }));
    fireEvent.change(screen.getByLabelText(/material/i), { target: { value: 'petg' } });
    fireEvent.blur(screen.getByLabelText(/material/i));
    fireEvent.click(screen.getByRole('button', { name: /add line/i }));
    await waitFor(() =>
      expect(add).toHaveBeenCalledWith(1, { product_id: 3, quantity: 1, material: 'PETG', color: null, note: null }),
    );
  });

  it('refetches the order when only half of a swap lands', async () => {
    // The first PATCH is committed on the server and the second is not, so the
    // component's own idea of the order is now wrong in a way it cannot detect.
    // Invalidating only `onSuccess` would leave the pre-swap rows on screen for
    // ever — and since the two lines then share a `sort_order`, every further
    // swap is a no-op. `onSettled` is what makes the operator see the truth.
    const patch = vi
      .spyOn(api, 'updateOrderLine')
      .mockResolvedValueOnce(order)
      .mockRejectedValueOnce(new Error('Line not found'));
    const refetch = vi.spyOn(api, 'getOrder').mockResolvedValue(order);

    render(
      <>
        <OrderProbe id={1} />
        <OrderLinesTable order={order} canEdit />
      </>,
    );
    await waitFor(() => expect(refetch).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByTestId('line-10-down'));

    await waitFor(() => expect(patch).toHaveBeenCalledTimes(2));
    // The failure is reported...
    expect(await screen.findByText('Line not found')).toBeInTheDocument();
    // ...and the cache is dropped anyway, which the watching `['project', 1]`
    // observer turns into a second, real fetch of the true server order.
    await waitFor(() => expect(refetch).toHaveBeenCalledTimes(2));
  });

  it('a save with nothing changed sends no PATCH and just closes the editor', async () => {
    const patch = vi.spyOn(api, 'updateOrderLine').mockResolvedValue(order);
    render(<OrderLinesTable order={order} canEdit />);

    fireEvent.click(screen.getByTestId('line-10-edit'));
    fireEvent.click(screen.getByTestId('line-10-save'));

    // Opening a row and closing it again is not an edit: `PATCH {}` would bump
    // the order's `updated_at` and refetch two keys to change nothing.
    //
    // ⚠️ The editor-closed assertion below is the load-bearing one. TanStack v5
    // defers `mutationFn` past `onMutate`, so a synchronous "not called" would
    // hold for a moment even if a PATCH were on its way — awaiting the closed
    // editor is what puts a turn of the microtask queue between the click and
    // the question, and `waitFor` says so rather than relying on it.
    await waitFor(() => expect(screen.queryByTestId('line-10-save')).not.toBeInTheDocument());
    expect(patch).not.toHaveBeenCalled();
    // The editor is closed, so the row offers Edit again rather than Save.
    expect(screen.getByTestId('line-10-edit')).toBeInTheDocument();
  });

  it('folds the material before comparing it, so a typed " petg " is saved as PETG', async () => {
    // The fold is in `changedFields`, NOT only on the field's blur — the plates
    // spell their material upper-case and the server stores whatever it is
    // given, so a row saved with a lower-case material would keep failing to
    // match them. Saving is the repair. Nothing is blurred here on purpose:
    // clicking Save straight out of the box is the path that used to send the
    // raw string.
    const patch = vi.spyOn(api, 'updateOrderLine').mockResolvedValue(order);
    render(<OrderLinesTable order={order} canEdit />);

    fireEvent.click(screen.getByTestId('line-11-edit'));
    // Scoped to the row: the add-line row at the bottom carries a material box
    // of its own, so a page-wide query finds two.
    const row = screen.getByTestId('line-11-save').closest('tr') as HTMLElement;
    fireEvent.change(within(row).getByLabelText(/material/i), { target: { value: ' petg ' } });
    fireEvent.click(screen.getByTestId('line-11-save'));

    // Trimmed, upper-cased, and alone — the untouched fields are not restated.
    await waitFor(() => expect(patch).toHaveBeenCalledWith(1, 11, { material: 'PETG' }));
  });

  it('sends nothing when the folded material equals the one already stored', async () => {
    // The other half of the same rule, and the one a fold applied only to the
    // OUTGOING value would break: line 10 already holds `PETG`, so typing
    // " petg " is not an edit. A `PATCH {}` here would bump the order's
    // `updated_at` and refetch two keys to change nothing.
    const patch = vi.spyOn(api, 'updateOrderLine').mockResolvedValue(order);
    render(<OrderLinesTable order={order} canEdit />);

    fireEvent.click(screen.getByTestId('line-10-edit'));
    const row = screen.getByTestId('line-10-save').closest('tr') as HTMLElement;
    fireEvent.change(within(row).getByLabelText(/material/i), { target: { value: ' petg ' } });
    fireEvent.click(screen.getByTestId('line-10-save'));

    // Same discriminator as the test above: the closed editor is what proves the
    // decision was taken, and a deferred `mutationFn` cannot hide behind it.
    await waitFor(() => expect(screen.queryByTestId('line-10-save')).not.toBeInTheDocument());
    expect(patch).not.toHaveBeenCalled();
    expect(screen.getByTestId('line-10-edit')).toBeInTheDocument();
  });

  it('no longer offers a print of its own — the plan block owns that', async () => {
    // The row's "print a plate…" picker was the interim answer of pass 2. The
    // plan block replaced it, and a second door onto PrintModal from the same
    // page would queue plates the plan is not counting.
    render(<OrderLinesTable order={order} canEdit />);

    expect(await screen.findByTestId('line-10-expand')).toBeInTheDocument();
    expect(screen.queryByTestId('line-10-print')).not.toBeInTheDocument();
  });
  describe('kits from stock (pass 8)', () => {
    // Line 10 holds two kits; line 11 holds none. `stock` is what is still FREE
    // — the line's own reservation is not in it, which is why the editable
    // ceiling below is the two added together.
    const stock = {
      kits_available: 1,
      balances: [{ part_id: 1, name: 'flask', qty_per_unit: 1, balance: 1 }],
      movements: [],
    };

    it('shows what a line already takes off the shelf, and nothing for a line that takes none', () => {
      render(<OrderLinesTable order={order} canEdit />);

      expect(screen.getByTestId('line-10-from-stock-shown')).toHaveTextContent('from stock 2');
      // ⚠️ Not "from stock 0" and not a bare 0 either — line 11 reserves
      // nothing, so the row says nothing.
      expect(screen.queryByTestId('line-11-from-stock-shown')).not.toBeInTheDocument();
    });

    it('offers the free kits PLUS the line own, because an edit releases its reservation first', async () => {
      vi.spyOn(api, 'getProductStock').mockResolvedValue(stock as never);
      render(<OrderLinesTable order={order} canEdit />);

      fireEvent.click(screen.getByTestId('line-10-edit'));
      const box = (await screen.findByTestId('line-10-from-stock')) as HTMLInputElement;
      expect(box.value).toBe('2');
      // One free + two this line already holds = three, capped by the line's
      // own quantity of two. A ceiling of one would make the box unable to
      // re-save the number it opened with.
      expect(box.max).toBe('2');
    });

    it('sends the rewritten reservation, and nothing else', async () => {
      vi.spyOn(api, 'getProductStock').mockResolvedValue(stock as never);
      const patch = vi.spyOn(api, 'updateOrderLine').mockResolvedValue(order);
      render(<OrderLinesTable order={order} canEdit />);

      fireEvent.click(screen.getByTestId('line-10-edit'));
      fireEvent.change(await screen.findByTestId('line-10-from-stock'), { target: { value: '1' } });
      fireEvent.click(screen.getByTestId('line-10-save'));

      await waitFor(() => expect(patch).toHaveBeenCalledWith(1, 10, { from_stock_units: 1 }));
    });

    it('leaves the reservation alone on a save that did not touch it', async () => {
      vi.spyOn(api, 'getProductStock').mockResolvedValue(stock as never);
      const patch = vi.spyOn(api, 'updateOrderLine').mockResolvedValue(order);
      render(<OrderLinesTable order={order} canEdit />);

      fireEvent.click(screen.getByTestId('line-10-edit'));
      await screen.findByTestId('line-10-from-stock');
      // Scoped to the row: the add-line row at the bottom carries a note box of
      // its own, so a page-wide query finds two.
      const row = screen.getByTestId('line-10-save').closest('tr') as HTMLElement;
      fireEvent.change(within(row).getByLabelText(/note/i), { target: { value: 'urgent' } });
      fireEvent.click(screen.getByTestId('line-10-save'));

      // ⚠️ Absent, not the current value restated: on this field alone, absent
      // means "leave it" and a number means "rewrite it" — release and reserve
      // again, with the ledger rows that record both. A note edit must not
      // rewrite a reservation.
      await waitFor(() => expect(patch).toHaveBeenCalledWith(1, 10, { note: 'urgent' }));
    });

    it('says so when the shelf emptied and less was reserved than asked', async () => {
      vi.spyOn(api, 'getProductStock').mockResolvedValue(stock as never);
      // Line 11 reserves nothing yet. Asked for two, the server could only hold
      // one back — the shelf emptied between the row opening and Save.
      const saved = {
        ...order,
        lines: order.lines.map((l) => (l.id === 11 ? { ...l, from_stock_units: 1 } : l)),
      } as unknown as typeof order;
      vi.spyOn(api, 'updateOrderLine').mockResolvedValue(saved);
      render(<OrderLinesTable order={order} canEdit />);

      fireEvent.click(screen.getByTestId('line-11-edit'));
      fireEvent.change(await screen.findByTestId('line-11-from-stock'), { target: { value: '2' } });
      fireEvent.click(screen.getByTestId('line-11-save'));

      expect(await screen.findByText(/only 1 could be reserved/i)).toBeInTheDocument();
    });
  });
});
