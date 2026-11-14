import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { render } from '../../../utils';
import { api, ApiError } from '../../../../api/client';
import type { CombinationState, ProductListItem } from '../../../../api/client';
import { AddToOrderDialog } from '../../../../components/projects/add-to-order/AddToOrderDialog';
import { lamp, pageOf, pipe, pipeDetail, suggestion, vase } from './fixtures';


/** The order the dialog is opened from (WS-13 E5: the dialog names it). */
const ORDER = { id: 5, code: 'OR-0005', name: 'Flasks for Acme', active: true };

describe('the products tab of «Add to order»', () => {
  let getPage: ReturnType<typeof vi.spyOn>;
  let suggest: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    vi.restoreAllMocks();
    getPage = vi
      .spyOn(api, 'getProductsPaged')
      .mockImplementation(async (params) => (params.page === 2 ? pageOf([vase], 30, 2) : pageOf([pipe, lamp], 30)));
    vi.spyOn(api, 'getProduct').mockResolvedValue(pipeDetail as never);
    vi.spyOn(api, 'getProductCategories').mockResolvedValue([{ id: 7, name: 'Pipes', products_count: 1 }]);
    vi.spyOn(api, 'getProductFacets').mockResolvedValue({ materials: [], colors: [], models: ['P1S', 'X1C'] });
    suggest = vi.spyOn(api, 'suggestStock').mockImplementation(async (items) => ({
      items: items.map((item) =>
        item.product_id === 1
          ? suggestion({ from_finished: Math.min(2, item.quantity), from_kits: Math.min(3, Math.max(0, item.quantity - 2)), to_print: Math.max(0, item.quantity - 5) })
          : suggestion({ product_id: item.product_id, finished_free: 0, kits_free: 1, from_finished: 0, from_kits: 1, to_print: item.quantity - 1, position_id: null, position_code: null }),
      ),
    }));
  });

  it('lists a server page, searched and filtered on the server', async () => {
    render(<AddToOrderDialog order={ORDER} onClose={() => {}} />);
    expect(await screen.findByTestId('add-product-1')).toBeInTheDocument();
    expect(getPage).toHaveBeenLastCalledWith({ page: 1, per_page: 24, active: true });
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'pip' } });
    await waitFor(() => expect(getPage).toHaveBeenLastCalledWith({ page: 1, per_page: 24, active: true, q: 'pip' }));
    fireEvent.change(screen.getByLabelText('Printer model'), { target: { value: 'X1C' } });
    await waitFor(() =>
      expect(getPage).toHaveBeenLastCalledWith({ page: 1, per_page: 24, active: true, q: 'pip', model: 'X1C' }),
    );
  });

  it('a ticked row gets its quantity, configuration, material and colour', async () => {
    render(<AddToOrderDialog order={ORDER} onClose={() => {}} />);
    const row = await screen.findByTestId('add-product-1');
    expect(within(row).getByLabelText('Quantity')).toBeDisabled();
    fireEvent.click(within(row).getByRole('checkbox'));
    expect(within(row).getByLabelText('Quantity')).toBeEnabled();
    expect(await within(row).findByLabelText('Tail')).toHaveValue('100');
    expect(within(row).getByLabelText('Material')).toHaveValue('');
    expect(within(row).getByRole('option', { name: 'PETG' })).toBeInTheDocument();
  });

  it('the stock is the server proposal — ready units first, then kits, the rest to print', async () => {
    render(<AddToOrderDialog order={ORDER} onClose={() => {}} />);
    const row = await screen.findByTestId('add-product-1');
    fireEvent.click(within(row).getByRole('checkbox'));
    fireEvent.change(within(row).getByLabelText('Quantity'), { target: { value: '6' } });
    await waitFor(() => expect(within(row).getByLabelText('Ready units')).toHaveValue(2));
    expect(within(row).getByLabelText('Kits')).toHaveValue(3);
    expect(within(row).getByText('to print: 1')).toBeInTheDocument();
    expect(within(row).getByText('picked automatically')).toBeInTheDocument();
    const last = suggest.mock.calls.at(-1)?.[0] ?? [];
    expect(last).toEqual([{ product_id: 1, options: [], quantity: 6 }]);
  });

  it('a changed number is the operator’s until «pick» hands it back', async () => {
    render(<AddToOrderDialog order={ORDER} onClose={() => {}} />);
    const row = await screen.findByTestId('add-product-1');
    fireEvent.click(within(row).getByRole('checkbox'));
    fireEvent.change(within(row).getByLabelText('Quantity'), { target: { value: '6' } });
    await waitFor(() => expect(within(row).getByLabelText('Ready units')).toHaveValue(2));
    fireEvent.change(within(row).getByLabelText('Ready units'), { target: { value: '1' } });
    expect(within(row).getByLabelText('Ready units')).toHaveValue(1);
    fireEvent.click(within(row).getByRole('button', { name: 'pick' }));
    await waitFor(() => expect(within(row).getByLabelText('Ready units')).toHaveValue(2));
  });

  it('the selection survives another page and another search', async () => {
    render(<AddToOrderDialog order={ORDER} onClose={() => {}} />);
    fireEvent.click(within(await screen.findByTestId('add-product-1')).getByRole('checkbox'));
    fireEvent.click(screen.getByRole('button', { name: /next page/i }));
    expect(await screen.findByTestId('add-product-3')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /previous page/i }));
    expect(within(await screen.findByTestId('add-product-1')).getByRole('checkbox')).toBeChecked();
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'x' } });
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: '' } });
    expect(within(await screen.findByTestId('add-product-1')).getByRole('checkbox')).toBeChecked();
  });

  it('marks a draft, and shows what an unticked row has in stock', async () => {
    render(<AddToOrderDialog order={ORDER} onClose={() => {}} />);
    const lampRow = await screen.findByTestId('add-product-2');
    // WS-13 E5 C03: a draft is said in the row's second line, not by a badge.
    expect(within(lampRow).getByText(/· draft/)).toBeInTheDocument();
    // The Pipe has groups: its ready units count every configuration (C04).
    expect(within(screen.getByTestId('add-product-1')).getByText('ready 2 (all configs) · kits 3')).toBeInTheDocument();
    expect(within(lampRow).getByText('ready 0 · kits 1')).toBeInTheDocument();
  });

  it('an order that is not active takes nothing from stock', async () => {
    render(<AddToOrderDialog order={{ ...ORDER, active: false }} onClose={() => {}} />);
    const row = await screen.findByTestId('add-product-1');
    fireEvent.click(within(row).getByRole('checkbox'));
    expect(within(row).queryByLabelText('Ready units')).not.toBeInTheDocument();
    expect(suggest).not.toHaveBeenCalled();
  });

  it('says so when the stock could not be asked', async () => {
    // Final review M11: a failed proposal is said, not left as «of —».
    suggest.mockRejectedValue(new Error('boom'));
    render(<AddToOrderDialog order={ORDER} onClose={() => {}} />);
    const row = await screen.findByTestId('add-product-1');
    fireEvent.click(within(row).getByRole('checkbox'));
    expect(await within(row).findByText('Could not read the stock')).toBeInTheDocument();
  });
});

