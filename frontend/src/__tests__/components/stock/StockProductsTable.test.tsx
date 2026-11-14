/**
 * The free-parts table (WS-13 E12 D): the mockup's six columns — the expander, the product,
 * kits by configuration, parts on the shelf, the orders' reservations and the row's two
 * actions — and the expanded row's parts, where a counted part out of the kit still shows.
 */

import { describe, it, expect, vi } from 'vitest';
import { fireEvent, screen, within } from '@testing-library/react';
import { render } from '../../utils';
import { StockProductsTable } from '../../../components/stock/StockProductsTable';
import type { StockListItem } from '../../../api/client';
import { STOCK_ROW_DEFAULTS } from '../../wireDefaults';

const row: StockListItem = {
  ...STOCK_ROW_DEFAULTS,
  id: 1,
  name: 'Lamp',
  sku: 'LMP-1',
  is_active: true,
  origin: 'catalog',
  kits_available: 3,
  reserved_kits: 2,
  parts_on_shelf: 9,
  parts: [
    { part_id: 11, name: 'lid', qty_per_unit: 1, balance: 5, variants: [] },
    {
      part_id: 12,
      name: 'shade',
      qty_per_unit: 2,
      balance: 3,
      variants: [
        { group: 'Shade', options: ['Large', 'Huge'] },
        { group: 'Mount', options: ['wall'] },
      ],
    },
    { part_id: 13, name: 'spare clip', qty_per_unit: 0, balance: 1, variants: [] },
  ],
  reservations: [
    { line_id: 7, order_id: 42, order_code: 'OR-0042', order_name: 'Order for Ivan', kits: 2 },
  ],
};
/** Standard 0, option B 3 — the case «Assemble» exists for (R01). */
const pipe: StockListItem = {
  ...STOCK_ROW_DEFAULTS,
  id: 2,
  name: 'Pipe',
  is_active: true,
  origin: 'catalog',
  kits_available: 0,
  reserved_kits: 0,
  parts_on_shelf: 6,
  parts: [],
  reservations: [],
  kits_by_option: [
    { group_id: 10, group_name: 'Tail', option_id: 100, option_name: 'straight', is_default: true, kits: 0 },
    { group_id: 10, group_name: 'Tail', option_id: 101, option_name: 'angled', is_default: false, kits: 3 },
  ],
};
const oneOff: StockListItem = {
  ...STOCK_ROW_DEFAULTS,
  id: 3,
  name: 'Bracket for Ivan',
  is_active: false,
  origin: 'order',
  kits_available: 1,
  reserved_kits: 0,
  parts_on_shelf: 1,
  parts: [],
  reservations: [],
};

function renderTable(overrides: Partial<Parameters<typeof StockProductsTable>[0]> = {}) {
  const onAdjust = vi.fn();
  const onAssemble = vi.fn();
  const onSortChange = vi.fn();
  render(
    <StockProductsTable
      products={[row, pipe, oneOff]}
      canEdit
      onAdjust={onAdjust}
      onAssemble={onAssemble}
      sort="kits-desc"
      onSortChange={onSortChange}
      footer={<div data-testid="page-bar" />}
      {...overrides}
    />,
  );
  return { onAdjust, onAssemble, onSortChange };
}

const headers = () =>
  screen
    .getAllByRole('columnheader')
    .map((th) => (th.getAttribute('aria-label') ?? th.textContent ?? '').replace(/[▲▼]/g, '').trim());

