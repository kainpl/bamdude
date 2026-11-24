/**
 * The lines table shows an order's work and opens the doors that change it: the row's
 * menu (configuration, edit, reorder, delete) and the expanded parts. What it must
 * never get wrong: the rows come in `sort_order`, every figure is the server's, a
 * reorder is the TWO patches that swap two neighbours — and a HALF-applied swap
 * refetches, so the operator never looks at an order the server no longer holds.
 * Editing a line is `LineEditDialog`'s (its own test file).
 *
 * ⚠️ The fixture lists line 11 BEFORE line 10 on purpose. Their `sort_order` values say
 * otherwise, so a component that dropped its `.sort(...)` and rendered the array would
 * fail the first test instead of passing it by coincidence.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, fireEvent, waitFor, within } from '@testing-library/react';
import { useEffect } from 'react';
import { useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query';
import { render } from '../../utils';
import { strayZeroTextNodes } from '../../domHelpers';
import { api } from '../../../api/client';
import type { Order, ProjectLine } from '../../../api/client';
import { OrderLinesTable } from '../../../components/projects/OrderLinesTable';
import { makeLine, makeOrder } from '../../fixtures/orderDetail';
import { PART_IMAGE_REFS_KEY } from '../../../utils/queryInvalidation';

/**
 * A stand-in for `OrderPage`'s own `['project', id]` query. Invalidation is only
 * observable as a REFETCH, and only while something is watching the key.
 */
function OrderProbe({ id }: { id: number }) {
  useQuery({ queryKey: ['project', id], queryFn: () => api.getOrder(id) });
  return null;
}

/** The same stand-in for the shelf — `['product-stock', id]`, the key `useProductStock` owns. */
function StockProbe({ id, onFetch }: { id: number; onFetch: () => void }) {
  useQuery({
    queryKey: ['product-stock', id],
    queryFn: async () => {
      onFetch();
      return null;
    },
  });
  return null;
}

const lid = makeLine({
  id: 11,
  product_id: 2,
  product_name: 'Lid',
  quantity: 4,
  material: null,
  sort_order: 1,
  units_printed: 4,
  from_stock_units: 0,
  from_finished: 0,
  from_kit_units: 0,
  covered_units: 4,
  progress: 1,
  prints_in_progress: 0,
  prints_queued: 0,
});
const flask = makeLine({
  id: 10,
  product_id: 1,
  product_name: 'Flask',
  quantity: 2,
  material: 'PETG',
  sort_order: 0,
  units_printed: 1,
  from_stock_units: 2,
  from_finished: 0,
  from_kit_units: 2,
  covered_units: 2,
  progress: 1,
  prints_in_progress: 0,
  prints_queued: 0,
  parts: [
    {
      part_id: 1,
      name: 'flask',
      qty_per_unit: 1,
      need: 2,
      usable: 1,
      in_progress: 0,
      remaining: 1,
      surplus: 0,
      variant: false,
      queued: 0,
      bankable: 0,
    },
  ],
});
const order: Order = makeOrder({ id: 1, status: 'active', lines: [lid, flask] });

function withLines(...lines: ProjectLine[]): Order {
  return makeOrder({ id: 1, status: 'active', lines });
}

function rowOf(lineId: number): HTMLElement {
  return document.querySelector(`tr[data-line="${lineId}"]`) as HTMLElement;
}

async function openMenu(product: string) {
  fireEvent.click(screen.getByRole('button', { name: `Line actions: ${product}` }));
  return screen.findByRole('menu');
}