/**
 * Which combinations an order can take (spec product-part-variant-combinations п. 25): the jar —
 * Weight (20): 400 (standard) / 600; Lid (21): screw (standard) / snap.
 */
const jar: ProductListItem = {
  ...pipe,
  id: 4,
  code: 'PR-0004',
  name: 'Jar',
  variant_group_names: ['Weight', 'Lid'],
  variant_groups: [
    { id: 20, name: 'Weight', default_option_id: 200, options: [{ id: 200, name: '400' }, { id: 201, name: '600' }] },
    { id: 21, name: 'Lid', default_option_id: 210, options: [{ id: 210, name: 'screw' }, { id: 211, name: 'snap' }] },
  ],
};
const JAR_NAMES: Record<number, string> = { 200: '400', 201: '600', 210: 'screw', 211: 'snap' };

/** The server's answer for a choice of the jar, when only the combinations in `open` can be ordered. */
function jarAnswer(open: string[], choices: Record<number, number>): CombinationState {
  const full = { 20: 200, 21: 210, ...choices };
  const isOpen = (c: Record<number, number>) => open.includes(`${c[20]},${c[21]}`);
  const axes: [number, number[]][] = [
    [20, [200, 201]],
    [21, [210, 211]],
  ];
  return {
    available: isOpen(full),
    reasons: isOpen(full) ? [] : [{ code: 'option_without_part', group: 'Lid', option: JAR_NAMES[full[21]], part: null }],
    options: axes.flatMap(([group, values]) =>
      values.map((value) => ({
        group_id: group,
        option_id: value,
        available_here: isOpen({ ...full, [group]: value }),
        available_anywhere: open.some((k) => k.split(',')[group === 20 ? 0 : 1] === String(value)),
      })),
    ),
  };
}