describe('StockProductsTable', () => {
  it('keeps the page bar out of the horizontal scroll, so it does not slide away with a wide table', () => {
    renderTable();
    expect(screen.getByRole('region', { name: 'Free parts' })).toContainElement(screen.getAllByRole('table')[0]);
    expect(screen.getByRole('region', { name: 'Free parts' })).not.toContainElement(screen.getByTestId('page-bar'));
  });

  it("six columns in the mockup's order, sorting on the server", () => {
    const { onSortChange } = renderTable();
    // The expander's header is decoration (aria-hidden); the other five are named.
    expect(headers()).toEqual(['Product', 'Kits by configuration', 'Parts on the shelf', 'Reserved by orders', 'Actions']);
    fireEvent.click(screen.getByRole('button', { name: /^Parts on the shelf/ }));
    expect(onSortChange).toHaveBeenCalledWith('shelf-desc');
    fireEvent.click(screen.getByRole('button', { name: /^Reserved by orders/ }));
    expect(onSortChange).toHaveBeenCalledWith('reserved-desc');
  });

  it('the product links to its page, with its SKU and its marks', async () => {
    renderTable();
    const lamp = screen.getByTestId('stock-row-1');
    // A link into the catalog waits for the signed-in user's rights (WS-13 E13 O19).
    expect(await within(lamp).findByRole('link', { name: 'Lamp' })).toHaveAttribute('href', '/products/1');
    expect(within(lamp).getByText('LMP-1')).toHaveClass('font-mono');
    const bracket = screen.getByTestId('stock-row-3');
    expect(within(bracket).getByText('not in the catalog')).toBeInTheDocument();
    expect(within(bracket).getByText('one-off')).toBeInTheDocument();
  });

  it('kits are one line per option for a product with variants, the whole figure without — and the note says they do not add up', () => {
    renderTable();
    expect(screen.getByTestId('stock-kits-1')).toHaveTextContent('3');
    const kits = screen.getByTestId('stock-kits-2');
    // One line per option; the figure is bold inside its line.
    expect(Array.from(kits.querySelectorAll('small')).map((line) => line.textContent)).toEqual([
      'Tail straight: 0',
      'Tail angled: 3',
    ]);
    expect(screen.getByText('Each row is one option with the other groups standard; the numbers do not add up.')).toBeInTheDocument();
  });

  it('no product with variants on the page — no note', () => {
    renderTable({ products: [row] });
    expect(screen.queryByText(/the numbers do not add up/)).toBeNull();
  });

  it("parts on the shelf are the server's sum; each reservation is its order's code and kits, linked — or a dash", async () => {
    renderTable();
    expect(screen.getByTestId('stock-shelf-1')).toHaveTextContent('9');
    const reserved = screen.getByTestId('stock-reserved-1');
    expect(await within(reserved).findByRole('link', { name: 'OR-0042' })).toHaveAttribute('href', '/projects/42');
    expect(reserved).toHaveTextContent('OR-0042 · 2');
    expect(screen.getByTestId('stock-reserved-2')).toHaveTextContent('—');
  });

  it('«Assemble» opens for the product even when the standard makes nothing (R01)', async () => {
    const { onAssemble, onAdjust } = renderTable();
    // «Adjust» waits for the signed-in user's `stock:adjust` (WS-13 E13 O06).
    await within(screen.getByTestId('stock-row-2')).findByRole('button', { name: 'Adjust' });
    const row2 = screen.getByTestId('stock-row-2');
    const assemble = within(row2).getByRole('button', { name: 'Assemble' });
    expect(assemble).toBeEnabled();
    fireEvent.click(assemble);
    expect(onAssemble).toHaveBeenCalledWith(pipe);
    fireEvent.click(within(row2).getByRole('button', { name: 'Adjust' }));
    expect(onAdjust).toHaveBeenCalledWith(pipe);
  });

  it('a one-off product cannot be assembled, and says why on screen', () => {
    const { onAssemble } = renderTable();
    const bracket = screen.getByTestId('stock-row-3');
    const assemble = within(bracket).getByRole('button', { name: 'Assemble' });
    expect(assemble).toBeDisabled();
    expect(assemble).toHaveAccessibleDescription('A one-off product is not kept in finished stock');
    expect(within(bracket).getByText('A one-off product is not kept in finished stock')).toBeVisible();
    fireEvent.click(assemble);
    expect(onAssemble).not.toHaveBeenCalled();
  });

  it('a reader gets no actions', () => {
    renderTable({ canEdit: false });
    expect(screen.queryByRole('button', { name: 'Assemble' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Adjust' })).toBeNull();
  });

  it('the expanded row lists every counted part: the variant chip, «× N», and a zero as «Out of kit» with its shelf (R02)', () => {
    renderTable();
    fireEvent.click(within(screen.getByTestId('stock-row-1')).getByRole('button', { name: /show parts/i }));
    const details = screen.getByTestId('stock-details-1');
    const rows = within(details).getAllByRole('row').slice(1);
    expect(rows.map((r) => r.querySelector('td')?.textContent)).toEqual(['lid', 'shadevariant', 'spare clip']);
    // Bound in two groups: the chip names both, values by a comma (spec product-part-variant-combinations).
    expect(within(rows[1]).getByText('variant')).toHaveAttribute('title', 'Shade: Large, Huge · Mount: wall');
    expect(within(rows[0]).getByText('× 1')).toBeInTheDocument();
    expect(within(rows[1]).getByText('× 2')).toBeInTheDocument();
    expect(within(rows[2]).getByText('Out of kit')).toBeInTheDocument();
    expect(within(details).getByTestId('stock-balance-13')).toHaveTextContent('1');
    // The reservations moved into their column; the expanded row is the parts only.
    expect(within(details).queryByText('Order for Ivan')).toBeNull();
  });
});
