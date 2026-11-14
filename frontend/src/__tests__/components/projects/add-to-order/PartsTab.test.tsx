import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { render } from '../../../utils';
import { api } from '../../../../api/client';
import { AddToOrderDialog } from '../../../../components/projects/add-to-order/AddToOrderDialog';
import { pageOf, part, partsPage } from './fixtures';


/** The order the dialog is opened from (WS-13 E5: the dialog names it). */
const ORDER = { id: 5, code: 'OR-0005', name: 'Flasks for Acme', active: true };

describe('the parts tab of «Add to order»', () => {
  let getParts: ReturnType<typeof vi.spyOn>;
  let add: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, 'getProductsPaged').mockResolvedValue(pageOf([], 0));
    vi.spyOn(api, 'getProductCategories').mockResolvedValue([]);
    vi.spyOn(api, 'getProductFacets').mockResolvedValue({ materials: [], colors: [], models: ['P1S', 'X1C'] });
    getParts = vi.spyOn(api, 'getProductParts').mockResolvedValue(
      partsPage([
        part({}),
        part({ part_id: 12, name: 'Elbow', variants: [{ group: 'Tail', options: ['angled'] }] }),
        part({ part_id: 21, name: 'Shade', product: { id: 2, code: 'PR-0002', name: 'Lamp', sku: null }, models: [] }),
      ]),
    );
    add = vi.spyOn(api, 'addOrderLines').mockResolvedValue({ order: { id: 5, lines: [] } as never, results: [] });
  });

  function open() {
    render(<AddToOrderDialog order={ORDER} onClose={() => {}} />);
    fireEvent.click(screen.getByRole('tab', { name: 'Parts of a product' }));
  }

  it('lists a server page of parts, searched and filtered by printer model', async () => {
    open();
    expect(await screen.findByTestId('add-part-11')).toBeInTheDocument();
    expect(getParts).toHaveBeenLastCalledWith({ page: 1, per_page: 24 });
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'elbow' } });
    await waitFor(() => expect(getParts).toHaveBeenLastCalledWith({ page: 1, per_page: 24, q: 'elbow' }));
    // The products tab stays mounted, hidden (WS-13 E5 B02) — its own filter is not this one.
    fireEvent.change(await within(screen.getByRole('tabpanel')).findByLabelText('Printer model'), { target: { value: 'X1C' } });
    await waitFor(() => expect(getParts).toHaveBeenLastCalledWith({ page: 1, per_page: 24, q: 'elbow', model: 'X1C' }));
  });

  it('names the option a part belongs to and the product it comes from', async () => {
    open();
    const elbow = await screen.findByTestId('add-part-12');
    // WS-13 E5 D03: the chip names the option; the group is its title.
    expect(within(elbow).getByText('angled')).toHaveAttribute('title', 'Tail: angled');
    // The SKU sits in its own monospace run inside that line (E5 D03).
    expect(within(elbow).getByText((_c, el) => el?.textContent === 'Pipe · PP-1 · PR-0001' && el.tagName === 'DIV')).toBeInTheDocument();
    expect(within(screen.getByTestId('add-part-21')).getByText('Lamp · PR-0002')).toBeInTheDocument();
  });

  it('parts of one product become one line', async () => {
    open();
    for (const id of [11, 12, 21]) fireEvent.click(within(await screen.findByTestId(`add-part-${id}`)).getByRole('checkbox'));
    fireEvent.change(within(screen.getByTestId('add-part-12')).getByLabelText('Order, pcs'), { target: { value: '3' } });
    expect(screen.getByText('Selected parts: 3 · 5 pcs')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Add parts' }));
    await waitFor(() =>
      expect(add).toHaveBeenCalledWith(5, [
        { kind: 'parts', product_id: 1, part_counts: { 11: 1, 12: 3 } },
        { kind: 'parts', product_id: 2, part_counts: { 21: 1 } },
      ]),
    );
  });

  it('orders at least one piece', async () => {
    open();
    const row = await screen.findByTestId('add-part-11');
    fireEvent.click(within(row).getByRole('checkbox'));
    fireEvent.change(within(row).getByLabelText('Order, pcs'), { target: { value: '0' } });
    // WS-13 E5 D06: the rule applies when the field is left — the count stays 1.
    fireEvent.blur(within(row).getByLabelText('Order, pcs'));
    expect(within(row).getByLabelText('Order, pcs')).toHaveValue(1);
  });
});