describe('the products tab of «Add to order» — which combinations an order can take', () => {
  let describeCombination: ReturnType<typeof vi.spyOn>;
  let add: ReturnType<typeof vi.spyOn>;
  const openOnly = (...open: string[]) =>
    describeCombination.mockImplementation(async (_id: number, choices: Record<number, number>) => jarAnswer(open, choices));

  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, 'getProductsPaged').mockImplementation(async (params) =>
      params.page === 2 ? pageOf([vase], 30, 2) : pageOf([jar, lamp], 30),
    );
    vi.spyOn(api, 'getProductCategories').mockResolvedValue([]);
    vi.spyOn(api, 'getProductFacets').mockResolvedValue({ materials: [], colors: [], models: [] });
    vi.spyOn(api, 'suggestStock').mockImplementation(async (items) => ({
      items: items.map((item) => suggestion({ product_id: item.product_id })),
    }));
    add = vi.spyOn(api, 'addOrderLines').mockImplementation(async (_order, lines) => ({
      order: { id: 5 } as never,
      results: lines.map((_line, i) => ({ line_id: 40 + i, asked_finished: 0, got_finished: 0, asked_kits: 0, got_kits: 0 })),
    }));
    describeCombination = vi.spyOn(api, 'describeCombination');
    openOnly('200,210', '201,211');
  });

  const tickJar = async () => {
    const row = await screen.findByTestId('add-product-4');
    fireEvent.click(within(row).getByRole('checkbox'));
    return row;
  };
  const addButton = () => screen.getByRole('button', { name: 'Add lines (1)' });

  it('marks a value that closes the combination and keeps it clickable', async () => {
    render(<AddToOrderDialog order={ORDER} onClose={() => {}} />);
    const row = await tickJar();
    // 600 + screw is closed; 600 + snap is open.
    const marked = await within(row).findByRole('option', { name: '600 — not with this choice' });
    expect(marked).toBeEnabled();
    expect(within(row).getByRole('option', { name: '400' })).toBeEnabled();
    await waitFor(() => expect(addButton()).toBeEnabled());
    fireEvent.change(within(row).getByLabelText('Weight'), { target: { value: '201' } });
    expect(within(row).getByLabelText('Weight')).toHaveValue('201');
    await waitFor(() => expect(describeCombination).toHaveBeenLastCalledWith(4, { 20: 201 }));
  });

  it('disables a value no available combination contains', async () => {
    openOnly('200,210');
    render(<AddToOrderDialog order={ORDER} onClose={() => {}} />);
    const row = await tickJar();
    expect(await within(row).findByRole('option', { name: '600 — not available' })).toBeDisabled();
    expect(within(row).getByRole('option', { name: 'snap — not available' })).toBeDisabled();
    // What is chosen is never off.
    expect(within(row).getByRole('option', { name: 'screw' })).toBeEnabled();
  });

  it('disables Add on a closed choice and says why, then lets two groups leave it', async () => {
    render(<AddToOrderDialog order={ORDER} onClose={() => {}} />);
    const row = await tickJar();
    await waitFor(() => expect(addButton()).toBeEnabled());
    fireEvent.change(within(row).getByLabelText('Weight'), { target: { value: '201' } });
    expect(await within(row).findByText('This combination can’t be ordered: no part for «screw» in «Lid»')).toBeInTheDocument();
    expect(screen.getByText('Can’t be ordered in the chosen combination: Jar')).toBeInTheDocument();
    expect(addButton()).toBeDisabled();
    // The disabled «Add» names the note that says why.
    expect(addButton()).toHaveAccessibleDescription('Can’t be ordered in the chosen combination: Jar');
    // A pick on another page still counts.
    fireEvent.click(screen.getByRole('button', { name: /next page/i }));
    expect(await screen.findByTestId('add-product-3')).toBeInTheDocument();
    expect(addButton()).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: /previous page/i }));
    const back = await screen.findByTestId('add-product-4');
    fireEvent.change(within(back).getByLabelText('Lid'), { target: { value: '211' } });
    await waitFor(() => expect(addButton()).toBeEnabled());
    expect(within(back).queryByTestId('combination-note')).not.toBeInTheDocument();
    expect(addButton()).not.toHaveAccessibleDescription();
    fireEvent.click(addButton());
    await waitFor(() =>
      expect(add).toHaveBeenCalledWith(5, [expect.objectContaining({ product_id: 4, choices: { 20: 201, 21: 211 } })]),
    );
  });

  it('keeps Add off while a combination is being asked', async () => {
    describeCombination.mockImplementation(() => new Promise(() => {}));
    render(<AddToOrderDialog order={ORDER} onClose={() => {}} />);
    await tickJar();
    expect(await screen.findByText('Checking the products’ combinations…')).toBeInTheDocument();
    expect(addButton()).toBeDisabled();
    expect(addButton()).toHaveAccessibleDescription('Checking the products’ combinations…');
  });

  it('asks nothing for a product without groups', async () => {
    render(<AddToOrderDialog order={ORDER} onClose={() => {}} />);
    const row = await screen.findByTestId('add-product-2');
    fireEvent.click(within(row).getByRole('checkbox'));
    await waitFor(() => expect(addButton()).toBeEnabled());
    expect(describeCombination).not.toHaveBeenCalled();
  });

  it('shows the 409 combination_unavailable sentence from the server, and asks again', async () => {
    const refusal = new ApiError('That combination is not available', 409, 'combination_unavailable');
    refusal.reasons = [{ code: 'part_not_sliced', group: null, option: null, part: 'jar body 400' }];
    add.mockRejectedValue(refusal);
    render(<AddToOrderDialog order={ORDER} onClose={() => {}} />);
    await tickJar();
    await waitFor(() => expect(addButton()).toBeEnabled());
    const asked = describeCombination.mock.calls.length;
    fireEvent.click(addButton());
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('That combination is not available');
    expect(alert).toHaveTextContent('«jar body 400» is not sliced');
    await waitFor(() => expect(describeCombination.mock.calls.length).toBeGreaterThan(asked));
  });
});
