/**
 * The plan block is the only place in the app that does arithmetic on order
 * data client-side, so the two things it must not get wrong are covered here:
 * the what-if follows the PLATE'S yields (not the row's clipped `useful`), and
 * "whole plan to queue" sends exactly the rows the operator left standing.
 *
 * ⚠️ `useful` on row 100 is deliberately 10 while its plate yields 10 and row
 * 200's `useful` is 2 while its plate yields 5 — a projection that reached for
 * `useful` instead of the recipe would pass the first test and fail the second.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, fireEvent, waitFor, within } from '@testing-library/react';
import { useQuery } from '@tanstack/react-query';
import { render } from '../../utils';
import { strayZeroTextNodes } from '../../domHelpers';
import { api } from '../../../api/client';
import type { Order, OrderPlan, PlateRecipe } from '../../../api/client';
import { PlanBlock } from '../../../components/projects/PlanBlock';

/** A stand-in for `OrderPage`'s own `['project', id]` query — an invalidation
 *  is observable only as a refetch, and only while something watches the key. */
function OrderProbe({ id, onFetch }: { id: number; onFetch: () => void }) {
  useQuery({
    queryKey: ['project', id],
    queryFn: async () => {
      onFetch();
      return null;
    },
  });
  return null;
}

const order = {
  id: 1,
  name: 'Ten flasks',
  status: 'active',
  lines: [
    {
      id: 10,
      product_id: 7,
      product_name: 'Flask',
      quantity: 12,
      material: 'PETG',
      color: null,
      note: null,
      sort_order: 0,
      units_printed: 0,
      progress: 0,
      archive_ids: [],
      parts: [
        { part_id: 1, name: 'Body', qty_per_unit: 1, need: 12, usable: 0, in_progress: 0, remaining: 12, surplus: 0 },
        { part_id: 2, name: 'Cap', qty_per_unit: 1, need: 12, usable: 9, in_progress: 0, remaining: 3, surplus: 0 },
      ],
    },
  ],
} as unknown as Order;

const plan: OrderPlan = {
  lines: [
    {
      line_id: 10,
      product_id: 7,
      product_name: 'Flask',
      material: 'PETG',
      outstanding_before: [{ part_id: 1, name: 'Body', count: 12 }],
      rows: [
        {
          plate_id: 100,
          library_file_id: 5,
          plate_index: 1,
          filename: 'big.3mf',
          count: 1,
          useful: [{ part_id: 1, name: 'Body', count: 10 }],
          print_time_seconds: 3600,
          filament_used_grams: 100,
          cost: 2,
          time_unknown: false,
        },
        {
          plate_id: 200,
          library_file_id: 6,
          plate_index: 0,
          filename: 'small.3mf',
          count: 1,
          useful: [{ part_id: 1, name: 'Body', count: 2 }],
          print_time_seconds: 1800,
          filament_used_grams: 20,
          cost: 0.4,
          time_unknown: false,
        },
      ],
      surplus_after: [{ part_id: 1, name: 'Body', count: 3 }],
      unsatisfiable: [{ part_id: 2, name: 'Cap', count: 3 }],
      candidates: [100, 200],
      not_sliced: [],
    },
  ],
  totals: { prints: 2, print_time_seconds: 5400, filament_used_grams: 120, cost: 2.4 },
};

const plates: PlateRecipe[] = [
  {
    id: 100,
    library_file_id: 5,
    plate_index: 1,
    filename: 'big.3mf',
    sliced: true,
    yield: [{ part_id: 1, name: 'Body', count: 10 }],
    unassigned: [],
    materials: ['PETG'],
    colors: [],
    print_time_seconds: 3600,
    filament_used_grams: 100,
  },
  {
    id: 200,
    library_file_id: 6,
    plate_index: 0,
    filename: 'small.3mf',
    sliced: true,
    yield: [{ part_id: 1, name: 'Body', count: 5 }],
    unassigned: [],
    materials: ['PETG'],
    colors: [],
    print_time_seconds: 1800,
    filament_used_grams: 20,
  },
];

