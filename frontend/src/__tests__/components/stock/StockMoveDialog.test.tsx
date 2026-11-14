/**
 * «Рух готових» (WS-13 E12 G): one movement of a finished-goods position — receipt,
 * stocktake, reserve, release or issue — in the Workshop dialog, with the active limit
 * named (R03), a «No choice» group as a configuration of its own (R11) and every number
 * taken from a CURRENT read only (G08 / R04).
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render } from '../../utils';
import { api, ApiError } from '../../../api/client';
import type { StockItemDetail, StockLookup, StockMoveKind } from '../../../api/client';
import { StockMoveDialog } from '../../../components/stock/StockMoveDialog';
import { pipeDetail, pipeItem, pipeProduct } from './stockFixtures';

/** 12 on hand: 4 held by hand, 2 by an order — 6 available (R03). */
const r03: StockItemDetail = {
  ...pipeDetail,
  on_hand: 12,
  reserved: 6,
  available: 6,
  reservations: [
    { project_line_id: null, project_id: null, project_code: null, project_name: null, qty: 4 },
    { project_line_id: 3, project_id: 42, project_code: 'OR-0042', project_name: 'Order for Ivan', qty: 2 },
  ],
};
const noManual: StockItemDetail = {
  ...r03,
  reserved: 2,
  available: 10,
  reservations: [{ project_line_id: 3, project_id: 42, project_code: 'OR-0042', project_name: 'Order for Ivan', qty: 2 }],
};
const found = (item = pipeItem): StockLookup => ({ item, configuration: pipeItem.configuration, can_assemble: 0, parts: [] });
const none: StockLookup = { item: null, configuration: pipeItem.configuration, can_assemble: 0, parts: [] };

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const submit = () => screen.getByTestId('stock-move-submit');
const qty = () => screen.getByLabelText('Quantity, pcs');
const limit = () => screen.getByTestId('stock-move-limit');

async function pickCustomer() {
  await screen.findByRole('option', { name: 'CU-0009 · ACME' });
  fireEvent.change(screen.getByLabelText('Customer'), { target: { value: '9' } });
}

