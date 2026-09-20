/**
 * The Stock tab over mocked API calls. `render` from `__tests__/utils` wraps
 * the providers and a BrowserRouter; the URL is set with pushState first, the
 * way `ProductsPage.test.tsx` does.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { render } from '../../utils';
import { api } from '../../../api/client';
import type { StockMovementsPage, StockSummary } from '../../../api/client';
import { StockPage } from '../../../pages/stock/StockPage';

const summary: StockSummary = {
  products: [
    {
      id: 1, name: 'Lamp', is_active: true, origin: 'catalog', kits_available: 3,
      parts: [
        { part_id: 11, name: 'lid', qty_per_unit: 1, balance: 5 },
        { part_id: 12, name: 'base', qty_per_unit: 1, balance: 3 },
      ],
      reservations: [{ line_id: 7, order_id: 42, order_name: 'Order for Ivan', kits: 2 }],
    },
    { id: 2, name: 'Old vase', is_active: false, origin: 'catalog', kits_available: 0, parts: [{ part_id: 21, name: 'body', qty_per_unit: 1, balance: 0 }], reservations: [] },
  ],
};

const page1: StockMovementsPage = {
  items: [
    { id: 9, part_id: 11, part_name: 'lid', delta: 2, reason: 'surplus_banked', project_line_id: 7, order_id: 42, order_name: 'Order for Ivan', archive_id: null, note: null, created_by: null, created_at: '2026-09-10T10:00:00', product_id: 1, product_name: 'Lamp' },
    { id: 8, part_id: 12, part_name: 'base', delta: -1, reason: 'manual', project_line_id: null, order_id: null, order_name: null, archive_id: null, note: 'dropped it', created_by: null, created_at: '2026-09-09T10:00:00', product_id: 1, product_name: 'Lamp' },
  ],
  next_before_id: 8,
};
const page2: StockMovementsPage = {
  items: [
    { id: 3, part_id: 21, part_name: 'body', delta: 1, reason: 'unfiled_print', project_line_id: null, order_id: null, order_name: null, archive_id: 500, note: null, created_by: null, created_at: '2026-09-01T10:00:00', product_id: 2, product_name: 'Old vase' },
  ],
  next_before_id: null,
};

afterEach(() => {
  window.history.pushState({}, '', '/');
});

describe('StockPage', () => {
  let getSummary: ReturnType<typeof vi.spyOn>;
  let getMovements: ReturnType<typeof vi.spyOn>;
  let getProducts: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    vi.restoreAllMocks();
    window.history.pushState({}, '', '/stock');
    getSummary = vi.spyOn(api, 'getStockSummary').mockResolvedValue(summary);
    getMovements = vi.spyOn(api, 'getStockMovements').mockImplementation(async (params) =>
      params?.before_id ? page2 : page1,
    );
    vi.spyOn(api, 'getSettings').mockResolvedValue({ date_format: 'system' } as never);
    getProducts = vi.spyOn(api, 'getProducts').mockResolvedValue([
      { id: 1, name: 'Lamp', is_active: true, origin: 'catalog', origin_file_id: null, origin_plate_index: null, cover_image_filename: null, has_cover: false, parts_count: 2, plates_count: 1, lines_count: 0, kits_available: 3 },
      { id: 2, name: 'Old vase', is_active: false, origin: 'catalog', origin_file_id: null, origin_plate_index: null, cover_image_filename: null, has_cover: false, parts_count: 1, plates_count: 1, lines_count: 0, kits_available: 0 },
    ]);
  });

  it('lists products with their kits and marks one that is out of the catalog', async () => {
    render(<StockPage />);
    const lamp = await screen.findByTestId('stock-row-1');
    expect(within(lamp).getByText('Lamp')).toBeInTheDocument();
    expect(within(lamp).getByTestId('stock-kits-1')).toHaveTextContent('3');
    expect(within(screen.getByTestId('stock-row-2')).getByText(/not in the catalog/i)).toBeInTheDocument();
    expect(getSummary).toHaveBeenLastCalledWith({});
  });

  it('re-queries when the toggle and the search change', async () => {
    render(<StockPage />);
    await screen.findByTestId('stock-row-1');
    fireEvent.click(screen.getByLabelText(/only with stock/i));
    await waitFor(() => expect(getSummary).toHaveBeenLastCalledWith({ with_stock: false }));
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'lamp' } });
    await waitFor(() => expect(getSummary).toHaveBeenLastCalledWith({ with_stock: false, q: 'lamp' }));
  });

  it('expands a row into its parts and the orders holding its kits', async () => {
    render(<StockPage />);
    const lamp = await screen.findByTestId('stock-row-1');
    fireEvent.click(within(lamp).getByRole('button', { name: /show parts/i }));
    const details = await screen.findByTestId('stock-details-1');
    expect(within(details).getByTestId('stock-balance-11')).toHaveTextContent('5');
    const link = within(details).getByRole('link', { name: 'Order for Ivan' });
    expect(link).toHaveAttribute('href', '/projects/42');
    expect(within(details).getByText('2 kits')).toBeInTheDocument();
  });

  it('opens the adjust dialog from a row', async () => {
    render(<StockPage />);
    const lamp = await screen.findByTestId('stock-row-1');
    fireEvent.click(within(lamp).getByRole('button', { name: /adjust/i }));
    expect(await screen.findByRole('dialog')).toBeInTheDocument();
    expect(screen.getByTestId('stock-adjust-submit')).toBeInTheDocument();
  });

  it('renders the journal and loads the older page with the cursor', async () => {
    render(<StockPage />);
    expect(await screen.findByTestId('stock-movement-9')).toBeInTheDocument();
    expect(getMovements).toHaveBeenLastCalledWith({ before_id: null, limit: 50 });
    fireEvent.click(screen.getByRole('button', { name: /show older/i }));
    expect(await screen.findByTestId('stock-movement-3')).toBeInTheDocument();
    expect(getMovements).toHaveBeenLastCalledWith({ before_id: 8, limit: 50 });
    expect(await screen.findByText(/whole ledger/i)).toBeInTheDocument();
  });

  it('re-queries the journal when a reason is picked', async () => {
    render(<StockPage />);
    await screen.findByTestId('stock-movement-9');
    getMovements.mockResolvedValueOnce({ items: [], next_before_id: null });
    fireEvent.change(screen.getByLabelText(/^reason$/i), { target: { value: 'manual' } });
    await waitFor(() =>
      expect(getMovements).toHaveBeenLastCalledWith({ reason: 'manual', before_id: null, limit: 50 }),
    );
    expect(await screen.findByText('No movement matches these filters.')).toBeInTheDocument();
  });

  it('shows the unfiltered empty state when the ledger has nothing yet', async () => {
    getMovements.mockResolvedValue({ items: [], next_before_id: null });
    render(<StockPage />);
    expect(await screen.findByText('Nothing has moved yet.')).toBeInTheDocument();
  });

  it('re-queries the journal when a product is picked, from the catalog rather than the (filtered) summary', async () => {
    render(<StockPage />);
    await screen.findByTestId('stock-movement-9');
    // The catalog list backing the filter is asked for WITHOUT an origin
    // filter — a one-off product can hold stock and history too, and neither
    // the summary nor the journal filters by origin.
    expect(getProducts).toHaveBeenCalledWith({ include_adhoc: true });
    fireEvent.change(screen.getByLabelText(/^product$/i), { target: { value: '2' } });
    await waitFor(() =>
      expect(getMovements).toHaveBeenLastCalledWith({ product_id: 2, before_id: null, limit: 50 }),
    );
  });
});