describe('PlanBlock', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, 'getOrderPlan').mockResolvedValue(plan);
    vi.spyOn(api, 'getProductPlates').mockResolvedValue(plates);
  });

  it('renders the recommended plates of every line', async () => {
    render(<PlanBlock order={order} canEdit />);

    expect(await screen.findByTestId('plan-row-100')).toHaveTextContent('big.3mf');
    expect(screen.getByTestId('plan-row-200')).toHaveTextContent('small.3mf');
    expect(screen.getByTestId('plan-row-100-count')).toHaveValue(1);
    // The whole file, not "plate 0".
    expect(screen.getByTestId('plan-row-200')).toHaveTextContent(/whole file/i);
    expect(screen.getByTestId('plan-totals-prints')).toHaveTextContent('2');
    // `0 && <jsx>` renders the NUMBER, and this block is full of counts that
    // legitimately reach zero — the detector is the only thing that sees one
    // left behind where a conditional used to be.
    expect(strayZeroTextNodes(screen.getByTestId('plan-block'))).toHaveLength(0);
  });

  it('leaves no bare zero behind when a row is emptied', async () => {
    render(<PlanBlock order={order} canEdit />);

    fireEvent.click(await screen.findByTestId('plan-row-200-dec'));

    // The count INPUT holds "0" as a value, not as a text node, so the row at
    // zero is still expected to leave the tree clean.
    expect(strayZeroTextNodes(screen.getByTestId('plan-block'))).toHaveLength(0);
  });

  it('recomputes the surplus and the totals from the plate yields when a count is raised', async () => {
    render(<PlanBlock order={order} canEdit />);

    // The server's own surplus first: 10 + 5 bodies against a need of 12.
    const surplus = await screen.findByTestId('plan-line-10-surplus');
    await waitFor(() => expect(surplus).toHaveTextContent('3'));

    fireEvent.click(screen.getByTestId('plan-row-100-inc'));

    expect(screen.getByTestId('plan-row-100-count')).toHaveValue(2);
    // 2 × 10 + 1 × 5 = 25 bodies, 13 more than the 12 outstanding.
    await waitFor(() => expect(screen.getByTestId('plan-line-10-surplus')).toHaveTextContent('13'));
    expect(screen.getByTestId('plan-totals-prints')).toHaveTextContent('3');
    expect(screen.getByTestId('plan-totals-time')).toHaveTextContent('2h 30m');
    expect(screen.getByTestId('plan-totals-grams')).toHaveTextContent('220.0');
    expect(screen.getByTestId('plan-totals-cost')).toHaveTextContent('$4.40');
  });

  it('sends only the rows left standing, and drops the caches the order is read from', async () => {
    const enqueue = vi
      .spyOn(api, 'enqueueOrderPlan')
      .mockResolvedValue({ created: [{ line_id: 10, plate_id: 100, queue_item_ids: [77] }] });
    const onProbeFetch = vi.fn();

    render(
      <>
        <PlanBlock order={order} canEdit />
        <OrderProbe id={1} onFetch={onProbeFetch} />
      </>,
    );

    fireEvent.click(await screen.findByTestId('plan-row-200-dec'));
    expect(screen.getByTestId('plan-row-200-count')).toHaveValue(0);

    fireEvent.click(screen.getByTestId('plan-enqueue-all'));

    await waitFor(() =>
      expect(enqueue).toHaveBeenCalledWith(1, {
        items: [{ plate_id: 100, count: 1, line_id: 10 }],
        target: { kind: 'auto' },
      }),
    );
    // The plan itself is re-read, and so is the order behind it.
    await waitFor(() => expect(api.getOrderPlan).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(onProbeFetch).toHaveBeenCalledTimes(2));
  });

  it('names a part no plate can make, and points at the product’s files', async () => {
    render(<PlanBlock order={order} canEdit />);

    const row = await screen.findByTestId('plan-unsatisfiable-2');
    expect(row).toHaveTextContent(/no plate for/i);
    expect(row).toHaveTextContent('Cap');
    expect(row).toHaveTextContent('PETG');
    expect(within(row).getByRole('link')).toHaveAttribute('href', '/products/7#files');
    // The slice slot is reserved, not offered.
    expect(within(row).getByRole('button')).toBeDisabled();
  });

  it('plans nothing for a closed order', async () => {
    render(<PlanBlock order={{ ...order, status: 'completed' }} canEdit />);

    expect(await screen.findByTestId('plan-closed')).toBeInTheDocument();
    expect(screen.queryByTestId('plan-row-100')).not.toBeInTheDocument();
    expect(api.getOrderPlan).not.toHaveBeenCalled();
  });

  it('says so when there is nothing outstanding left to plan', async () => {
    vi.spyOn(api, 'getOrderPlan').mockResolvedValue({
      lines: [{ ...plan.lines[0], rows: [], outstanding_before: [], unsatisfiable: [], surplus_after: [] }],
      totals: { prints: 0, print_time_seconds: 0, filament_used_grams: 0, cost: null },
    });

    render(<PlanBlock order={order} canEdit />);

    expect(await screen.findByTestId('plan-empty')).toBeInTheDocument();
    expect(screen.queryByTestId('plan-enqueue-all')).not.toBeInTheDocument();
  });

  it('hides every queue action from a reader', async () => {
    render(<PlanBlock order={order} canEdit={false} />);

    expect(await screen.findByTestId('plan-row-100')).toBeInTheDocument();
    expect(screen.queryByTestId('plan-enqueue-all')).not.toBeInTheDocument();
    expect(screen.queryByTestId('plan-row-100-queue')).not.toBeInTheDocument();
  });
});