describe('StockMoveDialog', () => {
  let move: ReturnType<typeof vi.spyOn>;
  let getItem: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    vi.restoreAllMocks();
    move = vi.spyOn(api, 'moveStock').mockResolvedValue({ ...pipeItem, moved: true });
    getItem = vi.spyOn(api, 'getStockItem').mockResolvedValue(pipeDetail);
    vi.spyOn(api, 'getStockCatalog').mockResolvedValue([pipeProduct] as never);
    vi.spyOn(api, 'getStockProduct').mockResolvedValue(pipeProduct as never);
    vi.spyOn(api, 'getCustomerOptions').mockResolvedValue([{ id: 9, code: 'CU-0009', name: 'ACME' }] as never);
    vi.spyOn(api, 'getDeliveryMethods').mockResolvedValue([]);
  });

  describe('the frame (G01)', () => {
    it.each<[StockMoveKind, string, string]>([
      ['receipt', 'Receipt', 'Receive'],
      ['stocktake', 'Stocktake', 'Save the count'],
      ['reserve', 'Reservation', 'Reserve'],
      ['release', 'Release of a reservation', 'Release'],
      ['issue', 'Issue', 'Issue'],
    ])('%s: its own title and the primary named for the action', async (kind, title, primary) => {
      render(<StockMoveDialog kind={kind} item={pipeItem} onClose={() => {}} />);
      const dialog = await screen.findByRole('dialog', { name: title });
      expect(dialog).toHaveAccessibleDescription('Pipe · SK-0005 · standard');
      expect(submit()).toHaveTextContent(primary);
      expect(within(dialog).getByRole('button', { name: 'Cancel' })).toBeInTheDocument();
    });

    it('from the header the subtitle says how finished goods are counted', async () => {
      render(<StockMoveDialog kind="receipt" onClose={() => {}} />);
      expect(await screen.findByRole('dialog', { name: 'Receipt' })).toHaveAccessibleDescription(
        'Finished goods are counted in pieces — separately for each configuration',
      );
    });
  });

  describe('the position (G02)', () => {
    it("a fixed position: the row's names, the figures only from the current read", async () => {
      const read = deferred<StockItemDetail>();
      getItem.mockReturnValue(read.promise);
      // The row says 99 on hand; the dialog never shows a figure the read did not give.
      render(<StockMoveDialog kind="reserve" item={{ ...pipeItem, on_hand: 99, available: 97 }} onClose={() => {}} />);
      const header = await screen.findByTestId('stock-position-header');
      expect(header).toHaveTextContent('Pipe');
      expect(header).toHaveTextContent('SK-0005');
      expect(header).toHaveTextContent('…');
      expect(header).not.toHaveTextContent('99');
      await act(async () => read.resolve(pipeDetail));
      await waitFor(() => expect(header).toHaveTextContent('On hand 5 · reserved 2 · available 3'));
    });

    it('from the header: a product, a select per group, and the server says which position it is', async () => {
      const lookup = vi.spyOn(api, 'lookupStockItem').mockResolvedValue(found());
      render(<StockMoveDialog kind="reserve" onClose={() => {}} />);
      fireEvent.click(await screen.findByRole('button', { name: 'PR-0001 · Pipe' }));
      await waitFor(() => expect(lookup).toHaveBeenLastCalledWith(1, []));
      await waitFor(() =>
        expect(screen.getByTestId('stock-lookup')).toHaveTextContent('Position SK-0005: on hand 5, reserved 2, available 3.'),
      );
      fireEvent.change(await screen.findByLabelText('Tail'), { target: { value: '101' } });
      await waitFor(() => expect(lookup).toHaveBeenLastCalledWith(1, [101]));
    });

    it('a receipt for a configuration without a position will create one', async () => {
      vi.spyOn(api, 'lookupStockItem').mockResolvedValue(none);
      // Stock takes any configuration (spec product-part-variant-combinations п. 14): whether an
      // order could take it is never asked here.
      const describeCombination = vi.spyOn(api, 'describeCombination');
      render(<StockMoveDialog kind="receipt" onClose={() => {}} />);
      fireEvent.click(await screen.findByRole('button', { name: 'PR-0001 · Pipe' }));
      expect(await screen.findByText('A new position will be created.')).toBeInTheDocument();
      fireEvent.change(qty(), { target: { value: '2' } });
      await waitFor(() => expect(submit()).toBeEnabled());
      fireEvent.click(submit());
      await waitFor(() => expect(move).toHaveBeenCalledWith({ kind: 'receipt', product_id: 1, options: [], qty: 2 }));
      expect(describeCombination).not.toHaveBeenCalled();
    });

    it('any other move of a configuration without a position is refused before it is sent, and says why', async () => {
      vi.spyOn(api, 'lookupStockItem').mockResolvedValue(none);
      render(<StockMoveDialog kind="reserve" onClose={() => {}} />);
      fireEvent.click(await screen.findByRole('button', { name: 'PR-0001 · Pipe' }));
      const note = await screen.findByText('This configuration has no position yet.');
      expect(note).toHaveClass('text-status-warning');
      expect(submit()).toBeDisabled();
      expect(submit()).toHaveAccessibleDescription('This configuration has no position yet.');
    });

    it("the lookup waits for the product's groups; a failed read of them offers a retry", async () => {
      const groups = deferred<typeof pipeProduct>();
      vi.spyOn(api, 'getStockProduct').mockReturnValueOnce(groups.promise as never).mockResolvedValue(pipeProduct as never);
      const lookup = vi.spyOn(api, 'lookupStockItem').mockResolvedValue(found());
      render(<StockMoveDialog kind="receipt" productId={1} onClose={() => {}} />);
      expect(await screen.findByText('Reading the variants…')).toBeInTheDocument();
      expect(lookup).not.toHaveBeenCalled();
      await act(async () => groups.reject(new Error('HTTP 500')));
      const alert = await screen.findByRole('alert');
      expect(alert).toHaveTextContent("Could not load the product's variants");
      expect(lookup).not.toHaveBeenCalled();
      fireEvent.click(within(alert).getByRole('button', { name: 'Retry' }));
      await waitFor(() => expect(lookup).toHaveBeenCalledWith(1, []));
    });

    // WS-13 E9 final review: from the product page the product is NAMED, not picked.
    it('opened for one product: names it, reads no catalog, and only its configuration is picked', async () => {
      const lookup = vi.spyOn(api, 'lookupStockItem').mockResolvedValue(none);
      render(<StockMoveDialog kind="receipt" productId={1} onClose={() => {}} />);
      await waitFor(() => expect(screen.getByTestId('stock-locked-product')).toHaveTextContent('PR-0001 · Pipe'));
      expect(screen.queryByRole('button', { name: 'PR-0001 · Pipe' })).not.toBeInTheDocument();
      expect(api.getStockCatalog).not.toHaveBeenCalled();
      fireEvent.change(await screen.findByLabelText('Tail'), { target: { value: '101' } });
      await waitFor(() => expect(lookup).toHaveBeenLastCalledWith(1, [101]));
    });

    // R11 / E9-V01: «No choice» is a configuration of its own.
    it('a group without a standard starts at «No choice»; the first option is a real choice; going back sends none', async () => {
      const noStandard = { ...pipeProduct, variant_groups: pipeProduct.variant_groups.map((g) => ({ ...g, default_option_id: null })) };
      vi.spyOn(api, 'getStockProduct').mockResolvedValue(noStandard as never);
      const lookup = vi.spyOn(api, 'lookupStockItem').mockResolvedValue({ ...none, configuration: { choices: [], changed_parts: [] } });
      render(<StockMoveDialog kind="receipt" productId={1} onClose={() => {}} />);
      const select = (await screen.findByRole('combobox', { name: 'Tail' })) as HTMLSelectElement;
      expect(select.value).toBe('');
      await waitFor(() => expect(lookup).toHaveBeenLastCalledWith(1, []));
      fireEvent.change(select, { target: { value: '100' } });
      await waitFor(() => expect(lookup).toHaveBeenLastCalledWith(1, [100]));
      fireEvent.change(select, { target: { value: '' } });
      await waitFor(() => expect(lookup).toHaveBeenLastCalledWith(1, []));
      await waitFor(() => expect(submit()).toBeEnabled());
      fireEvent.click(submit());
      await waitFor(() => expect(move).toHaveBeenCalledWith({ kind: 'receipt', product_id: 1, options: [], qty: 1 }));
    });
  });

  describe('the active limit (G03, R03)', () => {
    // Braces: a function returned from beforeEach is run as its teardown.
    beforeEach(() => {
      getItem.mockResolvedValue(r03);
    });

    it('a reservation: the available count, named; one more is said under the field and waits', async () => {
      render(<StockMoveDialog kind="reserve" item={pipeItem} onClose={() => {}} />);
      await waitFor(() => expect(limit()).toHaveTextContent('You can reserve 6 (available)'));
      fireEvent.change(qty(), { target: { value: '6' } });
      expect(submit()).toBeEnabled();
      fireEvent.change(qty(), { target: { value: '7' } });
      expect(limit()).toHaveTextContent('No more than 6');
      expect(submit()).toBeDisabled();
      expect(submit()).toHaveAccessibleDescription('No more than 6');
    });

    it('a release: the manual reservation only — an order’s never counts', async () => {
      render(<StockMoveDialog kind="release" item={pipeItem} onClose={() => {}} />);
      await waitFor(() => expect(limit()).toHaveTextContent('You can release 4 (in the manual reservation)'));
      fireEvent.change(qty(), { target: { value: '5' } });
      expect(submit()).toBeDisabled();
    });

    it('an issue: 6 of the available, or 4 from the manual reservation — the two never add up', async () => {
      render(<StockMoveDialog kind="issue" item={pipeItem} onClose={() => {}} />);
      await pickCustomer();
      await waitFor(() => expect(limit()).toHaveTextContent('You can issue 6 (available)'));
      fireEvent.change(qty(), { target: { value: '6' } });
      expect(submit()).toBeEnabled();
      fireEvent.change(qty(), { target: { value: '7' } });
      expect(submit()).toBeDisabled();
      fireEvent.change(qty(), { target: { value: '1' } });
      fireEvent.click(screen.getByLabelText('From the manual reservation'));
      expect(limit()).toHaveTextContent('You can issue 4 (from the manual reservation)');
      fireEvent.change(qty(), { target: { value: '4' } });
      expect(submit()).toBeEnabled();
      fireEvent.change(qty(), { target: { value: '5' } });
      expect(submit()).toBeDisabled();
    });

    it('while the position is read the limit is «reading…», never 0, and the primary waits', async () => {
      getItem.mockReturnValue(new Promise(() => {}) as never);
      render(<StockMoveDialog kind="reserve" item={pipeItem} onClose={() => {}} />);
      await waitFor(() => expect(limit()).toHaveTextContent('reading…'));
      expect(limit()).not.toHaveTextContent('0');
      expect(submit()).toBeDisabled();
    });

    it('the mark «from the manual reservation» shows only when there is one', async () => {
      getItem.mockResolvedValue(noManual);
      render(<StockMoveDialog kind="issue" item={pipeItem} onClose={() => {}} />);
      await waitFor(() => expect(limit()).toHaveTextContent('You can issue 10 (available)'));
      expect(screen.queryByLabelText('From the manual reservation')).toBeNull();
    });

    it('a ticked mark whose reservation a re-read emptied stays ticked and visible, and the primary says why', async () => {
      move.mockRejectedValue(new ApiError('Only 0 reserved without an order', 409));
      render(<StockMoveDialog kind="issue" item={pipeItem} onClose={() => {}} />);
      await pickCustomer();
      fireEvent.click(await screen.findByLabelText('From the manual reservation'));
      fireEvent.change(qty(), { target: { value: '2' } });
      getItem.mockResolvedValue(noManual);
      fireEvent.click(submit());
      expect(await screen.findByRole('alert')).toHaveTextContent('Only 0 reserved without an order');
      await waitFor(() =>
        expect(submit()).toHaveAccessibleDescription('Nothing is in the manual reservation — clear the mark'),
      );
      expect(screen.getByLabelText('From the manual reservation')).toBeChecked();
      expect(submit()).toBeDisabled();
    });
  });

  // Final review M7: the server's manual reservation is a reservation with no order LINE —
  // a line it could not resolve to an order still belongs to that order, never to the hand.
  it('a reservation of an order line the server could not name is not manual', async () => {
    getItem.mockResolvedValue({
      ...r03,
      reserved: 4,
      available: 8,
      reservations: [{ project_line_id: 7, project_id: null, project_code: null, project_name: null, qty: 4 }],
    });
    render(<StockMoveDialog kind="release" item={pipeItem} onClose={() => {}} />);
    await waitFor(() => expect(limit()).toHaveTextContent('No more than 0'));
    expect(submit()).toBeDisabled();
  });

  // Final review M13: the release's hint says what its limit says.
  it('the release says it touches only the manual reservation', async () => {
    render(<StockMoveDialog kind="release" item={pipeItem} onClose={() => {}} />);
    expect(
      await screen.findByText("Decreases the manual reservation; an order's reservation is released in its order."),
    ).toBeInTheDocument();
  });

  describe('a stocktake (G04)', () => {
    // Final review M8: a count that is no count says why the primary waits.
    it('a count that is not a whole number from 0 says so', async () => {
      render(<StockMoveDialog kind="stocktake" item={pipeItem} onClose={() => {}} />);
      const counted = await screen.findByLabelText('Counted on the shelf');
      fireEvent.change(counted, { target: { value: '2.5' } });
      await waitFor(() => expect(submit()).toHaveAccessibleDescription('The count is a whole number from 0'));
      expect(submit()).toBeDisabled();
    });

    it('the counted quantity beside what stands now, and the change', async () => {
      render(<StockMoveDialog kind="stocktake" item={pipeItem} onClose={() => {}} />);
      expect(await screen.findByText('Now: on hand 5, reserved 2')).toBeInTheDocument();
      fireEvent.change(screen.getByLabelText('Counted on the shelf'), { target: { value: '7' } });
      expect(screen.getByText('+2')).toBeInTheDocument();
      fireEvent.click(submit());
      await waitFor(() => expect(move).toHaveBeenCalledWith({ kind: 'stocktake', item_id: 5, counted: 7 }));
    });

    it('a lower count needs a note before it is sent', async () => {
      render(<StockMoveDialog kind="stocktake" item={pipeItem} onClose={() => {}} />);
      await screen.findByText('Now: on hand 5, reserved 2');
      fireEvent.change(screen.getByLabelText('Counted on the shelf'), { target: { value: '4' } });
      expect(submit()).toBeDisabled();
      expect(submit()).toHaveAccessibleDescription('A lower count needs a note');
      fireEvent.change(screen.getByLabelText('Basis / note'), { target: { value: 'broken' } });
      expect(submit()).toBeEnabled();
      fireEvent.click(submit());
      await waitFor(() => expect(move).toHaveBeenCalledWith({ kind: 'stocktake', item_id: 5, counted: 4, note: 'broken' }));
    });

    it('a count below the reservation is said before it is sent', async () => {
      render(<StockMoveDialog kind="stocktake" item={pipeItem} onClose={() => {}} />);
      await screen.findByText('Now: on hand 5, reserved 2');
      fireEvent.change(screen.getByLabelText('Counted on the shelf'), { target: { value: '1' } });
      fireEvent.change(screen.getByLabelText('Basis / note'), { target: { value: 'lost' } });
      expect(screen.getByText('Not below the reservation of 2 — release the reservation first')).toBeInTheDocument();
      expect(submit()).toBeDisabled();
    });

    it('a count that matches the shelf is sent, and says nothing moved', async () => {
      move.mockResolvedValue({ ...pipeItem, moved: false });
      render(<StockMoveDialog kind="stocktake" item={pipeItem} onClose={() => {}} />);
      await screen.findByText('Now: on hand 5, reserved 2');
      fireEvent.change(screen.getByLabelText('Counted on the shelf'), { target: { value: '5' } });
      expect(screen.getByText('no change')).toBeInTheDocument();
      fireEvent.click(submit());
      expect(await screen.findByText('Nothing changed — the count matches the shelf.')).toBeInTheDocument();
    });

    it('a zero count of a configuration with no position is not offered — it would move nothing', async () => {
      vi.spyOn(api, 'lookupStockItem').mockResolvedValue(none);
      render(<StockMoveDialog kind="stocktake" onClose={() => {}} />);
      fireEvent.click(await screen.findByRole('button', { name: 'PR-0001 · Pipe' }));
      await screen.findByText('A new position will be created.');
      fireEvent.change(screen.getByLabelText('Counted on the shelf'), { target: { value: '0' } });
      expect(submit()).toBeDisabled();
      fireEvent.change(screen.getByLabelText('Counted on the shelf'), { target: { value: '2' } });
      expect(submit()).toBeEnabled();
    });
  });

  describe('an issue (G05)', () => {
    const acme = {
      id: 9,
      name: 'ACME',
      contacts: [
        { id: 1, name: 'Ivan', phone: '+380', delivery_method_name: 'Nova Poshta', delivery_details: 'Branch 5' },
        { id: 2, name: 'Olena', phone: null, delivery_method_name: null, delivery_details: null },
      ],
    };

    it('names its customer — without one the primary waits and says why', async () => {
      render(<StockMoveDialog kind="issue" item={pipeItem} onClose={() => {}} />);
      await screen.findByRole('option', { name: 'CU-0009 · ACME' });
      expect(submit()).toBeDisabled();
      expect(submit()).toHaveAccessibleDescription('An issue names its customer.');
    });

    it('takes the main contact as the recipient and sends the waybill', async () => {
      vi.spyOn(api, 'getDeliveryMethods').mockResolvedValue([{ id: 1, name: 'Nova Poshta', position: 0, contacts_count: 1 }]);
      vi.spyOn(api, 'getCustomer').mockResolvedValue(acme as never);
      render(<StockMoveDialog kind="issue" item={pipeItem} onClose={() => {}} />);
      await pickCustomer();
      await waitFor(() => expect(screen.getByLabelText('Recipient name')).toHaveValue('Ivan'));
      expect(screen.getByLabelText('Delivery method')).toHaveValue('Nova Poshta');
      expect(screen.getByLabelText('Waybill no.')).toHaveAttribute('maxLength', '24');
      fireEvent.change(screen.getByLabelText('Waybill no.'), { target: { value: '2045' } });
      fireEvent.click(screen.getByLabelText('From the manual reservation'));
      await waitFor(() => expect(submit()).toBeEnabled());
      fireEvent.click(submit());
      await waitFor(() =>
        expect(move).toHaveBeenCalledWith({
          kind: 'issue',
          item_id: 5,
          qty: 1,
          customer_id: 9,
          from_reserve: true,
          recipient: { name: 'Ivan', phone: '+380', delivery_method: 'Nova Poshta', delivery_details: 'Branch 5' },
          waybill: '2045',
        }),
      );
    });

    it('while the contacts are read it says so, and the fields stay open', async () => {
      vi.spyOn(api, 'getCustomer').mockReturnValue(new Promise(() => {}) as never);
      render(<StockMoveDialog kind="issue" item={pipeItem} onClose={() => {}} />);
      await pickCustomer();
      expect(await screen.findByText('Reading the contacts…')).toBeInTheDocument();
      expect(screen.getByLabelText('Recipient name')).toBeEnabled();
      expect(screen.getByLabelText('Recipient name')).toHaveValue('');
    });

    it('contacts that failed say so with a retry; nothing is filled in silently', async () => {
      vi.spyOn(api, 'getCustomer').mockRejectedValueOnce(new Error('HTTP 500')).mockResolvedValue(acme as never);
      render(<StockMoveDialog kind="issue" item={pipeItem} onClose={() => {}} />);
      await pickCustomer();
      const note = await screen.findByText('Could not load the contacts');
      expect(screen.getByLabelText('Recipient name')).toHaveValue('');
      expect(screen.getByLabelText('Recipient name')).toBeEnabled();
      fireEvent.click(within(note.closest('[data-testid="stock-move-contacts"]') as HTMLElement).getByRole('button', { name: 'Retry' }));
      await waitFor(() => expect(screen.getByLabelText('Recipient name')).toHaveValue('Ivan'));
    });

    it('a customer with no contacts leaves the fields empty, with no note', async () => {
      vi.spyOn(api, 'getCustomer').mockResolvedValue({ id: 9, name: 'ACME', contacts: [] } as never);
      render(<StockMoveDialog kind="issue" item={pipeItem} onClose={() => {}} />);
      await pickCustomer();
      await waitFor(() => expect(screen.queryByText('Reading the contacts…')).toBeNull());
      expect(screen.queryByText('Could not load the contacts')).toBeNull();
      expect(screen.getByLabelText('Recipient name')).toHaveValue('');
    });

    it('what the operator typed is never overwritten by contacts that arrive later', async () => {
      const read = deferred<typeof acme>();
      vi.spyOn(api, 'getCustomer').mockReturnValue(read.promise as never);
      render(<StockMoveDialog kind="issue" item={pipeItem} onClose={() => {}} />);
      await pickCustomer();
      fireEvent.change(screen.getByLabelText('Recipient name'), { target: { value: 'Petro' } });
      await act(async () => read.resolve(acme));
      expect(screen.getByLabelText('Recipient name')).toHaveValue('Petro');
    });

    // Final review M12 (G08): a customer read a minute ago is read again at the pick.
    it('a cached customer is read again at the pick: «reading…» until it answers, then its contact', async () => {
      const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 60_000 } } });
      client.setQueryData(['customer-recipient', 9], {
        ...acme,
        contacts: [{ id: 1, name: 'Old', phone: null, delivery_method_name: null, delivery_details: null }],
      });
      const read = deferred<typeof acme>();
      const get = vi.spyOn(api, 'getCustomer').mockReturnValue(read.promise as never);
      render(
        <QueryClientProvider client={client}>
          <StockMoveDialog kind="issue" item={pipeItem} onClose={() => {}} />
        </QueryClientProvider>,
      );
      await pickCustomer();
      await waitFor(() => expect(get).toHaveBeenCalledWith(9));
      expect(screen.getByText('Reading the contacts…')).toBeInTheDocument();
      await act(async () => read.resolve(acme));
      await waitFor(() => expect(screen.getByLabelText('Recipient name')).toHaveValue('Ivan'));
      expect(screen.queryByText('Reading the contacts…')).toBeNull();
    });

    it('an issue names the dispatch note it made', async () => {
      move.mockResolvedValue({ ...pipeItem, moved: true, issue_id: 7, issue_code: 'DN-0007' });
      vi.spyOn(api, 'getCustomer').mockResolvedValue({ id: 9, name: 'ACME', contacts: [] } as never);
      const onClose = vi.fn();
      render(<StockMoveDialog kind="issue" item={pipeItem} onClose={onClose} />);
      await pickCustomer();
      await waitFor(() => expect(submit()).toBeEnabled());
      fireEvent.click(submit());
      expect(await screen.findByRole('dialog', { name: 'Dispatch note issued' })).toHaveTextContent('DN-0007');
      expect(onClose).not.toHaveBeenCalled();
    });

    // E6-V01 (Codex): the note's window names the units the request SENT.
    it.each(['3', '9'])('the note names the issued quantity when the field reads %s by the answer', async (typedMeanwhile) => {
      vi.spyOn(api, 'getCustomer').mockResolvedValue({ id: 9, name: 'ACME', contacts: [] } as never);
      getItem.mockResolvedValue(r03);
      let finish!: (value: never) => void;
      move.mockImplementation(() => new Promise((resolve) => (finish = resolve)));
      render(<StockMoveDialog kind="issue" item={pipeItem} onClose={() => {}} />);
      await pickCustomer();
      fireEvent.change(qty(), { target: { value: '3' } });
      await waitFor(() => expect(submit()).toBeEnabled());
      fireEvent.click(submit());
      await waitFor(() => expect(move).toHaveBeenCalledWith(expect.objectContaining({ qty: 3 })));
      fireEvent.change(qty(), { target: { value: typedMeanwhile } });
      await act(async () => finish({ ...pipeItem, moved: true, issue_id: 7, issue_code: 'DN-0007' } as never));
      expect(await screen.findByRole('dialog', { name: 'Dispatch note issued' })).toHaveTextContent('DN-0007 · 3 pcs');
      expect(move).toHaveBeenCalledTimes(1);
    });
  });

  describe('the note (G06)', () => {
    it.each<[StockMoveKind, string, string]>([
      ['receipt', 'Received from a partner…', "Increases the position's stock."],
      ['issue', 'Shipped…', 'Decreases the stock; no more than is available, or within the manual reservation.'],
      ['reserve', '', 'Increases the reservation; no more than is available.'],
      ['release', '', "Decreases the manual reservation; an order's reservation is released in its order."],
      ['stocktake', '', 'Records the counted quantity; the difference goes into the journal.'],
    ])('%s: the basis field and the kind’s hint', async (kind, placeholder, hint) => {
      render(<StockMoveDialog kind={kind} item={pipeItem} onClose={() => {}} />);
      const note = await screen.findByLabelText('Basis / note');
      expect(note).toHaveAttribute('maxLength', '500');
      if (placeholder) expect(note).toHaveAttribute('placeholder', placeholder);
      expect(screen.getByText(hint)).toBeInTheDocument();
    });
  });

  describe('behaviour (G07)', () => {
    it('the cursor starts in the first field: the quantity for a position, the count for a stocktake, the product from the header', async () => {
      const { unmount } = render(<StockMoveDialog kind="receipt" item={pipeItem} onClose={() => {}} />);
      await waitFor(() => expect(qty()).toHaveFocus());
      unmount();
      const second = render(<StockMoveDialog kind="stocktake" item={pipeItem} onClose={() => {}} />);
      await waitFor(() => expect(screen.getByLabelText('Counted on the shelf')).toHaveFocus());
      second.unmount();
      render(<StockMoveDialog kind="receipt" onClose={() => {}} />);
      await waitFor(() => expect(screen.getByLabelText('Product')).toHaveFocus());
    });

    it('a refusal stays in the dialog: the server sentence in its slot, the focus on the primary, the draft whole, the position read again', async () => {
      getItem.mockResolvedValue(r03);
      move.mockRejectedValue(new ApiError('Only 3 available', 409));
      const onClose = vi.fn();
      render(<StockMoveDialog kind="reserve" item={pipeItem} onClose={onClose} />);
      await waitFor(() => expect(limit()).toHaveTextContent('You can reserve 6 (available)'));
      fireEvent.change(qty(), { target: { value: '5' } });
      fireEvent.change(screen.getByLabelText('Basis / note'), { target: { value: 'for Ivan' } });
      const reads = getItem.mock.calls.length;
      // A real press puts the focus on the button first.
      submit().focus();
      fireEvent.click(submit());
      const alert = await screen.findByRole('alert');
      expect(alert).toHaveTextContent('Only 3 available');
      expect(within(screen.getByRole('dialog')).getByRole('alert')).toBe(alert);
      await waitFor(() => expect(submit()).toHaveFocus());
      expect(qty()).toHaveValue(5);
      expect(screen.getByLabelText('Basis / note')).toHaveValue('for Ivan');
      await waitFor(() => expect(getItem.mock.calls.length).toBeGreaterThan(reads));
      expect(onClose).not.toHaveBeenCalled();
    });

    // E12 pilot (browser): the primary waits while the position is read again, a disabled
    // button cannot keep the focus, and it fell to the page. Once the re-read has answered the
    // focus goes to the primary when it can act — else to the field that says why.
    it('a refusal whose re-read leaves the draft over the new limit puts the cursor in the quantity', async () => {
      getItem.mockResolvedValueOnce(r03).mockResolvedValue({ ...r03, reserved: 10, available: 2 });
      move.mockRejectedValue(new ApiError('Only 2 available', 409));
      render(<StockMoveDialog kind="reserve" item={pipeItem} onClose={() => {}} />);
      await waitFor(() => expect(limit()).toHaveTextContent('You can reserve 6 (available)'));
      fireEvent.change(qty(), { target: { value: '5' } });
      // A real press puts the focus on the button first.
      submit().focus();
      fireEvent.click(submit());
      expect(await screen.findByRole('alert')).toHaveTextContent('Only 2 available');
      await waitFor(() => expect(limit()).toHaveTextContent('No more than 2'));
      await waitFor(() => expect(qty()).toHaveFocus());
      expect(submit()).toBeDisabled();
    });

    // Final review I2: the re-read can answer while the operator is already fixing the draft —
    // a focus they placed is theirs; only a focus that was lost is given back.
    it('a re-read that answers while the operator types leaves the cursor where they put it', async () => {
      const again = deferred<StockItemDetail>();
      getItem.mockResolvedValueOnce(r03).mockReturnValueOnce(again.promise as never).mockResolvedValue(r03);
      move.mockRejectedValue(new ApiError('Only 3 available', 409));
      render(<StockMoveDialog kind="reserve" item={pipeItem} onClose={() => {}} />);
      await waitFor(() => expect(limit()).toHaveTextContent('You can reserve 6 (available)'));
      fireEvent.change(qty(), { target: { value: '5' } });
      submit().focus();
      fireEvent.click(submit());
      expect(await screen.findByRole('alert')).toHaveTextContent('Only 3 available');
      const noteField = screen.getByLabelText('Basis / note');
      noteField.focus();
      await act(async () => again.resolve(r03));
      await waitFor(() => expect(limit()).toHaveTextContent('You can reserve 6 (available)'));
      expect(noteField).toHaveFocus();
    });

    // Final review M3: nothing is being read after a failed re-read — no «reading…».
    it('a re-read that failed after a refusal names no limit it does not have', async () => {
      getItem.mockResolvedValueOnce(r03).mockRejectedValue(new Error('HTTP 500'));
      move.mockRejectedValue(new ApiError('Only 3 available', 409));
      render(<StockMoveDialog kind="reserve" item={pipeItem} onClose={() => {}} />);
      await waitFor(() => expect(limit()).toHaveTextContent('You can reserve 6 (available)'));
      fireEvent.click(submit());
      expect(await screen.findByRole('alert')).toHaveTextContent('Only 3 available');
      expect(await screen.findByText('Could not refresh')).toBeInTheDocument();
      expect(screen.queryAllByText('reading…')).toHaveLength(0);
      expect(submit()).toBeDisabled();
    });

    it('from the header a refusal reads the configuration again', async () => {
      const lookup = vi.spyOn(api, 'lookupStockItem').mockResolvedValue(found());
      move.mockRejectedValue(new ApiError('Only 3 available', 409));
      render(<StockMoveDialog kind="reserve" onClose={() => {}} />);
      fireEvent.click(await screen.findByRole('button', { name: 'PR-0001 · Pipe' }));
      await waitFor(() => expect(submit()).toBeEnabled());
      const reads = lookup.mock.calls.length;
      fireEvent.click(submit());
      expect(await screen.findByRole('alert')).toHaveTextContent('Only 3 available');
      await waitFor(() => expect(lookup.mock.calls.length).toBeGreaterThan(reads));
    });

    it('one press, one request; nothing closes the dialog while it is on its way', async () => {
      const sent = deferred<never>();
      move.mockReturnValue(sent.promise);
      const onClose = vi.fn();
      render(<StockMoveDialog kind="receipt" item={pipeItem} onClose={onClose} />);
      await waitFor(() => expect(submit()).toBeEnabled());
      fireEvent.click(submit());
      fireEvent.click(submit());
      await waitFor(() => expect(move).toHaveBeenCalledTimes(1));
      fireEvent.keyDown(window, { key: 'Escape' });
      fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
      expect(onClose).not.toHaveBeenCalled();
      await act(async () => sent.resolve({ ...pipeItem, moved: true } as never));
      await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
    });

    it('a success says so and closes', async () => {
      const onClose = vi.fn();
      render(<StockMoveDialog kind="receipt" item={pipeItem} onClose={onClose} />);
      fireEvent.change(qty(), { target: { value: '3' } });
      fireEvent.change(screen.getByLabelText('Basis / note'), { target: { value: 'x' } });
      fireEvent.click(submit());
      await waitFor(() => expect(move).toHaveBeenCalledWith({ kind: 'receipt', item_id: 5, qty: 3, note: 'x' }));
      expect(await screen.findByText('The stock was updated.')).toBeInTheDocument();
      await waitFor(() => expect(onClose).toHaveBeenCalled());
    });
  });

  describe('freshness (G08, R04)', () => {
    it('a cached position is not the answer: 10 in the cache, 6 from the read — the limit is 6', async () => {
      const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 60_000 } } });
      client.setQueryData(['stock-item', 5], { ...r03, available: 10 });
      getItem.mockResolvedValue(r03);
      render(
        <QueryClientProvider client={client}>
          <StockMoveDialog kind="reserve" item={pipeItem} onClose={() => {}} />
        </QueryClientProvider>,
      );
      await waitFor(() => expect(getItem).toHaveBeenCalled());
      await waitFor(() => expect(limit()).toHaveTextContent('You can reserve 6 (available)'));
      expect(limit()).not.toHaveTextContent('10');
    });

    // F6 D1 (owner, 2026-10-04): a re-read of the SAME position keeps the numbers this dialog
    // already read on screen, dimmed — never as the limit: the primary waits for the answer.
    it('a re-read of the same position keeps its last numbers on screen, dimmed, and the primary waits (F6 D1)', async () => {
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      const again = deferred<StockItemDetail>();
      getItem.mockResolvedValueOnce(r03).mockReturnValueOnce(again.promise as never);
      render(
        <QueryClientProvider client={client}>
          <StockMoveDialog kind="reserve" item={pipeItem} onClose={() => {}} />
        </QueryClientProvider>,
      );
      await waitFor(() => expect(limit()).toHaveTextContent('You can reserve 6 (available)'));
      await waitFor(() => expect(submit()).toBeEnabled());
      act(() => {
        void client.invalidateQueries({ queryKey: ['stock-item'] });
      });
      await waitFor(() => expect(submit()).toBeDisabled());
      expect(limit()).toHaveTextContent('You can reserve 6 (available)');
      expect(limit()).toHaveAttribute('data-stale', 'true');
      const header = screen.getByTestId('stock-position-header');
      expect(header).toHaveTextContent('On hand 12 · reserved 6 · available 6');
      expect(header).toHaveAttribute('data-stale', 'true');
      await act(async () => again.resolve({ ...r03, reserved: 7, available: 5 }));
      await waitFor(() => expect(limit()).toHaveTextContent('You can reserve 5 (available)'));
      expect(limit()).not.toHaveAttribute('data-stale');
      expect(screen.getByTestId('stock-position-header')).not.toHaveAttribute('data-stale');
      expect(submit()).toBeEnabled();
    });

    it('from the header, a re-read of the same configuration keeps its position line, dimmed (F6 D1)', async () => {
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      const again = deferred<StockLookup>();
      vi.spyOn(api, 'lookupStockItem')
        .mockResolvedValueOnce(found())
        .mockReturnValueOnce(again.promise as never)
        .mockResolvedValue(found());
      render(
        <QueryClientProvider client={client}>
          <StockMoveDialog kind="receipt" onClose={() => {}} />
        </QueryClientProvider>,
      );
      fireEvent.click(await screen.findByRole('button', { name: 'PR-0001 · Pipe' }));
      await waitFor(() => expect(screen.getByTestId('stock-lookup')).toHaveTextContent('Position SK-0005'));
      await waitFor(() => expect(submit()).toBeEnabled());
      act(() => {
        void client.invalidateQueries({ queryKey: ['stock-lookup'] });
      });
      await waitFor(() => expect(submit()).toBeDisabled());
      expect(screen.getByTestId('stock-lookup')).toHaveTextContent('Position SK-0005');
      expect(screen.getByTestId('stock-lookup')).toHaveAttribute('data-stale', 'true');
      await act(async () => again.resolve(found()));
      await waitFor(() => expect(screen.getByTestId('stock-lookup')).not.toHaveAttribute('data-stale'));
      await waitFor(() => expect(submit()).toBeEnabled());
    });

    // Codex E12-V05: a first read that failed is no answer of this dialog — a cache from before
    // the opening stays off the screen; a retry that succeeds is one, and its numbers then hold
    // (dimmed) through the next re-read of the same position.
    it('a first read that failed shows no cached numbers; its retry does, and they hold through the next re-read (Codex V05)', async () => {
      const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 60_000 } } });
      client.setQueryData(['stock-item', 5], { ...r03, on_hand: 987, available: 985 });
      const again = deferred<StockItemDetail>();
      getItem
        .mockRejectedValueOnce(new Error('HTTP 500'))
        .mockResolvedValueOnce(r03)
        .mockReturnValueOnce(again.promise as never);
      render(
        <QueryClientProvider client={client}>
          <StockMoveDialog kind="reserve" item={pipeItem} onClose={() => {}} />
        </QueryClientProvider>,
      );
      expect(await screen.findByText('Could not read the position')).toBeInTheDocument();
      const header = screen.getByTestId('stock-position-header');
      expect(header).not.toHaveTextContent('987');
      expect(header).toHaveTextContent('…');
      expect(screen.queryByTestId('stock-move-limit')).toBeNull();
      expect(submit()).toBeDisabled();
      fireEvent.click(within(screen.getByTestId('stock-move-position')).getByRole('button', { name: 'Retry' }));
      await waitFor(() => expect(limit()).toHaveTextContent('You can reserve 6 (available)'));
      expect(screen.getByTestId('stock-position-header')).toHaveTextContent('On hand 12');
      act(() => {
        void client.invalidateQueries({ queryKey: ['stock-item'] });
      });
      await waitFor(() => expect(submit()).toBeDisabled());
      expect(screen.getByTestId('stock-position-header')).toHaveTextContent('On hand 12');
      expect(screen.getByTestId('stock-position-header')).toHaveAttribute('data-stale', 'true');
      await act(async () => again.resolve(r03));
      await waitFor(() => expect(submit()).toBeEnabled());
    });

    it('from the header, a first lookup that failed shows no cached position line (Codex V05)', async () => {
      const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 60_000 } } });
      const cached = found({ ...pipeItem, on_hand: 987, available: 985 });
      client.setQueryData(['stock-lookup', 1, ''], cached);
      client.setQueryData(['stock-lookup', 1, '100'], cached);
      vi.spyOn(api, 'lookupStockItem').mockRejectedValueOnce(new Error('HTTP 500')).mockResolvedValue(found());
      render(
        <QueryClientProvider client={client}>
          <StockMoveDialog kind="receipt" onClose={() => {}} />
        </QueryClientProvider>,
      );
      fireEvent.click(await screen.findByRole('button', { name: 'PR-0001 · Pipe' }));
      expect(await screen.findByText('Could not read this configuration')).toBeInTheDocument();
      expect(screen.queryByText(/987/)).toBeNull();
      expect(submit()).toBeDisabled();
      fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
      await waitFor(() => expect(screen.getByTestId('stock-lookup')).toHaveTextContent('Position SK-0005'));
      expect(screen.queryByText(/987/)).toBeNull();
    });

    it('another configuration on its way: «reading…», and the primary waits — the old answer proves nothing', async () => {
      const angled = deferred<StockLookup>();
      vi.spyOn(api, 'lookupStockItem').mockImplementation(async (_id: number, options: number[] = []) =>
        options.includes(101) ? angled.promise : found(),
      );
      render(<StockMoveDialog kind="reserve" onClose={() => {}} />);
      fireEvent.click(await screen.findByRole('button', { name: 'PR-0001 · Pipe' }));
      await waitFor(() => expect(submit()).toBeEnabled());
      fireEvent.change(await screen.findByLabelText('Tail'), { target: { value: '101' } });
      await waitFor(() => expect(limit()).toHaveTextContent('reading…'));
      expect(submit()).toBeDisabled();
      expect(screen.queryByText('This configuration has no position yet.')).toBeNull();
    });

    it('another configuration that could not be read: an alert with its retry, and the primary waits', async () => {
      vi.spyOn(api, 'lookupStockItem').mockImplementation(async (_id: number, options: number[] = []) => {
        if (options.includes(101)) throw new Error('HTTP 500');
        return found();
      });
      render(<StockMoveDialog kind="reserve" onClose={() => {}} />);
      fireEvent.click(await screen.findByRole('button', { name: 'PR-0001 · Pipe' }));
      await waitFor(() => expect(submit()).toBeEnabled());
      fireEvent.change(await screen.findByLabelText('Tail'), { target: { value: '101' } });
      expect(await screen.findByText('Could not read this configuration')).toBeInTheDocument();
      expect(submit()).toBeDisabled();
    });

    it('a refusal, a failed re-read, a retry: the draft stays whole and the new limit stands', async () => {
      getItem.mockResolvedValueOnce(r03);
      move.mockRejectedValueOnce(new ApiError('Only 2 available', 409));
      render(<StockMoveDialog kind="reserve" item={pipeItem} onClose={() => {}} />);
      await waitFor(() => expect(limit()).toHaveTextContent('You can reserve 6 (available)'));
      fireEvent.change(qty(), { target: { value: '5' } });
      fireEvent.change(screen.getByLabelText('Basis / note'), { target: { value: 'hold' } });
      getItem.mockRejectedValueOnce(new Error('HTTP 500'));
      fireEvent.click(submit());
      expect(await screen.findByText('Could not refresh')).toBeInTheDocument();
      expect(submit()).toBeDisabled();
      getItem.mockResolvedValue({ ...r03, reserved: 10, available: 2 });
      fireEvent.click(within(screen.getByTestId('stock-move-position')).getByRole('button', { name: 'Retry' }));
      // The draft (5) stays; the new limit (2) judges it.
      await waitFor(() => expect(limit()).toHaveTextContent('No more than 2'));
      expect(qty()).toHaveValue(5);
      expect(screen.getByLabelText('Basis / note')).toHaveValue('hold');
      expect(submit()).toBeDisabled();
    });
  });
});
