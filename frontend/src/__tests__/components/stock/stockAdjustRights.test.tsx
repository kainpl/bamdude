/**
 * Correcting the books is `stock:adjust` (WS-13 E13 O06, O19): a stocktake, a position's location
 * and minimum, a hand correction of the free parts and an order's write-off. A storekeeper who
 * moves goods but does not correct them is offered none of those doors — the server answers each
 * with 403 — and keeps every movement: receipt, assembly, reservation, issue.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, screen, within } from '@testing-library/react';
import { Route, Routes } from 'react-router';
import { render } from '../../utils';
import { api } from '../../../api/client';
import type { Permission, ProductStock as ProductStockWire, StockItem, StockListItem } from '../../../api/client';
import { FinishedGoodsTable } from '../../../components/stock/FinishedGoodsTable';
import { StockProductsTable } from '../../../components/stock/StockProductsTable';
import { ProductStock } from '../../../components/products/ProductStock';
import { FulfilmentDialog } from '../../../components/projects/fulfilment/FulfilmentDialog';
import { StockItemPage } from '../../../pages/stock/StockItemPage';
import { STOCK_ROW_DEFAULTS } from '../../wireDefaults';
import { pipeDetail } from './stockFixtures';

const auth = vi.hoisted(() => ({ granted: new Set<string>() }));
vi.mock('../../../contexts/AuthContext', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../../contexts/AuthContext')>();
  return {
    ...actual,
    useAuth: () => ({
      ...actual.useAuth(),
      hasPermission: (p: Permission) => auth.granted.has(p),
      hasAnyPermission: (...ps: Permission[]) => ps.some((p) => auth.granted.has(p)),
    }),
  };
});

const MOVER = ['stock:read', 'stock:move', 'orders:read', 'orders:update', 'products:read'];
const ADJUSTER = [...MOVER, 'stock:adjust'];
const ROLES: [string, string[], boolean][] = [
  ['a storekeeper without «adjust»', MOVER, false],
  ['a storekeeper with «adjust»', ADJUSTER, true],
];

const lamp: StockItem = {
  id: 3,
  code: 'SK-0003',
  product: { id: 1, name: 'Lamp', sku: 'LMP-1', has_cover: false },
  configuration: { choices: [], changed_parts: [] },
  location: 'A-3',
  on_hand: 4,
  reserved: 1,
  available: 3,
  min_qty: 5,
  below_min: true,
  short_by: 2,
  can_assemble: 2,
};

const partsRow: StockListItem = {
  ...STOCK_ROW_DEFAULTS,
  id: 1,
  name: 'Lamp',
  is_active: true,
  origin: 'catalog',
  kits_available: 3,
  reserved_kits: 0,
  parts_on_shelf: 5,
  parts: [{ part_id: 11, name: 'lid', qty_per_unit: 1, balance: 5, variants: [] }],
  reservations: [],
};

const shelf: ProductStockWire = {
  kits_by_option: [],
  kits_available: 3,
  balances: [{ part_id: 1, name: 'Lid', qty_per_unit: 1, balance: 5 }],
  movements: [],
};

beforeEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
});

describe('a storekeeper without the catalog’s read (WS-13 E13 O19)', () => {
  it('the free parts’ «Adjust» needs the catalog’s read too — its dialog reads the product’s shelf', () => {
    auth.granted = new Set(['stock:read', 'stock:move', 'stock:adjust']);
    render(
      <StockProductsTable products={[partsRow]} canEdit onAdjust={() => {}} onAssemble={() => {}} sort="kits-desc" onSortChange={() => {}} />,
    );
    expect(screen.queryByRole('button', { name: 'Adjust' })).toBeNull();
  });

  it('the position page offers no way into the product — no link and no dead «Open the product»', async () => {
    auth.granted = new Set(['stock:read', 'stock:move']);
    vi.spyOn(api, 'getStockItem').mockResolvedValue(pipeDetail);
    vi.spyOn(api, 'getStockJournal').mockResolvedValue({ items: [], next_cursor: null, meta: { total: 0, current_page: 1, per_page: 24, last_page: 1 } });
    window.history.pushState({}, '', '/stock/5');
    render(
      <Routes>
        <Route path="/stock/:id" element={<StockItemPage />} />
      </Routes>,
    );
    await screen.findByRole('heading', { level: 1, name: 'Pipe' });
    expect(document.querySelector('a[href^="/products/"]')).toBeNull();
    expect(screen.queryByText('Open the product')).toBeNull();
    expect(screen.queryByText(/^product card$/i)).toBeNull();
  });
});

describe.each(ROLES)('%s', (_who, permissions, adjusts) => {
  beforeEach(() => {
    auth.granted = new Set(permissions);
  });

  it('the finished goods’ row menu: every movement, and the corrections only with «adjust»', () => {
    render(<FinishedGoodsTable items={[lamp]} sort="product-asc" onSortChange={() => {}} canEdit onAction={() => {}} />);
    fireEvent.click(screen.getByTestId('finished-3-menu'));
    const items = within(screen.getByTestId('finished-3-menu-panel'))
      .getAllByRole('menuitem')
      .map((m) => m.textContent?.trim());
    expect(items).toEqual(expect.arrayContaining(['Receipt', 'Assemble from parts', 'Reserve', 'Issue', 'Open position']));
    expect(items.includes('Stocktake')).toBe(adjusts);
    expect(items.includes('Location and minimum')).toBe(adjusts);
  });

  it('the free parts’ row: «Assemble» for a mover, «Adjust» only with «adjust»', () => {
    render(
      <StockProductsTable products={[partsRow]} canEdit onAdjust={() => {}} onAssemble={() => {}} sort="kits-desc" onSortChange={() => {}} />,
    );
    expect(screen.getByRole('button', { name: 'Assemble' })).toBeInTheDocument();
    expect(screen.queryAllByRole('button', { name: 'Adjust' })).toHaveLength(adjusts ? 1 : 0);
  });

  it('a product’s free parts: «Adjust» only with «adjust»', async () => {
    vi.spyOn(api, 'getProductStock').mockResolvedValue(shelf);
    render(<ProductStock productId={5} productName="Flask kit" canEdit />);
    await screen.findByText('Lid');
    expect(screen.queryAllByRole('button', { name: 'Adjust' })).toHaveLength(adjusts ? 1 : 0);
  });

  it('the position page: «Stocktake» and «Location and minimum» only with «adjust»', async () => {
    vi.spyOn(api, 'getStockItem').mockResolvedValue(pipeDetail);
    vi.spyOn(api, 'getStockJournal').mockResolvedValue({ items: [], next_cursor: null, meta: { total: 0, current_page: 1, per_page: 24, last_page: 1 } });
    window.history.pushState({}, '', '/stock/5');
    render(
      <Routes>
        <Route path="/stock/:id" element={<StockItemPage />} />
      </Routes>,
    );
    await screen.findByRole('heading', { level: 1, name: 'Pipe' });
    const actions = screen.getByTestId('item-actions');
    expect(within(actions).getByRole('button', { name: 'Reserve' })).toBeInTheDocument();
    expect(within(actions).queryAllByRole('button', { name: 'Location and minimum' })).toHaveLength(adjusts ? 1 : 0);
    fireEvent.click(screen.getByTestId('item-menu'));
    const menu = within(screen.getByTestId('item-menu-panel')).getAllByRole('menuitem').map((m) => m.textContent?.trim());
    expect(menu.includes('Stocktake')).toBe(adjusts);
  });

  it('an order’s issue dialog: the write-off only with «adjust»', async () => {
    vi.spyOn(api, 'getFulfilment').mockResolvedValue({
      lines: [
        {
          line_id: 7,
          product_name: 'Pipe',
          mode: 'product',
          ordered: 10,
          from_finished: 2,
          kits_reserved: 0,
          can_assemble: 0,
          can_receive: 0,
          held: 2,
          issued: 0,
          written_off: 0,
          parts: [],
          configuration: null,
          stock_position: null,
        },
      ],
      ordered: 10,
      issued: 0,
      held: 2,
      fully_issued: false,
      can_assemble: 0,
      can_receive: 0,
      can_issue: 2,
      closes_to_stock: false,
      can_complete: false,
      recipient: { name: 'Ivan', phone: '+380501112233', delivery_method: 'Nova Poshta', delivery_details: 'Branch 5' },
    });
    vi.spyOn(api, 'getDeliveryMethods').mockResolvedValue([]);
    render(
      <FulfilmentDialog
        order={{ id: 5, code: 'OR-0005', name: 'Pipes', status: 'active', customer_name: 'ACME', bankable_surplus: 0, due_date: null }}
        onClose={() => {}}
      />,
    );
    await screen.findByText('Pipe');
    expect(screen.queryAllByRole('button', { name: 'Write off…' })).toHaveLength(adjusts ? 1 : 0);
  });
});
