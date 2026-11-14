/**
 * WS-13 E5 — the «Add to order» frame and the order choice (vault
 * 60-specs/workshop-ui-parity-e05-add-to-order-config, B01–B06, G02).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { render } from '../../../utils';
import { api, ApiError } from '../../../../api/client';
import type { BatchLine } from '../../../../api/client';
import { AddToOrderDialog } from '../../../../components/projects/add-to-order/AddToOrderDialog';
import { enabledButton, filesPage, lamp, libraryFile, pageOf, part, partsPage, pipe, pipeDetail, plate, suggestion } from './fixtures';

const navigate = vi.fn();
vi.mock('react-router', async (importOriginal) => ({
  ...(await importOriginal<typeof import('react-router')>()),
  useNavigate: () => navigate,
}));

const ORDER = { id: 5, code: 'OR-0005', name: 'Flasks for Acme', active: true };

const ordersPage = (items: object[], total = items.length) => ({
  items,
  meta: { total, current_page: 1, per_page: 20, last_page: Math.max(1, Math.ceil(total / 20)) },
  totals: { active: total, completed: 0, cancelled: 0, all: total, stages: {} },
});

async function tick(id: number) {
  const row = await screen.findByTestId(`add-product-${id}`);
  fireEvent.click(within(row).getByRole('checkbox'));
  return row;
}

describe('AddToOrderDialog — the frame (WS-13 E5)', () => {
  let add: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    vi.restoreAllMocks();
    navigate.mockReset();
    vi.spyOn(api, 'getProductsPaged').mockResolvedValue(pageOf([pipe, lamp], 2));
    vi.spyOn(api, 'getProduct').mockResolvedValue(pipeDetail as never);
    vi.spyOn(api, 'getProductCategories').mockResolvedValue([]);
    vi.spyOn(api, 'getProductFacets').mockResolvedValue({ materials: [], colors: [], models: [] });
    vi.spyOn(api, 'getProductParts').mockResolvedValue(partsPage([part({})]));
    vi.spyOn(api, 'getLibraryFilesPaged').mockResolvedValue(filesPage([]) as never);
    vi.spyOn(api, 'suggestStock').mockImplementation(async (items) => ({
      items: items.map((item) => suggestion({ product_id: item.product_id })),
    }));
    add = vi.spyOn(api, 'addOrderLines').mockImplementation(async (_order, lines) => ({
      order: { id: 5 } as never,
      results: lines.map((_line, i) => ({ line_id: 40 + i, asked_finished: 0, got_finished: 0, asked_kits: 0, got_kits: 0 })),
    }));
  });

  it('puts the first focus in the active tab’s search box, or in the order search from a product (B06)', async () => {
    vi.spyOn(api, 'getOrdersPaged').mockResolvedValue(ordersPage([]) as never);
    const fromOrder = render(<AddToOrderDialog order={ORDER} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByLabelText('Product, SKU, category, material or part name…')).toHaveFocus());
    fromOrder.unmount();

    render(<AddToOrderDialog preselectProduct={{ id: 1, code: 'PR-0001' }} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByLabelText('Find an order…')).toHaveFocus());
  });

  it('shows placeholder rows while a tab is first read (C07, D07, E03)', async () => {
    const never = () => new Promise<never>(() => {});
    vi.spyOn(api, 'getProductsPaged').mockImplementation(never);
    vi.spyOn(api, 'getProductParts').mockImplementation(never);
    vi.spyOn(api, 'getLibraryFilesPaged').mockImplementation(never);
    vi.spyOn(api, 'getLibraryFolders').mockResolvedValue([] as never);
    render(<AddToOrderDialog order={ORDER} onClose={() => {}} />);
    for (const tab of ['Products', 'Parts of a product', 'One-off from a file']) {
      fireEvent.click(screen.getByRole('tab', { name: tab }));
      const busy = await within(screen.getByRole('tabpanel')).findByRole('status');
      expect(busy).toHaveAttribute('aria-busy', 'true');
      expect(within(busy).getByText('Loading...')).toBeInTheDocument();
    }
  });

  it('is a Workshop xl dialog that names the order it adds to (B01)', async () => {
    render(<AddToOrderDialog order={ORDER} onClose={() => {}} />);
    const dialog = await screen.findByRole('dialog', { name: 'Add to order' });
    expect(dialog).toHaveStyle({ maxWidth: 'min(1560px, 95vw)' });
    expect(dialog).toHaveAccessibleDescription('OR-0005 · Flasks for Acme');
  });

  it('mounts a tab on its first visit and keeps its search when you come back (B02)', async () => {
    render(<AddToOrderDialog order={ORDER} onClose={() => {}} />);
    await screen.findByTestId('add-product-1');
    expect(api.getProductParts).not.toHaveBeenCalled();
    expect(api.getLibraryFilesPaged).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('tab', { name: 'Parts of a product' }));
    const search = await screen.findByLabelText('Part, product, SKU or file…');
    fireEvent.change(search, { target: { value: 'tail' } });
    fireEvent.click(screen.getByRole('tab', { name: 'Products' }));
    fireEvent.click(screen.getByRole('tab', { name: 'Parts of a product' }));
    expect(screen.getByLabelText('Part, product, SKU or file…')).toHaveValue('tail');
    // Every tab points at a panel that exists, visited or not.
    for (const tab of screen.getAllByRole('tab')) {
      expect(document.getElementById(tab.getAttribute('aria-controls') ?? '')).not.toBeNull();
    }
    expect(api.getLibraryFilesPaged).not.toHaveBeenCalled();
  });

  it('sums up the whole selection on one line, every kind picked (B03)', async () => {
    vi.spyOn(api, 'getLibraryFilesPaged').mockResolvedValue(filesPage([libraryFile(31, 'flask.gcode.3mf')]) as never);
    vi.spyOn(api, 'getLibraryFilePlates').mockResolvedValue({ file_id: 31, filename: 'flask.gcode.3mf', plates: [plate(2)], is_multi_plate: false });
    render(<AddToOrderDialog order={ORDER} onClose={() => {}} />);
    expect(await screen.findByText('Nothing selected')).toBeInTheDocument();
    await tick(1);
    fireEvent.click(screen.getByRole('tab', { name: 'Parts of a product' }));
    fireEvent.click(within(await screen.findByTestId('add-part-11')).getByRole('checkbox'));
    fireEvent.click(screen.getByRole('tab', { name: 'One-off from a file' }));
    fireEvent.click(await screen.findByRole('button', { name: /flask\.gcode\.3mf/ }));
    fireEvent.click(await screen.findByRole('radio', { name: /Plate 2/ }));
    expect(
      screen.getByText('Selected: 1 product · 1 pcs · parts: 1 · 1 pcs · one-off from plate 2 × 1'),
    ).toBeInTheDocument();
  });

  it('while the batch is in flight nothing can be changed, closed or sent twice (B04)', async () => {
    add.mockImplementation(() => new Promise(() => {}));
    const onClose = vi.fn();
    render(<AddToOrderDialog order={ORDER} onClose={onClose} />);
    const row = await tick(1);
    fireEvent.click(await enabledButton('Add lines (1)'));
    const adding = await screen.findByRole('button', { name: 'Adding…' });
    expect(adding).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled();
    expect(within(row).getByLabelText('Quantity')).toBeDisabled();
    fireEvent.keyDown(window, { key: 'Escape' });
    fireEvent.click(adding);
    expect(onClose).not.toHaveBeenCalled();
    expect(add).toHaveBeenCalledTimes(1);
  });

  it('says a refusal inside the dialog and hands the controls back with the selection (B04)', async () => {
    add.mockRejectedValue(new ApiError('Product not found', 404));
    const onClose = vi.fn();
    render(<AddToOrderDialog order={ORDER} onClose={onClose} />);
    const row = await tick(1);
    fireEvent.click(await enabledButton('Add lines (1)'));
    expect(await screen.findByRole('alert')).toHaveTextContent('Product not found');
    expect(within(row).getByRole('checkbox')).toBeChecked();
    expect(screen.getByRole('button', { name: 'Add lines (1)' })).toBeEnabled();
    expect(onClose).not.toHaveBeenCalled();
  });

  it('from a product, goes to the order the batch was added to (B04, K4)', async () => {
    vi.spyOn(api, 'getOrdersPaged').mockResolvedValue(
      ordersPage([
        { id: 6, code: 'OR-0006', name: 'Spares', customer_name: 'Acme' },
        { id: 7, code: 'OR-0007', name: 'Lamps', customer_name: null },
      ]) as never,
    );
    let release: (v: unknown) => void = () => {};
    add.mockImplementation(
      (_order: number, lines: BatchLine[]) =>
        new Promise((resolve) => {
          release = () =>
            resolve({ order: { id: 6 } as never, results: lines.map((_l: BatchLine, i: number) => ({ line_id: 40 + i, asked_finished: 0, got_finished: 0, asked_kits: 0, got_kits: 0 })) });
        }),
    );
    const onClose = vi.fn();
    render(<AddToOrderDialog preselectProduct={{ id: 1, code: 'PR-0001' }} onClose={onClose} />);
    await screen.findByRole('option', { name: /OR-0006/ });
    fireEvent.change(screen.getByLabelText('Order'), { target: { value: '6' } });
    fireEvent.click(await enabledButton('Add lines (1)'));
    await waitFor(() => expect(add).toHaveBeenCalled());
    release(null);
    await waitFor(() => expect(navigate).toHaveBeenCalledWith('/projects/6'));
    expect(add).toHaveBeenCalledWith(6, expect.any(Array));
    expect(onClose).toHaveBeenCalled();
  });

  it('from an order, closes and stays (B04)', async () => {
    const onClose = vi.fn();
    render(<AddToOrderDialog order={ORDER} onClose={onClose} />);
    await tick(1);
    fireEvent.click(await enabledButton('Add lines (1)'));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(navigate).not.toHaveBeenCalled();
  });

  it('says an inactive order takes nothing from stock (B05)', async () => {
    render(<AddToOrderDialog order={{ ...ORDER, active: false }} onClose={() => {}} />);
    expect(
      await screen.findByText('The order is not active — nothing is taken from stock, everything goes to print.'),
    ).toBeInTheDocument();
  });
});

describe('AddToOrderDialog — choosing the order (WS-13 E5 G02)', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, 'getProductsPaged').mockResolvedValue(pageOf([pipe], 1));
    vi.spyOn(api, 'getProduct').mockResolvedValue(pipeDetail as never);
    vi.spyOn(api, 'getProductCategories').mockResolvedValue([]);
    vi.spyOn(api, 'getProductFacets').mockResolvedValue({ materials: [], colors: [], models: [] });
    vi.spyOn(api, 'suggestStock').mockImplementation(async (items) => ({
      items: items.map((item) => suggestion({ product_id: item.product_id })),
    }));
  });

  const open = () => render(<AddToOrderDialog preselectProduct={{ id: 1, code: 'PR-0001' }} onClose={() => {}} />);

  it('asks for an order in the subtitle, names each with its customer, and shows the chosen one', async () => {
    vi.spyOn(api, 'getOrdersPaged').mockResolvedValue(
      ordersPage([
        { id: 6, code: 'OR-0006', name: 'Spares', customer_name: 'Acme' },
        { id: 7, code: 'OR-0007', name: 'Lamps', customer_name: null },
      ]) as never,
    );
    open();
    const dialog = await screen.findByRole('dialog', { name: 'Add to order' });
    expect(dialog).toHaveAccessibleDescription('Choose an order');
    const select = await screen.findByLabelText('Order');
    await waitFor(() =>
      expect([...select.querySelectorAll('option')].map((o) => o.textContent)).toEqual([
        'Choose an active order',
        'OR-0006 · Spares · Acme',
        'OR-0007 · Lamps · no customer',
      ]),
    );
    fireEvent.change(select, { target: { value: '7' } });
    expect(dialog).toHaveAccessibleDescription('OR-0007 · Lamps');
  });

  it('says it is loading, and never offers a choice meanwhile', async () => {
    vi.spyOn(api, 'getOrdersPaged').mockImplementation(() => new Promise(() => {}));
    open();
    expect(await screen.findByText('Loading orders…')).toBeInTheDocument();
    expect(screen.getByLabelText('Order')).toBeDisabled();
  });

  it('says a failed read and offers to retry', async () => {
    const get = vi.spyOn(api, 'getOrdersPaged').mockRejectedValue(new ApiError('boom', 500));
    open();
    expect(await screen.findByText('Could not load the orders')).toBeInTheDocument();
    get.mockResolvedValue(ordersPage([{ id: 6, code: 'OR-0006', name: 'Spares', customer_name: null }]) as never);
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(screen.queryByText('Could not load the orders')).not.toBeInTheDocument());
  });

  it('tells «no active orders» from «nothing found»', async () => {
    vi.spyOn(api, 'getOrdersPaged').mockResolvedValue(ordersPage([]) as never);
    open();
    expect(await screen.findByText('No active orders')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Find an order…'), { target: { value: 'zzz' } });
    expect(await screen.findByText('Nothing found')).toBeInTheDocument();
  });

  it('pages through when the server has more than one page (E13 D01a)', async () => {
    const items = Array.from({ length: 20 }, (_v, i) => ({ id: 10 + i, code: `OR-00${10 + i}`, name: `O${i}`, customer_name: null }));
    const get = vi.spyOn(api, 'getOrdersPaged').mockResolvedValue(ordersPage(items, 43) as never);
    open();
    expect(await screen.findByText('Page 1 of 3')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    await waitFor(() => expect(get).toHaveBeenLastCalledWith({ status: 'active', page: 2, per_page: 20 }));
  });
});
