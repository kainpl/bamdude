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
import { act, screen, fireEvent, waitFor } from '@testing-library/react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import type { QueryClient } from '@tanstack/react-query';
import { useEffect } from 'react';
import { render } from '../../utils';
import { api, ApiError, STOCK_MOVEMENT_LIMIT, STOCK_NOTE_TOKENS, STOCK_REASONS } from '../../../api/client';
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

/** The test's handle on the wrapper's own client, so an invalidation can be
 *  driven from outside the component under test. */
function CaptureClient({ onReady }: { onReady: (qc: QueryClient) => void }) {
  const qc = useQueryClient();
  useEffect(() => {
    onReady(qc);
  }, [qc, onReady]);
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

    expect(await screen.findByTestId('stock-error')).toHaveTextContent(/could not load the stock/i);
    expect(screen.queryByTestId('stock-no-counted-parts')).not.toBeInTheDocument();
    expect(screen.queryByText(/nothing has moved yet/i)).not.toBeInTheDocument();
  });

  it('keeps the shelf on screen when a background refetch fails', async () => {
    // ⚠️ Data before status (finding I1) — the rule the order, product and
    // customer pages already follow. A proxy hiccup on a REFETCH must not blank
    // a section somebody is reading; the stale balances plus one toast are the
    // honest answer, an empty section is not.
    const get = vi
      .spyOn(api, 'getProductStock')
      .mockResolvedValueOnce(stock)
      .mockRejectedValue(new ApiError('Bad gateway', 502));
    let client: QueryClient | null = null;

    render(
      <>
        <CaptureClient onReady={(qc) => (client = qc)} />
        <ProductStock productId={5} canEdit />
      </>,
    );
    expect(await screen.findByTestId('stock-kits')).toHaveTextContent('3 kits');

    await act(async () => {
      await client!.invalidateQueries({ queryKey: ['product-stock', 5] });
    });

    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
    expect(screen.getByTestId('stock-kits')).toHaveTextContent('3 kits');
    expect(screen.getByTestId('stock-balance-1')).toHaveTextContent('5');
    expect(screen.queryByTestId('stock-error')).not.toBeInTheDocument();
    // …and the query is the one `appQueryClient` reports on, so the silence is
    // not silent. The flag lives on the hook, which is the only declaration of
    // this key.
    expect(client!.getQueryCache().find({ queryKey: ['product-stock', 5] })?.meta?.refreshToast).toBe(true);
  });

  it('says the product counts no printed parts rather than showing an empty table', async () => {
    // ⚠️ `balances` is empty only when the product COUNTS nothing — every
    // counted part comes back, with a 0 where nothing has moved. So this
    // sentence is about the product's composition, never about an empty shelf.
    vi.spyOn(api, 'getProductStock').mockResolvedValue({ balances: [], kits_available: 0, movements: [] });

    render(<ProductStock productId={5} canEdit />);

    expect(await screen.findByTestId('stock-no-counted-parts')).toBeInTheDocument();
    // A SETTLED empty success is the only state that may say this.
    expect(screen.getByText(/nothing has moved yet/i)).toBeInTheDocument();
    expect(screen.queryByTestId('stock-kits')).not.toBeInTheDocument();
    // Nothing to correct, so nothing offering to.
    expect(screen.queryByRole('button', { name: /adjust/i })).not.toBeInTheDocument();
  });

  it('shows a shelf that is merely empty as zeros under a zero-kit headline', async () => {
    // The other half of finding M2: the parts exist and there are none of them,
    // which is a table of zeros — not "this product has no counted parts".
    vi.spyOn(api, 'getProductStock').mockResolvedValue({
      kits_available: 0,
      balances: [
        { part_id: 1, name: 'Lid', qty_per_unit: 1, balance: 0 },
        { part_id: 2, name: 'Flask', qty_per_unit: 2, balance: 0 },
      ],
      movements: [],
    });

    render(<ProductStock productId={5} canEdit />);

    expect(await screen.findByTestId('stock-kits')).toHaveTextContent('0 kits');
    expect(screen.getByTestId('stock-balance-1')).toHaveTextContent('0');
    expect(screen.getByTestId('stock-balance-2')).toHaveTextContent('0');
    expect(screen.queryByTestId('stock-no-counted-parts')).not.toBeInTheDocument();
  });

  it('says so when the ledger it shows is only the last page of it', async () => {
    // ⚠️ Finding M3. A full page of exactly the limit is indistinguishable from
    // a complete history, so the operator reads the oldest row as the first
    // movement there ever was.
    const many = Array.from({ length: STOCK_MOVEMENT_LIMIT }, (_, i) => ({ ...stock.movements[0], id: 1000 + i }));
    vi.spyOn(api, 'getProductStock').mockResolvedValue({ ...stock, movements: many });

    render(<ProductStock productId={5} canEdit />);

    expect(await screen.findByTestId('stock-movements-truncated')).toHaveTextContent(
      `Showing the last ${STOCK_MOVEMENT_LIMIT} movements`,
    );
  });

  it('does not claim a truncated ledger when the page is not full', async () => {
    vi.spyOn(api, 'getProductStock').mockResolvedValue(stock);

    render(<ProductStock productId={5} canEdit />);

    await screen.findByTestId('stock-kits');
    expect(screen.queryByTestId('stock-movements-truncated')).not.toBeInTheDocument();
  });

  it('the adjust dialog answers a keyboard like every other overlay', async () => {
    // Finding M1: the role, the name, the focus and Escape as one unit — see
    // `useDialogFocus`. Without them it is an anonymous `<div>` a screen reader
    // never announces, opening at the top of the page behind.
    vi.spyOn(api, 'getProductStock').mockResolvedValue(stock);

    render(<ProductStock productId={5} canEdit />);
    fireEvent.click(await screen.findByRole('button', { name: /adjust/i }));

    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(dialog).toHaveAccessibleName('Adjust the stock');
    await waitFor(() => expect(document.activeElement).toBe(dialog));

    fireEvent.keyDown(document, { key: 'Escape' });

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('refuses to submit a correction that says nothing or moves nothing', async () => {
    // The three ways this form is not a movement: no reason given (the note is
    // the whole value of a `manual` row), a delta of zero (the ledger never
    // writes one) and a fractional delta (parts are whole things).
    const adjust = vi.spyOn(api, 'adjustProductStock').mockResolvedValue(stock.movements[2]);
    vi.spyOn(api, 'getProductStock').mockResolvedValue(stock);

    render(<ProductStock productId={5} canEdit />);
    fireEvent.click(await screen.findByRole('button', { name: /adjust/i }));
    const submit = screen.getByTestId('stock-adjust-submit');

    // A delta with no note.
    fireEvent.change(screen.getByLabelText(/change/i), { target: { value: '3' } });
    expect(submit).toBeDisabled();

    // A note with a zero delta.
    fireEvent.change(screen.getByLabelText(/why/i), { target: { value: 'counted the shelf' } });
    fireEvent.change(screen.getByLabelText(/change/i), { target: { value: '0' } });
    expect(submit).toBeDisabled();

    // Half a lid is not a lid.
    fireEvent.change(screen.getByLabelText(/change/i), { target: { value: '1.5' } });
    expect(submit).toBeDisabled();

    // Whitespace is not a reason.
    fireEvent.change(screen.getByLabelText(/change/i), { target: { value: '3' } });
    fireEvent.change(screen.getByLabelText(/why/i), { target: { value: '   ' } });
    expect(submit).toBeDisabled();

    expect(adjust).not.toHaveBeenCalled();
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
