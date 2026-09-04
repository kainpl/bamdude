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
    expect(patch).not.toHaveBeenCalled();
    // The editor is closed, so the row offers Edit again rather than Save.
    expect(screen.getByTestId('line-10-edit')).toBeInTheDocument();
    expect(screen.queryByTestId('line-10-save')).not.toBeInTheDocument();
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
});
