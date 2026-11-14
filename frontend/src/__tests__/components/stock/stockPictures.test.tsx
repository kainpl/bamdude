/**
 * Spec part-thumbnails §12.2, plan E6 (task 45): the stock names its parts with pictures — a product's
 * expanded row of free parts, a position's free parts, the assembly dialog — and the stock page's journal
 * shows the cell the product tab shares with it (plan E5, D11).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, within } from '@testing-library/react';
import { Route, Routes } from 'react-router';
import { render } from '../../utils';
import { api } from '../../../api/client';
import type { PartImageRef, StockItemDetail, StockJournalRow, StockListItem } from '../../../api/client';
import { AssembleDialog } from '../../../components/stock/AssembleDialog';
import { StockJournal } from '../../../components/stock/StockJournal';
import { StockProductsTable } from '../../../components/stock/StockProductsTable';
import { StockItemPage } from '../../../pages/stock/StockItemPage';
import { STOCK_ROW_DEFAULTS } from '../../wireDefaults';
import { pipeDetail, pipeItem, pipeProduct } from './stockFixtures';

const ready = (v: string): PartImageRef => ({ kind: 'render', status: 'ready', v });
const src = (id: number, v: string) => `/api/v1/product-parts/${id}/image?v=${v}&size=sm`;

const detail: StockItemDetail = {
  ...pipeDetail,
  parts: [
    { part_id: 11, name: 'flask', per: 1, on_shelf: 4, image: ready('f11') },
    { part_id: 12, name: 'tail', per: 2, on_shelf: 3, image: null },
  ],
};

afterEach(() => {
  window.history.pushState({}, '', '/');
});

describe('a product’s free parts', () => {
  it('the expanded row shows each counted part with its picture', () => {
    const lamp: StockListItem = {
      ...STOCK_ROW_DEFAULTS,
      id: 1,
      name: 'Lamp',
      sku: 'LMP-1',
      is_active: true,
      origin: 'catalog',
      kits_available: 3,
      reserved_kits: 0,
      parts_on_shelf: 5,
      parts: [{ part_id: 11, name: 'lid', qty_per_unit: 1, balance: 5, variants: [], image: ready('l11') }],
      reservations: [],
    };
    render(
      <StockProductsTable
        products={[lamp]}
        canEdit
        onAdjust={vi.fn()}
        onAssemble={vi.fn()}
        sort="kits-desc"
        onSortChange={vi.fn()}
        footer={<div />}
      />,
    );
    fireEvent.click(within(screen.getByTestId('stock-row-1')).getByRole('button', { name: /show parts/i }));
    expect(within(screen.getByTestId('stock-details-1')).getByTestId('part-thumb-img')).toHaveAttribute('src', src(11, 'l11'));
  });
});

describe('a position’s page', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    window.history.pushState({}, '', '/stock/5');
    vi.spyOn(api, 'getStockItem').mockResolvedValue(detail);
    vi.spyOn(api, 'getStockJournal').mockResolvedValue({
      items: [],
      next_cursor: null,
      meta: { total: 0, current_page: 1, per_page: 24, last_page: 1 },
    });
    vi.spyOn(api, 'getSettings').mockResolvedValue({ date_format: 'system' } as never);
  });

  it('each free part of the configuration shows its picture beside «part (× per)»', async () => {
    render(
      <Routes>
        <Route path="/stock/:id" element={<StockItemPage />} />
      </Routes>,
    );
    const card = await screen.findByTestId('item-parts');
    const flask = within(card).getByText('flask (× 1)').parentElement as HTMLElement;
    expect(within(flask).getByTestId('part-thumb-img')).toHaveAttribute('src', src(11, 'f11'));
    expect(within(card).getByTestId('item-shelf-11')).toHaveTextContent('4');
  });
});

describe('the assembly dialog', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, 'assembleStock').mockResolvedValue(pipeItem);
    vi.spyOn(api, 'getStockItem').mockResolvedValue(detail);
    vi.spyOn(api, 'getStockCatalog').mockResolvedValue([pipeProduct] as never);
    vi.spyOn(api, 'getStockProduct').mockResolvedValue(pipeProduct as never);
  });

  it('each part the kit takes shows its picture', async () => {
    render(<AssembleDialog item={pipeItem} onClose={() => {}} />);
    const row = await screen.findByTestId('assemble-part-11');
    expect(within(row).getByTestId('part-thumb-img')).toHaveAttribute('src', src(11, 'f11'));
  });

  it('the name lines up with the row’s figures: the row is top-aligned, so is the name beside its picture (final review)', async () => {
    render(<AssembleDialog item={pipeItem} onClose={() => {}} />);
    const row = await screen.findByTestId('assemble-part-11');
    expect(row).toHaveClass('align-top');
    expect(within(row).getByTestId('part-thumb').parentElement).toHaveClass('items-start');
  });

  it('opened for a product, the lookup’s parts show their pictures too (final review)', async () => {
    vi.spyOn(api, 'lookupStockItem').mockResolvedValue({
      item: pipeItem,
      configuration: pipeItem.configuration,
      can_assemble: 1,
      parts: [
        { part_id: 11, name: 'flask', per: 1, on_shelf: 4, image: ready('k11') },
        { part_id: 12, name: 'tail', per: 2, on_shelf: 6, image: null },
      ],
    });
    render(<AssembleDialog productId={1} onClose={() => {}} />);
    const row = await screen.findByTestId('assemble-part-11');
    expect(within(row).getByTestId('part-thumb-img')).toHaveAttribute('src', src(11, 'k11'));
  });
});

describe('the stock page’s journal', () => {
  // Green from the start: the cell is the product tab's (plan E5, D11). This pins that the stock page
  // inherits it — taking PartThumb out of `JournalWhat` turns it red.
  it('a parts row shows its part’s picture', async () => {
    const shelf: StockJournalRow = {
      book: 'parts',
      id: 8,
      created_at: '2026-09-27T09:00:00',
      product_id: 1,
      product_name: 'Pipe',
      item: { id: 5, code: 'SK-0005', configuration: { choices: [], changed_parts: [] } },
      part_name: 'flask',
      part_id: 11,
      image: ready('f11'),
      kind: 'manual',
      delta: 3,
      delta_on_hand: 0,
      delta_reserved: 0,
      note: 'counted_by_operator',
      customer: null,
      project: null,
      user: null,
    };
    vi.restoreAllMocks();
    window.history.pushState({}, '', '/stock?tab=journal');
    vi.spyOn(api, 'getStockJournal').mockResolvedValue({
      items: [shelf],
      next_cursor: null,
      meta: { total: 1, current_page: 1, per_page: 24, last_page: 1 },
    });
    vi.spyOn(api, 'getStockJournalProducts').mockResolvedValue([]);
    vi.spyOn(api, 'getSettings').mockResolvedValue({ date_format: 'system', time_format: '24h' } as never);
    render(<StockJournal />);
    const row = await screen.findByTestId('journal-row-parts-8');
    expect(within(row).getByTestId('part-thumb-img')).toHaveAttribute('src', '/api/v1/product-parts/11/image?v=f11&size=sm');
  });
});
