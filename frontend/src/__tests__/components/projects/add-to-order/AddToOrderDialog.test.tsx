import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { render } from '../../../utils';
import { api, ApiError } from '../../../../api/client';
import { AddToOrderDialog } from '../../../../components/projects/add-to-order/AddToOrderDialog';
import { enabledButton, filesPage, lamp, libraryFile, pageOf, part, partsPage, pipe, pipeDetail, plate, suggestion } from './fixtures';

const orders = {
  items: [
    { id: 5, code: 'OR-0005', name: 'Flasks for Acme', status: 'active' },
    { id: 6, code: 'OR-0006', name: 'Spares', status: 'active' },
  ],
  meta: { total: 2, current_page: 1, per_page: 20, last_page: 1 },
  totals: { active: 2, completed: 0, cancelled: 0, all: 2, stages: {} },
};

/** The order the dialog is opened from (WS-13 E5: it names it in the subtitle). */
const ORDER = { id: 5, code: 'OR-0005', name: 'Flasks for Acme', active: true };

describe('AddToOrderDialog', () => {
  let add: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, 'getProductsPaged').mockResolvedValue(pageOf([pipe, lamp], 2));
    vi.spyOn(api, 'getProduct').mockResolvedValue(pipeDetail as never);
    vi.spyOn(api, 'getProductCategories').mockResolvedValue([]);
    vi.spyOn(api, 'getProductFacets').mockResolvedValue({ materials: [], colors: [], models: [] });
    vi.spyOn(api, 'getProductParts').mockResolvedValue({ items: [], meta: { total: 0, current_page: 1, per_page: 24, last_page: 1 } });
    vi.spyOn(api, 'getLibraryFilesPaged').mockResolvedValue({ items: [], meta: { total: 0, current_page: 1, per_page: 24, last_page: 1 } } as never);
    vi.spyOn(api, 'suggestStock').mockImplementation(async (items) => ({
      items: items.map((item) =>
        item.product_id === 1
          ? suggestion({ from_finished: 2, from_kits: 3, to_print: Math.max(0, item.quantity - 5) })
          : suggestion({ product_id: 2, finished_free: 0, kits_free: 1, from_finished: 0, from_kits: 1, to_print: item.quantity - 1 }),
      ),
    }));
    add = vi.spyOn(api, 'addOrderLines').mockImplementation(async (_order, lines) => ({
      order: { id: 5 } as never,
      results: lines.map((_line, i) => ({ line_id: 40 + i, asked_finished: 0, got_finished: 0, asked_kits: 0, got_kits: 0 })),
    }));
  });

  async function tick(id: number) {
    const row = await screen.findByTestId(`add-product-${id}`);
    fireEvent.click(within(row).getByRole('checkbox'));
    return row;
  }

  it('three tabs switch without losing the picks', async () => {
    render(<AddToOrderDialog order={ORDER} onClose={() => {}} />);
    await tick(1);
    fireEvent.click(screen.getByRole('tab', { name: 'Parts of a product' }));
    fireEvent.click(screen.getByRole('tab', { name: 'One-off from a file' }));
    fireEvent.click(screen.getByRole('tab', { name: 'Products' }));
    expect(within(await screen.findByTestId('add-product-1')).getByRole('checkbox')).toBeChecked();
  });

  it('says what is picked and adds every line in one request', async () => {
    const onClose = vi.fn();
    render(<AddToOrderDialog order={ORDER} onClose={onClose} />);
    const pipeRow = await tick(1);
    fireEvent.change(within(pipeRow).getByLabelText('Quantity'), { target: { value: '6' } });
    fireEvent.change(await within(pipeRow).findByLabelText('Tail'), { target: { value: '101' } });
    const lampRow = await tick(2);
    await waitFor(() => expect(within(lampRow).getByLabelText('Kits')).toHaveValue(1));
    fireEvent.change(within(lampRow).getByLabelText('Kits'), { target: { value: '0' } });
    fireEvent.change(within(lampRow).getByLabelText('Material'), { target: { value: 'PLA' } });
    expect(screen.getByText('Selected: 2 products · 7 pcs')).toBeInTheDocument();
    fireEvent.click(await enabledButton('Add lines (2)'));
    await waitFor(() =>
      expect(add).toHaveBeenCalledWith(5, [
        { kind: 'product', product_id: 1, quantity: 6, choices: { 10: 101 }, material: null, color: null, stock: 'auto' },
        {
          kind: 'product',
          product_id: 2,
          quantity: 1,
          choices: {},
          material: 'PLA',
          color: null,
          stock: { from_finished: 0, from_kits: 0 },
        },
      ]),
    );
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it('warns when the shelf gave less than was asked', async () => {
    add.mockResolvedValue({
      order: { id: 5, lines: [{ id: 40, product_name: 'Pipe' }] } as never,
      results: [{ line_id: 40, asked_finished: 2, got_finished: 1, asked_kits: 0, got_kits: 0 }],
    });
    render(<AddToOrderDialog order={ORDER} onClose={() => {}} />);
    const row = await tick(1);
    fireEvent.change(within(row).getByLabelText('Quantity'), { target: { value: '6' } });
    await waitFor(() => expect(within(row).getByLabelText('Ready units')).toHaveValue(2));
    fireEvent.click(await enabledButton('Add lines (1)'));
    expect(await screen.findByText(/Pipe: ready 1 of 2/)).toBeInTheDocument();
  });

  it('a refusal is the server sentence, and the dialog stays', async () => {
    add.mockRejectedValue(new ApiError('Product not found', 404));
    const onClose = vi.fn();
    render(<AddToOrderDialog order={ORDER} onClose={onClose} />);
    await tick(1);
    fireEvent.click(await enabledButton('Add lines (1)'));
    expect(await screen.findByText('Product not found')).toBeInTheDocument();
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
  });

  it('from a product page: the product is ticked and an active order is chosen first', async () => {
    const getOrders = vi.spyOn(api, 'getOrdersPaged').mockResolvedValue(orders as never);
    const describeCombination = vi
      .spyOn(api, 'describeCombination')
      .mockResolvedValue({ available: true, reasons: [], options: [] });
    render(<AddToOrderDialog preselectProduct={{ id: 1, code: 'PR-0001' }} onClose={() => {}} />);
    expect(within(await screen.findByTestId('add-product-1')).getByRole('checkbox')).toBeChecked();
    // Final review I4: the product is found, not merely ticked somewhere off the page.
    expect(api.getProductsPaged).toHaveBeenLastCalledWith({ page: 1, per_page: 24, active: true, q: 'PR-0001' });
    expect(screen.getByLabelText('Product, SKU, category, material or part name…')).toHaveValue('PR-0001');
    expect(getOrders).toHaveBeenLastCalledWith({ status: 'active', page: 1, per_page: 20 });
    const submit = screen.getByRole('button', { name: 'Add lines (1)' });
    // The product's combination has answered: what keeps «Add» off now is the missing order alone.
    await waitFor(() => expect(describeCombination).toHaveBeenCalledWith(1, {}));
    await waitFor(() => expect(screen.queryByText('Checking the products’ combinations…')).not.toBeInTheDocument());
    expect(submit).toBeDisabled();
    fireEvent.change(screen.getByLabelText('Order'), { target: { value: '6' } });
    expect(submit).toBeEnabled();
    fireEvent.click(submit);
    await waitFor(() => expect(add).toHaveBeenCalledWith(6, expect.any(Array)));
  });

  it('from a product page whose catalog page never loads: the pick is asked at once, «Add» is not held', async () => {
    // Review M-h: the product page says whether its product has groups, so the dialog does
    // not wait on «Checking…» for a list that failed — the list says so with its own retry.
    vi.spyOn(api, 'getProductsPaged').mockRejectedValue(new Error('offline'));
    vi.spyOn(api, 'getOrdersPaged').mockResolvedValue(orders as never);
    const describeCombination = vi
      .spyOn(api, 'describeCombination')
      .mockResolvedValue({ available: true, reasons: [], options: [] });
    render(
      <AddToOrderDialog preselectProduct={{ id: 1, code: 'PR-0001', name: 'Pipe', hasGroups: true }} onClose={() => {}} />,
    );
    await waitFor(() => expect(describeCombination).toHaveBeenCalledWith(1, {}));
    expect(await screen.findByText('Could not load the products')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText('Checking the products’ combinations…')).not.toBeInTheDocument());
    await screen.findByRole('option', { name: /OR-0006/ });
    fireEvent.change(screen.getByLabelText('Order'), { target: { value: '6' } });
    const submit = screen.getByRole('button', { name: 'Add lines (1)' });
    await waitFor(() => expect(submit).toBeEnabled());
    fireEvent.click(submit);
    await waitFor(() => expect(add).toHaveBeenCalledWith(6, [expect.objectContaining({ product_id: 1 })]));
  });

  it('from a product without groups: nothing is asked, even before its page is listed', async () => {
    vi.spyOn(api, 'getProductsPaged').mockImplementation(() => new Promise(() => {}));
    vi.spyOn(api, 'getOrdersPaged').mockResolvedValue(orders as never);
    const describeCombination = vi.spyOn(api, 'describeCombination');
    render(
      <AddToOrderDialog preselectProduct={{ id: 2, code: 'PR-0002', name: 'Lamp', hasGroups: false }} onClose={() => {}} />,
    );
    await screen.findByRole('option', { name: /OR-0006/ });
    fireEvent.change(screen.getByLabelText('Order'), { target: { value: '6' } });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Add lines (1)' })).toBeEnabled());
    expect(screen.queryByText('Checking the products’ combinations…')).not.toBeInTheDocument();
    expect(describeCombination).not.toHaveBeenCalled();
  });
  it('three kinds of lines, picked on three tabs, go in one batch', async () => {
    vi.spyOn(api, 'getProductParts').mockResolvedValue(partsPage([part({})]));
    vi.spyOn(api, 'getLibraryFilesPaged').mockResolvedValue(filesPage([libraryFile(31, 'flask.gcode.3mf')]) as never);
    vi.spyOn(api, 'getLibraryFilePlates').mockResolvedValue({ file_id: 31, filename: 'flask.gcode.3mf', plates: [plate(1)], is_multi_plate: false });
    render(<AddToOrderDialog order={ORDER} onClose={() => {}} />);
    await tick(1);
    fireEvent.click(screen.getByRole('tab', { name: 'Parts of a product' }));
    fireEvent.click(within(await screen.findByTestId('add-part-11')).getByRole('checkbox'));
    fireEvent.click(screen.getByRole('tab', { name: 'One-off from a file' }));
    fireEvent.click(await screen.findByRole('button', { name: /^flask\.gcode\.3mf/ }));
    fireEvent.click(await screen.findByRole('radio', { name: /Plate 1/ }));
    // WS-13 E5 B03: one line over the whole selection — the first part in its own form,
    // the next ones continue it («…· parts: N · M pcs · one-off from plate …»).
    expect(screen.getByText('Selected: 1 product · 1 pcs · parts: 1 · 1 pcs · one-off from plate 1 × 1')).toBeInTheDocument();
    fireEvent.click(await enabledButton('Add lines (3)'));
    await waitFor(() =>
      expect(add).toHaveBeenCalledWith(5, [
        expect.objectContaining({ kind: 'product', product_id: 1 }),
        { kind: 'parts', product_id: 1, part_counts: { 11: 1 } },
        { kind: 'plate', library_file_id: 31, plate_index: 1, copies: 1 },
      ]),
    );
  });

  it('warns an auto row that got less than the proposal it showed', async () => {
    // Final review I1: for «auto» the server picks again at confirmation, so its
    // asked equals its got — the warning compares with what the dialog showed.
    add.mockResolvedValue({
      order: { id: 5, lines: [{ id: 40, product_name: 'Pipe' }] } as never,
      results: [{ line_id: 40, asked_finished: 0, got_finished: 0, asked_kits: 5, got_kits: 5 }],
    });
    render(<AddToOrderDialog order={ORDER} onClose={() => {}} />);
    const row = await tick(1);
    fireEvent.change(within(row).getByLabelText('Quantity'), { target: { value: '6' } });
    await waitFor(() => expect(within(row).getByLabelText('Ready units')).toHaveValue(2));
    fireEvent.click(await enabledButton('Add lines (1)'));
    expect(await screen.findByText(/Pipe: ready 0 of 2/)).toBeInTheDocument();
    expect(screen.queryByText(/kits 5 of/)).not.toBeInTheDocument();
  });
});
