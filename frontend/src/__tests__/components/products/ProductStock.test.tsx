/**
 * The product's free stock: the shelf, the kits it makes, and the ledger.
 *
 * Four things this section must never get wrong are covered here: the headline
 * is the SERVER's `kits_available` (not a division re-done on the client), a
 * movement is named from its own `part_name` even when its part has left
 * `balances`, the seven backend note TOKENS are translated while an operator's
 * own words are printed verbatim, and a refused correction says what the server
 * said rather than a house sentence about it.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, fireEvent, waitFor } from '@testing-library/react';
import { useQuery } from '@tanstack/react-query';
import { render } from '../../utils';
import { api, ApiError, STOCK_NOTE_TOKENS, STOCK_REASONS } from '../../../api/client';
import type { ProductStock as ProductStockWire } from '../../../api/client';
import { ProductStock } from '../../../components/products/ProductStock';
import { formatDateOnly } from '../../../utils/date';
import en from '../../../i18n/locales/en';
import uk from '../../../i18n/locales/uk';

/** Stand-ins for the product page's and the catalog's own queries, so an
 *  invalidation is observable as the REFETCH the operator actually gets. */
function ProductProbe({ onFetch }: { onFetch: () => void }) {
  useQuery({
    queryKey: ['product', 5],
    queryFn: async () => {
      onFetch();
      return null;
    },
  });
  return null;
}

function ProductsProbe({ onFetch }: { onFetch: () => void }) {
  useQuery({
    queryKey: ['products', 'probe'],
    queryFn: async () => {
      onFetch();
      return null;
    },
  });
  return null;
}

const stock: ProductStockWire = {
  kits_available: 3,
  balances: [
    { part_id: 1, name: 'Lid', qty_per_unit: 1, balance: 5 },
    { part_id: 2, name: 'Flask', qty_per_unit: 2, balance: 6 },
  ],
  movements: [
    {
      id: 9,
      part_id: 1,
      part_name: 'Lid',
      delta: 5,
      reason: 'surplus_banked',
      project_line_id: 4,
      order_id: 7,
      order_name: 'W02453',
      archive_id: null,
      note: null,
      created_by: 1,
      created_at: '2026-09-01T10:00:00Z',
    },
    {
      id: 8,
      part_id: 2,
      part_name: 'Flask',
      delta: 6,
      reason: 'unfiled_print',
      project_line_id: null,
      order_id: null,
      order_name: null,
      archive_id: 42,
      note: 'counted_by_operator',
      created_by: 1,
      created_at: '2026-08-31T10:00:00Z',
    },
    {
      // ⚠️ Part 3 is deliberately NOT in `balances` — it stopped counting after
      // this movement was written. Its history must still name it.
      id: 7,
      part_id: 3,
      part_name: 'Retired knob',
      delta: -2,
      reason: 'manual',
      project_line_id: null,
      order_id: null,
      order_name: null,
      archive_id: null,
      note: 'the shelf was two short',
      created_by: 1,
      created_at: '2026-08-30T10:00:00Z',
    },
  ],
};