describe('OrderLinesTable · the table (E4 B)', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('keeps the line quantity free of the duplicate calculator action', () => {
    render(<OrderLinesTable order={order} canEdit />);
    const row = screen.getByTestId('line-10-quantity').closest('td')!;
    expect(within(row).queryByRole('button')).not.toBeInTheDocument();
  });

  it('renders lines in sort order with six columns, the menu one named only for a screen reader', () => {
    render(<OrderLinesTable order={order} canEdit />);
    const rows = screen.getAllByRole('row').filter((r) => r.getAttribute('data-line'));
    expect(rows.map((r) => r.getAttribute('data-line'))).toEqual(['10', '11']);
    const headers = screen.getAllByRole('columnheader');
    expect(headers.map((th) => th.textContent)).toEqual([
      '',
      'Product / configuration',
      'Quantity',
      'Material / colour',
      'Coverage',
      'Actions',
    ]);
    expect(within(headers[5]).getByText('Actions')).toHaveClass('sr-only');
    expect(screen.queryByRole('columnheader', { name: 'Note' })).not.toBeInTheDocument();
  });

  it('draws the product cover when it has one and a package icon when it does not', () => {
    render(
      <OrderLinesTable
        order={withLines({ ...flask, product_has_cover: true }, { ...lid, product_has_cover: false })}
        canEdit
      />,
    );
    const cover = rowOf(10).querySelector('img') as HTMLImageElement;
    expect(cover.getAttribute('src')).toContain('/products/1/cover-image');
    expect(cover).toHaveAttribute('alt', '');
    expect(rowOf(11).querySelector('img')).toBeNull();
    expect(within(rowOf(11)).getByTestId('line-11-thumb').querySelector('svg')).not.toBeNull();
  });

  it('says a one-off product is one-off, and shows the SKU only when there is one', () => {
    render(
      <OrderLinesTable
        order={withLines({ ...flask, product_origin: 'adhoc_plate', product_sku: 'FL-01' }, lid)}
        canEdit
      />,
    );
    expect(within(rowOf(10)).getByText('one-off')).toBeInTheDocument();
    expect(within(rowOf(11)).queryByText('one-off')).not.toBeInTheDocument();
    expect(screen.getByTestId('line-10-config')).toHaveTextContent(/^FL-01 · standard configuration$/);
    expect(screen.getByTestId('line-11-config')).toHaveTextContent(/^standard configuration$/);
  });

  it('names a non-standard configuration and lists what a parts line wants', () => {
    const configured = {
      ...flask,
      configuration: {
        choices: [{ group_id: 1, group_name: 'Mount', option_id: 4, option_name: 'DIN', is_default: false }],
        changed_parts: [],
      },
    };
    const partsLine = makeLine({
      ...lid,
      mode: 'parts',
      parts: [
        { ...flask.parts[0], part_id: 1, name: 'flask', qty_per_unit: 2, need: 2 },
        { ...flask.parts[0], part_id: 2, name: 'cap', qty_per_unit: 4, need: 3 },
      ],
    });
    render(<OrderLinesTable order={withLines(configured, partsLine)} canEdit />);
    expect(screen.getByTestId('line-10-config')).toHaveTextContent('Mount: DIN');
    expect(screen.getByTestId('line-10-config').querySelector('[data-config-accent]')).not.toBeNull();
    expect(screen.getByTestId('line-11-config')).toHaveTextContent('parts only: flask × 2, cap × 3');
  });

  it('puts the note under the name, in quotes', () => {
    render(<OrderLinesTable order={withLines({ ...flask, note: 'for the kiosk' }, lid)} canEdit />);
    expect(screen.getByTestId('line-10-note')).toHaveTextContent('«for the kiosk»');
    expect(screen.queryByTestId('line-11-note')).not.toBeInTheDocument();
  });

  it('reads the quantity in pieces or parts, with what stock gave and what went out below it', () => {
    const moved = { ...flask, quantity: 6, from_finished: 2, from_kit_units: 1, held: 3, issued: 1 };
    const partsLine = makeLine({ ...lid, mode: 'parts', quantity: 6, from_finished: 0, from_kit_units: 0 });
    render(<OrderLinesTable order={withLines(moved, partsLine)} canEdit />);
    expect(screen.getByTestId('line-10-quantity')).toHaveTextContent(/^6 pcs$/);
    expect(screen.getByTestId('line-10-from-stock-shown')).toHaveTextContent('from stock: 2 ready · 1 kits');
    expect(screen.getByTestId('line-10-issued')).toHaveTextContent('on the shelf for the order 3 · issued 1');
    expect(screen.getByTestId('line-11-quantity')).toHaveTextContent(/^6 parts$/);
    expect(screen.queryByTestId('line-11-from-stock-shown')).not.toBeInTheDocument();
    expect(screen.queryByTestId('line-11-issued')).not.toBeInTheDocument();
    expect(strayZeroTextNodes(rowOf(11))).toHaveLength(0);
  });

  it('says only the ready units or only the kits when the other is zero', () => {
    render(<OrderLinesTable order={withLines({ ...flask, from_finished: 0, from_kit_units: 2 }, lid)} canEdit />);
    expect(screen.getByTestId('line-10-from-stock-shown')).toHaveTextContent(/^from stock: 2 kits$/);
  });

  it('names the material and the colour, or says either may be anything', () => {
    render(
      <OrderLinesTable
        order={withLines({ ...flask, material: 'PETG', color: '#FFFFFF' }, { ...lid, material: null, color: null })}
        canEdit
      />,
    );
    const set = screen.getByTestId('line-10-material');
    expect(set).toHaveTextContent('PETG');
    expect(set.querySelector('[data-swatch]')).not.toBeNull();
    expect(screen.getByTestId('line-10-color')).toHaveTextContent('White');
    expect(screen.getByTestId('line-11-material')).toHaveTextContent('any');
    expect(screen.getByTestId('line-11-color')).toHaveTextContent('colour — any');
    expect(screen.getByTestId('line-11-material').querySelector('[data-swatch]')).toBeNull();
  });

  it('shows a colour it cannot read as the stored text, without a swatch', () => {
    render(<OrderLinesTable order={withLines({ ...flask, color: 'Ocean' }, lid)} canEdit />);
    expect(screen.getByTestId('line-10-color')).toHaveTextContent(/^Ocean$/);
    expect(screen.getByTestId('line-10-material').querySelector('[data-swatch]')).toBeNull();
  });

  it('draws the server coverage as «covered / quantity» with a percentage and its sources', () => {
    const partial = { ...flask, quantity: 4, covered_units: 3, progress: 0.75, units_printed: 1, from_stock_units: 2 };
    const partsLine = makeLine({ ...lid, mode: 'parts', units_printed: 5, from_stock_units: 0 });
    render(<OrderLinesTable order={withLines(partial, partsLine)} canEdit />);
    const bar = screen.getByTestId('line-10-progress');
    expect(bar).toHaveTextContent('3 / 4');
    expect(bar).toHaveTextContent('75%');
    expect(screen.getByTestId('line-10-coverage-sources')).toHaveTextContent(/^1 printed · 2 from stock$/);
    expect(screen.getByTestId('line-11-coverage-sources')).toHaveTextContent(/^5 printed$/);
  });

  it('says what is printing and queued in words for a screen reader, and nothing for a line with none', () => {
    render(<OrderLinesTable order={withLines({ ...flask, prints_in_progress: 1, prints_queued: 2 }, lid)} canEdit />);
    const live = screen.getByTestId('line-10-live');
    expect(within(live).getByText('printing 1 print(s), queued 2 job(s)')).toHaveClass('sr-only');
    expect(live.querySelectorAll('svg')).toHaveLength(2);
    expect(screen.queryByTestId('line-11-live')).not.toBeInTheDocument();
    expect(strayZeroTextNodes(rowOf(11))).toHaveLength(0);
  });

  it('carries no action buttons in the row besides the expander and the menu', () => {
    render(<OrderLinesTable order={order} canEdit />);
    const buttons = within(rowOf(10)).getAllByRole('button');
    expect(buttons.map((b) => b.getAttribute('aria-label'))).toEqual(['Show parts', 'Line actions: Flask']);
  });

  it('gives a reader the expander and no menu', () => {
    render(<OrderLinesTable order={order} canEdit={false} />);
    expect(within(rowOf(10)).getAllByRole('button').map((b) => b.getAttribute('aria-label'))).toEqual(['Show parts']);
    fireEvent.click(screen.getByTestId('line-10-expand'));
    expect(screen.getByText('flask')).toBeInTheDocument();
    expect(screen.getByTestId('part-1-remaining').textContent).toBe('1');
  });

  it('lists the menu in order, with the ends of the list unable to move further', async () => {
    render(<OrderLinesTable order={order} canEdit />);
    const menu = await openMenu('Flask');
    const items = within(menu).getAllByRole('menuitem');
    expect(items.map((i) => i.textContent)).toEqual([
      'Part configuration…',
      'Add part to line…',
      'Edit line…',
      'Move up',
      'Move down',
      'Delete line',
    ]);
    expect(within(menu).getByRole('separator')).toBeInTheDocument();
    expect(within(menu).getByRole('menuitem', { name: 'Move up' })).toBeDisabled();
    expect(within(menu).getByRole('menuitem', { name: 'Move down' })).toBeEnabled();
    fireEvent.keyDown(menu, { key: 'Escape' });
    const last = await openMenu('Lid');
    expect(within(last).getByRole('menuitem', { name: 'Move up' })).toBeEnabled();
    expect(within(last).getByRole('menuitem', { name: 'Move down' })).toBeDisabled();
  });

  it('offers the configuration for a parts line too', async () => {
    render(<OrderLinesTable order={withLines(flask, makeLine({ ...lid, mode: 'parts' }))} canEdit />);
    const menu = await openMenu('Lid');
    expect(within(menu).getByRole('menuitem', { name: 'Part configuration…' })).toBeEnabled();
  });

  it('closes the configuration of a completed order and of a line whose stock moved, and says why', async () => {
    const { unmount } = render(<OrderLinesTable order={{ ...order, status: 'completed' }} canEdit />);
    let item = within(await openMenu('Flask')).getByRole('menuitem', { name: 'Part configuration…' });
    expect(item).toBeDisabled();
    expect(item).toHaveAttribute('title', 'The order is completed — reopen it to change a line’s configuration');
    unmount();

    render(<OrderLinesTable order={withLines({ ...flask, issued: 1 }, lid)} canEdit />);
    item = within(await openMenu('Flask')).getByRole('menuitem', { name: 'Part configuration…' });
    expect(item).toBeDisabled();
    expect(item).toHaveAttribute('title', "This line's stock has moved — take more from stock instead");
  });

  it('opens the configuration dialog and the edit dialog from the menu', async () => {
    vi.spyOn(api, 'getProduct').mockReturnValue(new Promise(() => {}));
    vi.spyOn(api, 'getProductStock').mockReturnValue(new Promise(() => {}));
    vi.spyOn(api, 'suggestStock').mockReturnValue(new Promise(() => {}));
    render(<OrderLinesTable order={order} canEdit />);
    fireEvent.click(within(await openMenu('Flask')).getByRole('menuitem', { name: 'Edit line…' }));
    expect(await screen.findByRole('dialog', { name: 'Edit line' })).toBeInTheDocument();
  });

  it('moving a line down swaps sort_order with its neighbour through two PATCHes', async () => {
    const patch = vi.spyOn(api, 'updateOrderLine').mockResolvedValue(order);
    render(<OrderLinesTable order={order} canEdit />);
    fireEvent.click(within(await openMenu('Flask')).getByRole('menuitem', { name: 'Move down' }));
    await waitFor(() => expect(patch).toHaveBeenCalledTimes(2));
    expect(patch).toHaveBeenCalledWith(1, 10, { sort_order: 1 });
    expect(patch).toHaveBeenCalledWith(1, 11, { sort_order: 0 });
  });

  it('refetches the order when only half of a swap lands', async () => {
    // The first PATCH lands and the second does not: `onSettled` is what makes the
    // operator see the truth.
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
    fireEvent.click(within(await openMenu('Flask')).getByRole('menuitem', { name: 'Move down' }));
    await waitFor(() => expect(patch).toHaveBeenCalledTimes(2));
    expect(await screen.findByText('Line not found')).toBeInTheDocument();
    await waitFor(() => expect(refetch).toHaveBeenCalledTimes(2));
  });

  it('asks before deleting, naming the line and what happens to its prints and stock', async () => {
    const shelf = vi.fn();
    const del = vi.spyOn(api, 'deleteOrderLine').mockResolvedValue(order);
    render(
      <>
        <StockProbe id={1} onFetch={shelf} />
        <OrderLinesTable order={order} canEdit />
      </>,
    );
    await waitFor(() => expect(shelf).toHaveBeenCalledTimes(1));
    fireEvent.click(within(await openMenu('Flask')).getByRole('menuitem', { name: 'Delete line' }));
    const dialog = await screen.findByRole('dialog', { name: 'Delete line «Flask»?' });
    expect(dialog).toHaveTextContent(
      'Prints of this line stay in the order without a line; its stock reservation goes back.',
    );
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));
    await waitFor(() => expect(del).toHaveBeenCalledWith(1, 10));
    // The delete released a reservation: the shelf the line took from refetches.
    await waitFor(() => expect(shelf).toHaveBeenCalledTimes(2));
  });

  it('drops the cached pictures of the deleted line’s product — its one-off parts free their ids (spec procurement-stock-images A16)', async () => {
    vi.spyOn(api, 'deleteOrderLine').mockResolvedValue(order);
    const seen = { client: null as QueryClient | null };
    function Seed() {
      const qc = useQueryClient();
      useEffect(() => {
        seen.client = qc;
        // The test client collects an unwatched query at once (`gcTime: 0`): only the refresh may drop these.
        qc.setQueryDefaults([PART_IMAGE_REFS_KEY], { gcTime: Infinity });
        qc.setQueryData([PART_IMAGE_REFS_KEY, 50], { part_id: 50, product_id: 1, image: null });
        qc.setQueryData([PART_IMAGE_REFS_KEY, 51], { part_id: 51, product_id: 2, image: null });
      }, [qc]);
      return null;
    }
    render(
      <>
        <Seed />
        <OrderLinesTable order={order} canEdit />
      </>,
    );
    fireEvent.click(within(await openMenu('Flask')).getByRole('menuitem', { name: 'Delete line' }));
    fireEvent.click(within(await screen.findByRole('dialog', { name: 'Delete line «Flask»?' })).getByRole('button', { name: 'Delete' }));
    await waitFor(() => expect(seen.client!.getQueryState([PART_IMAGE_REFS_KEY, 50])).toBeUndefined());
    expect(seen.client!.getQueryData([PART_IMAGE_REFS_KEY, 51])).toEqual({ part_id: 51, product_id: 2, image: null });
  });

  it('keeps a refused delete in its dialog, in the server’s words (E13 E02)', async () => {
    vi.spyOn(api, 'deleteOrderLine').mockRejectedValue(new Error('The line holds issued units'));
    render(<OrderLinesTable order={order} canEdit />);
    fireEvent.click(within(await openMenu('Flask')).getByRole('menuitem', { name: 'Delete line' }));
    const dialog = await screen.findByRole('dialog', { name: 'Delete line «Flask»?' });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('The line holds issued units');
  });

  it('says how to start when there are no lines', () => {
    render(<OrderLinesTable order={withLines()} canEdit />);
    expect(screen.getByText('No lines yet — add a product or parts from the catalog.')).toBeInTheDocument();
  });

  it('no longer offers a print of its own — the plan block owns that', async () => {
    render(<OrderLinesTable order={order} canEdit />);
    expect(await screen.findByTestId('line-10-expand')).toBeInTheDocument();
    expect(screen.queryByTestId('line-10-print')).not.toBeInTheDocument();
  });

  it('opens «Add to order» for this order instead of an add-line row', async () => {
    vi.spyOn(api, 'getProductsPaged').mockResolvedValue({
      items: [],
      meta: { total: 0, current_page: 1, per_page: 24, last_page: 1 },
      categories: [],
      uncategorized: 0,
      all_categories: 0,
      catalog_total: 0,
    });
    vi.spyOn(api, 'getProductCategories').mockResolvedValue([]);
    vi.spyOn(api, 'getProductFacets').mockResolvedValue({ materials: [], colors: [], models: [] });
    render(<OrderLinesTable order={order} canEdit />);
    fireEvent.click(screen.getByRole('button', { name: 'Add to order' }));
    expect(await screen.findByRole('dialog', { name: 'Add to order' })).toBeInTheDocument();
    expect(screen.queryByLabelText('Order')).not.toBeInTheDocument();
  });
  it('delegates whole-order and exact-line shortcuts without requiring line-edit permission', async () => {
    const queue = vi.fn();
    const synthetic = makeOrder({ lines: [
      makeLine({ id: 41, product_id: 7, product_name: 'Product A', sort_order: 0 }),
      makeLine({ id: 42, product_id: 7, product_name: 'Product A', sort_order: 1 }),
    ] });
    render(<OrderLinesTable order={synthetic} canEdit={false} onQueuePlan={queue} />);
    fireEvent.click(screen.getByRole('button', { name: 'Whole order to auto queue…' }));
    expect(queue).toHaveBeenLastCalledWith();
    const row = document.querySelector('[data-line="42"]')!;
    fireEvent.click(within(row as HTMLElement).getByRole('button', { name: 'Line actions: Product A' }));
    const menu = await screen.findByRole('menu');
    expect(within(menu).queryByRole('menuitem', { name: 'Edit line…' })).not.toBeInTheDocument();
    fireEvent.click(within(menu).getByRole('menuitem', { name: 'This line’s plan to auto queue…' }));
    expect(queue).toHaveBeenLastCalledWith(42);
  });

  it('offers no queue shortcuts on a closed order', () => {
    render(<OrderLinesTable order={makeOrder({ status: 'completed' })} canEdit={false} onQueuePlan={vi.fn()} />);
    expect(screen.queryByRole('button', { name: 'Whole order to auto queue…' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Line actions:/ })).not.toBeInTheDocument();
  });

});
