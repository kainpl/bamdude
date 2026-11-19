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
import { act, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import type { QueryClient } from '@tanstack/react-query';
import { useEffect } from 'react';
import { render } from '../../utils';
import { api, ApiError, STOCK_NOTE_TOKENS, STOCK_REASONS } from '../../../api/client';
import type { ProductStock as ProductStockWire } from '../../../api/client';
import { ProductStock } from '../../../components/products/ProductStock';
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
  kits_by_option: [],
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

  it('shows the server kits and every counted balance — the movements are the tab’s journal', async () => {
    vi.spyOn(api, 'getProductStock').mockResolvedValue(stock);

    render(<ProductStock productId={5} productName="Flask kit" canEdit />);

    // The headline is `kits_available` as sent — NOT `min(balance/qty)` redone
    // here, which would drift the moment a part stopped counting.
    expect(await screen.findByTestId('stock-kits')).toHaveTextContent('3');
    expect(screen.getByTestId('stock-balance-1')).toHaveTextContent('5');
    expect(screen.getByTestId('stock-balance-2')).toHaveTextContent('6');
    // The ledger is the product page's journal now (WS-13 E9 F03), not a list here.
    expect(screen.queryByTestId('stock-movement-9')).not.toBeInTheDocument();
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
        <ProductStock productId={5} productName="Flask kit" canEdit />
      </>,
    );
    await waitFor(() => expect(get).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(product).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(products).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole('button', { name: /adjust/i }));
    // The dialog reads the shelf itself, a current read (WS-13 E10 R07) — one more fetch.
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
    fireEvent.change(screen.getByLabelText('Part'), { target: { value: '2' } });
    fireEvent.change(screen.getByLabelText(/change/i), { target: { value: '-2' } });
    fireEvent.change(screen.getByLabelText(/why/i), { target: { value: 'counted the shelf' } });
    fireEvent.click(screen.getByTestId('stock-adjust-submit'));

    await waitFor(() =>
      expect(adjust).toHaveBeenCalledWith(5, { part_id: 2, delta: -2, note: 'counted the shelf' }),
    );
    // All three caches were dropped, which the watching observers turn into
    // real refetches — the shelf, the product's own `kits_available` and the
    // catalog card that shows it.
    await waitFor(() => expect(get).toHaveBeenCalledTimes(3));
    await waitFor(() => expect(product).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(products).toHaveBeenCalledTimes(2));
  });

  it('reports a refused correction in the server own words', async () => {
    vi.spyOn(api, 'getProductStock').mockResolvedValue(stock);
    vi.spyOn(api, 'adjustProductStock').mockRejectedValue(
      new ApiError('Lid holds 5; stock never goes below 0', 409),
    );

    render(<ProductStock productId={5} productName="Flask kit" canEdit />);
    fireEvent.click(await screen.findByRole('button', { name: /adjust/i }));
    // Within the shelf as this dialog read it — the server's ledger is the one that refuses
    // (somebody took parts meanwhile); below the read the dialog does not send at all.
    fireEvent.change(screen.getByLabelText(/change/i), { target: { value: '-3' } });
    fireEvent.change(screen.getByLabelText(/why/i), { target: { value: 'miscount' } });
    fireEvent.click(screen.getByTestId('stock-adjust-submit'));

    // The ledger's own sentence, not a house paraphrase of it: only the server
    // knows which part it was and by how much.
    expect(await screen.findByText('Lid holds 5; stock never goes below 0')).toBeInTheDocument();
  });

  it('offers a reader no way to correct the shelf', async () => {
    vi.spyOn(api, 'getProductStock').mockResolvedValue(stock);

    render(<ProductStock productId={5} productName="Flask kit" canEdit={false} />);

    expect(await screen.findByTestId('stock-kits')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /adjust/i })).not.toBeInTheDocument();
  });

  it('a failed fetch says so, and never claims the shelf is empty', async () => {
    // ⚠️ "Nothing on the shelf yet" over a request that never came back tells
    // the operator their stock is gone. The section's whole job is to say what
    // is there, so it must be able to say that it does not know.
    vi.spyOn(api, 'getProductStock').mockRejectedValue(new ApiError('Product not found', 404));

    render(<ProductStock productId={5} productName="Flask kit" canEdit />);

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
        <ProductStock productId={5} productName="Flask kit" canEdit />
      </>,
    );
    expect(await screen.findByTestId('stock-kits')).toHaveTextContent('3');

    await act(async () => {
      await client!.invalidateQueries({ queryKey: ['product-stock', 5] });
    });

    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
    expect(screen.getByTestId('stock-kits')).toHaveTextContent('3');
    expect(screen.getByTestId('stock-balance-1')).toHaveTextContent('5');
    expect(screen.queryByTestId('stock-error')).not.toBeInTheDocument();
    // …and the query is the one `appQueryClient` reports on, so the silence is
    // not silent. The flag lives on the hook, which is the only declaration of
    // this key.
    expect(client!.getQueryCache().find({ queryKey: ['product-stock', 5] })?.meta?.refreshToast).toBe(true);
  });

  // WS-13 E9 Codex review V03 (F02, R03): a zero the last read did not confirm is not a
  // reason to shut «Assemble…» — the dialog's own lookup decides what can be assembled.
  it('a zero the shelf could not re-read does not keep «Assemble…» shut', async () => {
    const get = vi.spyOn(api, 'getProductStock').mockResolvedValue({ ...stock, kits_available: 0 });
    let client: QueryClient | null = null;
    render(
      <>
        <CaptureClient onReady={(qc) => (client = qc)} />
        <ProductStock productId={5} productName="Flask kit" canEdit />
      </>,
    );
    await screen.findByTestId('stock-kits');
    const assemble = () => screen.getByRole('button', { name: /Assemble/ });
    // A fresh zero, no variants: nothing to assemble.
    expect(assemble()).toBeDisabled();
    get.mockRejectedValue(new ApiError('Bad gateway', 502));
    await act(async () => {
      await client!.invalidateQueries({ queryKey: ['product-stock', 5] });
    });
    expect(client!.getQueryState(['product-stock', 5])?.status).toBe('error');
    // TanStack tells its observers a tick later than the cache: wait for the page to hear it.
    await waitFor(() => expect(assemble()).toBeEnabled());
    // The shelf answers again with a zero: shut again.
    get.mockResolvedValue({ ...stock, kits_available: 0 });
    await act(async () => {
      await client!.invalidateQueries({ queryKey: ['product-stock', 5] });
    });
    await waitFor(() => expect(assemble()).toBeDisabled());
  });

  it('says the product counts no printed parts rather than showing an empty table', async () => {
    // ⚠️ `balances` is empty only when the product COUNTS nothing — every
    // counted part comes back, with a 0 where nothing has moved. So this
    // sentence is about the product's composition, never about an empty shelf.
    vi.spyOn(api, 'getProductStock').mockResolvedValue({ kits_by_option: [], balances: [], kits_available: 0, movements: [] });

    render(<ProductStock productId={5} productName="Flask kit" canEdit />);

    expect(await screen.findByTestId('stock-no-counted-parts')).toBeInTheDocument();
    expect(screen.queryByTestId('stock-kits')).not.toBeInTheDocument();
    // Nothing to correct, so nothing offering to.
    expect(screen.queryByRole('button', { name: /adjust/i })).not.toBeInTheDocument();
  });

  it('shows a shelf that is merely empty as zeros under a zero-kit headline', async () => {
    // The other half of finding M2: the parts exist and there are none of them,
    // which is a table of zeros — not "this product has no counted parts".
    vi.spyOn(api, 'getProductStock').mockResolvedValue({ kits_by_option: [],
      kits_available: 0,
      balances: [
        { part_id: 1, name: 'Lid', qty_per_unit: 1, balance: 0 },
        { part_id: 2, name: 'Flask', qty_per_unit: 2, balance: 0 },
      ],
      movements: [],
    });

    render(<ProductStock productId={5} productName="Flask kit" canEdit />);

    expect(await screen.findByTestId('stock-kits')).toHaveTextContent('0');
    expect(screen.getByTestId('stock-balance-1')).toHaveTextContent('0');
    expect(screen.getByTestId('stock-balance-2')).toHaveTextContent('0');
    expect(screen.queryByTestId('stock-no-counted-parts')).not.toBeInTheDocument();
  });

  it('the adjust dialog answers a keyboard like every other overlay', async () => {
    // Finding M1: the role, the name, the focus and Escape as one unit — see
    // `useDialogFocus`. Without them it is an anonymous `<div>` a screen reader
    // never announces, opening at the top of the page behind.
    vi.spyOn(api, 'getProductStock').mockResolvedValue(stock);

    render(<ProductStock productId={5} productName="Flask kit" canEdit />);
    fireEvent.click(await screen.findByRole('button', { name: /adjust/i }));

    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(dialog).toHaveAccessibleName('Adjust free parts');
    // The cursor starts in the first field (WS-13 E10 J).
    await waitFor(() => expect(screen.getByLabelText('Part')).toHaveFocus());

    fireEvent.keyDown(document, { key: 'Escape' });

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('refuses to submit a correction that says nothing or moves nothing', async () => {
    // The three ways this form is not a movement: no reason given (the note is
    // the whole value of a `manual` row), a delta of zero (the ledger never
    // writes one) and a fractional delta (parts are whole things).
    const adjust = vi.spyOn(api, 'adjustProductStock').mockResolvedValue(stock.movements[2]);
    vi.spyOn(api, 'getProductStock').mockResolvedValue(stock);

    render(<ProductStock productId={5} productName="Flask kit" canEdit />);
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
      'defects_recorded',
      'assembled',
      'order_completed',
      'status_edited',
    ]);
    expect([...STOCK_REASONS]).toEqual([
      'surplus_banked',
      'unfiled_print',
      'reserved_for_order',
      'reserved_parts_for_order',
      'reservation_released',
      'manual',
      'assembled',
      'made_for_order',
      'held_for_order',
      'hold_released',
      'issued_for_order',
      'written_off_for_order',
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

describe('ProductStock · parts held for orders and parts out of the kit', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  // spec workshop-order-issue-followups, rules 34 and 49.
  it('shows what orders hold and puts out-of-kit parts in their own group', async () => {
    vi.spyOn(api, 'getProductStock').mockResolvedValue({ kits_by_option: [],
      balances: [
        { part_id: 1, name: 'shade', qty_per_unit: 1, balance: 4, held_for_orders: 3 },
        { part_id: 2, name: 'handle', qty_per_unit: 0, balance: 5, held_for_orders: 0 },
      ],
      kits_available: 4,
      movements: [],
    });
    render(<ProductStock productId={5} productName="Flask kit" canEdit={false} />);
    expect(await screen.findByTestId('stock-held-1')).toHaveTextContent('3');
    const group = screen.getByTestId('stock-out-of-kit');
    expect(within(group).getByText('Out of kit')).toBeInTheDocument();
    expect(within(group).getByText('handle')).toBeInTheDocument();
    expect(within(group).queryByText('shade')).not.toBeInTheDocument();
  });

  it('each counted part shows its picture beside its name (spec part-thumbnails §12.2)', async () => {
    vi.spyOn(api, 'getProductStock').mockResolvedValue({
      balances: [
        { part_id: 3, name: 'Body', qty_per_unit: 1, balance: 4, held_for_orders: 0, image: { kind: 'render', status: 'ready', v: 'b3' } },
        { part_id: 5, name: 'Cube', qty_per_unit: 0, balance: 1, held_for_orders: 0, image: null },
      ],
      kits_available: 4,
      kits_by_option: [],
      movements: [],
    } as never);
    render(<ProductStock productId={7} productName="Flask" canEdit={false} />);
    expect(await screen.findByRole('button', { name: 'Show Body larger' })).toBeInTheDocument();
    expect(within(screen.getByTestId('stock-out-of-kit')).getByTestId('part-thumb')).toHaveAttribute('data-state', 'none');
  });
});