describe('ProductStock', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    // The section reads the user's own `date_format`, like every other
    // date-showing screen. `iso` makes the assertion below zone-independent.
    vi.spyOn(api, 'getSettings').mockResolvedValue({ date_format: 'iso' } as never);
  });

  it('shows the server kits, every counted balance and the whole ledger', async () => {
    vi.spyOn(api, 'getProductStock').mockResolvedValue(stock);

    render(<ProductStock productId={5} canEdit />);

    // The headline is `kits_available` as sent — NOT `min(balance/qty)` redone
    // here, which would drift the moment a part stopped counting.
    expect(await screen.findByTestId('stock-kits')).toHaveTextContent('3 kits');
    expect(screen.getByTestId('stock-balance-1')).toHaveTextContent('5');
    expect(screen.getByTestId('stock-balance-2')).toHaveTextContent('6');

    // Signed, and pointing at the order that produced the surplus.
    const banked = screen.getByTestId('stock-movement-9');
    expect(banked).toHaveTextContent('+5');
    expect(banked).toHaveTextContent('Surplus banked');
    expect(banked.querySelector('a')).toHaveAttribute('href', '/projects/7');

    // An archive is TEXT, not a link: this app has no per-archive route.
    const unfiled = screen.getByTestId('stock-movement-8');
    expect(unfiled).toHaveTextContent('Print #42');
    expect(unfiled.querySelector('a')).toBeNull();
    // A backend note is a token, and reads as a sentence in the UI language.
    expect(unfiled).toHaveTextContent('counted by the operator');

    // A part that left `balances` is still named, from `part_name`.
    const manual = screen.getByTestId('stock-movement-7');
    expect(manual).toHaveTextContent('Retired knob');
    expect(manual).toHaveTextContent('−2');
    // The operator's own words are printed as they were typed.
    expect(manual).toHaveTextContent('the shelf was two short');
  });

  it('the adjust dialog posts the correction and refetches the shelf, the product and the catalog', async () => {
    const get = vi.spyOn(api, 'getProductStock').mockResolvedValue(stock);
    const adjust = vi.spyOn(api, 'adjustProductStock').mockResolvedValue(stock.movements[2]);
    const product = vi.fn();
    const products = vi.fn();

    render(
      <>
        <ProductProbe onFetch={product} />
        <ProductsProbe onFetch={products} />
        <ProductStock productId={5} canEdit />
      </>,
    );
    await waitFor(() => expect(get).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(product).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(products).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole('button', { name: /adjust/i }));
    fireEvent.change(screen.getByLabelText(/part/i), { target: { value: '2' } });
    fireEvent.change(screen.getByLabelText(/change/i), { target: { value: '-2' } });
    fireEvent.change(screen.getByLabelText(/why/i), { target: { value: 'counted the shelf' } });
    fireEvent.click(screen.getByTestId('stock-adjust-submit'));

    await waitFor(() =>
      expect(adjust).toHaveBeenCalledWith(5, { part_id: 2, delta: -2, note: 'counted the shelf' }),
    );
    // All three caches were dropped, which the watching observers turn into
    // real refetches — the shelf, the product's own `kits_available` and the
    // catalog card that shows it.
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(product).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(products).toHaveBeenCalledTimes(2));
  });

  it('reports a refused correction in the server own words', async () => {
    vi.spyOn(api, 'getProductStock').mockResolvedValue(stock);
    vi.spyOn(api, 'adjustProductStock').mockRejectedValue(
      new ApiError('Lid holds 5; stock never goes below 0', 409),
    );

    render(<ProductStock productId={5} canEdit />);
    fireEvent.click(await screen.findByRole('button', { name: /adjust/i }));
    fireEvent.change(screen.getByLabelText(/change/i), { target: { value: '-99' } });
    fireEvent.change(screen.getByLabelText(/why/i), { target: { value: 'miscount' } });
    fireEvent.click(screen.getByTestId('stock-adjust-submit'));

    // The ledger's own sentence, not a house paraphrase of it: only the server
    // knows which part it was and by how much.
    expect(await screen.findByText('Lid holds 5; stock never goes below 0')).toBeInTheDocument();
  });

  it('offers a reader no way to correct the shelf', async () => {
    vi.spyOn(api, 'getProductStock').mockResolvedValue(stock);

    render(<ProductStock productId={5} canEdit={false} />);

    expect(await screen.findByTestId('stock-kits')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /adjust/i })).not.toBeInTheDocument();
  });

  it('says the shelf is empty rather than showing a zero table', async () => {
    vi.spyOn(api, 'getProductStock').mockResolvedValue({ balances: [], kits_available: 0, movements: [] });

    render(<ProductStock productId={5} canEdit />);

    expect(await screen.findByText(/nothing on the shelf/i)).toBeInTheDocument();
    // A SETTLED empty success is the only state that may say this.
    expect(screen.getByText(/nothing has moved yet/i)).toBeInTheDocument();
    expect(screen.queryByTestId('stock-kits')).not.toBeInTheDocument();
    // Nothing to correct, so nothing offering to.
    expect(screen.queryByRole('button', { name: /adjust/i })).not.toBeInTheDocument();
  });

  it('dates a movement from the naive-UTC column, not from the platform parser', async () => {
    // ⚠️ `created_at` carries no `Z`, and `new Date(x)` reads a naive string as
    // LOCAL time — so the raw parser and the truth disagree by the whole offset
    // for the hours near midnight, which is where the operator's "did that
    // happen yesterday?" question always lands. `formatDateOnly` appends the Z.
    const naive = '2026-09-01T22:30:00';
    vi.spyOn(api, 'getProductStock').mockResolvedValue({
      ...stock,
      movements: [{ ...stock.movements[0], created_at: naive }],
    });

    render(<ProductStock productId={5} canEdit />);

    const row = await screen.findByTestId('stock-movement-9');
    const shown = row.querySelector('td')?.textContent ?? '';
    await waitFor(() => expect(shown).not.toBe(''));
    // The helper's own answer for that string — zone-independent by
    // construction — and the proof that it was read as UTC: the naive value and
    // the same instant spelled with a Z land on the same day.
    expect(row.querySelector('td')).toHaveTextContent(formatDateOnly(naive, undefined, 'iso'));
    expect(formatDateOnly(naive, undefined, 'iso')).toBe(formatDateOnly(`${naive}Z`, undefined, 'iso'));
  });

  it('a failed fetch says so, and never claims the shelf is empty', async () => {
    // ⚠️ "Nothing on the shelf yet" over a request that never came back tells
    // the operator their stock is gone. The section's whole job is to say what
    // is there, so it must be able to say that it does not know.
    vi.spyOn(api, 'getProductStock').mockRejectedValue(new ApiError('Product not found', 404));

    render(<ProductStock productId={5} canEdit />);

    expect(await screen.findByText(/could not load the stock/i)).toBeInTheDocument();
    expect(screen.queryByText(/nothing on the shelf/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/nothing has moved yet/i)).not.toBeInTheDocument();
  });

  /**
   * ⚠️ The two closed sets are COPIES of Python constants
   * (`backend/app/services/part_stock.py::NOTE_TOKENS` / `REASONS`) — a test
   * may import nothing from the backend, so the copy is pinned here instead.
   * A token added on that side without a label lands in the operator's history
   * as a raw identifier; a label added here for a token nobody writes is dead
   * weight. Both halves are checked in both locales.
   */
  it('every backend note token and reason has a label in both locales', () => {
    expect([...STOCK_NOTE_TOKENS]).toEqual([
      'order_cancelled',
      'line_deleted',
      'project_deleted',
      'reservation_rewritten',
      'filed_under_order',
      'unfiled_from_order',
      'counted_by_operator',
    ]);
    expect([...STOCK_REASONS]).toEqual([
      'surplus_banked',
      'unfiled_print',
      'reserved_for_order',
      'reservation_released',
      'manual',
    ]);

    for (const bundle of [en, uk]) {
      for (const token of STOCK_NOTE_TOKENS) {
        expect(bundle.stock.note[token]).toBeTruthy();
      }
      for (const reason of STOCK_REASONS) {
        expect(bundle.stock.reason[reason]).toBeTruthy();
      }
    }
  });
});
