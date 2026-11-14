/**
 * WS-13 E5 C — the «Products» tab of «Add to order»: the row as the mockup draws it,
 * its groups from the catalog row (H01), a group without a standard option left
 * unchosen (R11), and a stock proposal that never passes another configuration's
 * numbers off as this one's (R02).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { focusManager } from '@tanstack/react-query';
import { render } from '../../../utils';
import { api, ApiError } from '../../../../api/client';
import type { StockSuggestItem } from '../../../../api/client';
import { AddToOrderDialog } from '../../../../components/projects/add-to-order/AddToOrderDialog';
import { enabledButton, lamp, pageOf, pipe, suggestion, vase } from './fixtures';

const ORDER = { id: 5, code: 'OR-0005', name: 'Flasks for Acme', active: true };

/** Vase: «Mount» has no standard option — wall (21) or DIN (22). */
const mountedVase = {
  ...vase,
  variant_groups: [{ id: 12, name: 'Mount', default_option_id: null, options: [{ id: 21, name: 'wall' }, { id: 22, name: 'DIN' }] }],
  variant_group_names: ['Mount'],
};

type Answer = { items: ReturnType<typeof suggestion>[] };

describe('the products tab of «Add to order» (WS-13 E5 C)', () => {
  let suggest: ReturnType<typeof vi.spyOn>;
  let add: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, 'getProductsPaged').mockResolvedValue(pageOf([pipe, lamp, mountedVase], 3));
    vi.spyOn(api, 'getProduct');
    vi.spyOn(api, 'getProductCategories').mockResolvedValue([]);
    vi.spyOn(api, 'getProductFacets').mockResolvedValue({ materials: [], colors: [], models: [] });
    suggest = vi.spyOn(api, 'suggestStock').mockImplementation(async (items: StockSuggestItem[]) => ({
      items: items.map((item) => suggestion({ product_id: item.product_id })),
    }));
    add = vi.spyOn(api, 'addOrderLines').mockImplementation(async (_order, lines) => ({
      order: { id: 5, lines: [{ id: 40, product_name: 'Pipe' }] } as never,
      results: lines.map((_line, i) => ({ line_id: 40 + i, asked_finished: 0, got_finished: 0, asked_kits: 0, got_kits: 0 })),
    }));
  });

  const open = () => render(<AddToOrderDialog order={ORDER} onClose={() => {}} />);
  const tick = async (id: number) => {
    const row = await screen.findByTestId(`add-product-${id}`);
    fireEvent.click(within(row).getByRole('checkbox'));
    return row;
  };

  it('heads its six columns in the mockup’s order', async () => {
    open();
    await screen.findByTestId('add-product-1');
    const heads = [...within(screen.getByRole('tabpanel')).getAllByRole('columnheader')].map((h) => h.textContent);
    // The tick column has no visible title, only one for a screen reader.
    expect(heads).toEqual(['Pick', 'Product', 'Configuration', 'Quantity', 'From stock', 'Material / colour']);
  });

  it('draws the product: code · SKU · category, a draft in words, the model chips', async () => {
    open();
    const pipeRow = await screen.findByTestId('add-product-1');
    expect(within(pipeRow).getByText('PP-1')).toHaveClass('font-mono');
    expect(within(pipeRow).getByText('P1S')).toBeInTheDocument();
    const lampRow = screen.getByTestId('add-product-2');
    expect(within(lampRow).getByText(/· draft/)).toBeInTheDocument();
    expect(within(lampRow).queryByText('Draft')).not.toBeInTheDocument();
  });

  it('an unticked row names its options, its stock across configurations and its materials', async () => {
    open();
    const pipeRow = await screen.findByTestId('add-product-1');
    expect(within(pipeRow).getByText('Tail: straight / angled')).toBeInTheDocument();
    expect(within(pipeRow).getByText('ready 2 (all configs) · kits 3')).toBeInTheDocument();
    expect(within(pipeRow).getByLabelText('Quantity')).toBeDisabled();
    expect(within(pipeRow).getByLabelText('Quantity')).toHaveValue(1);
    expect(within(pipeRow).getByText('PETG')).toBeInTheDocument();
    expect(within(screen.getByTestId('add-product-2')).getByText('no variants')).toBeInTheDocument();
  });

  it('a ticked row takes its groups from the catalog row — no product is read (H01)', async () => {
    open();
    const row = await tick(1);
    const tail = within(row).getByLabelText('Tail');
    expect(tail).toHaveValue('100');
    expect([...tail.querySelectorAll('option')].map((o) => o.textContent)).toEqual(['straight', 'angled']);
    expect(api.getProduct).not.toHaveBeenCalled();
  });

  it('leaves a group without a standard option unchosen — in the field, the proposal and the batch (R11)', async () => {
    open();
    const row = await tick(3);
    const mount = within(row).getByLabelText('Mount');
    expect(mount).toHaveValue('');
    expect(within(mount).getByRole('option', { name: 'No choice — no standard option' })).toBeInTheDocument();
    await waitFor(() => expect(suggest.mock.calls.at(-1)?.[0]).toEqual([{ product_id: 3, options: [], quantity: 1 }]));

    fireEvent.change(mount, { target: { value: '22' } });
    expect(mount).toHaveValue('22');
    await waitFor(() => expect(suggest.mock.calls.at(-1)?.[0]).toEqual([{ product_id: 3, options: [22], quantity: 1 }]));

    fireEvent.change(mount, { target: { value: '' } });
    await waitFor(() => expect(suggest.mock.calls.at(-1)?.[0]).toEqual([{ product_id: 3, options: [], quantity: 1 }]));
    fireEvent.click(await enabledButton('Add lines (1)'));
    await waitFor(() => expect(add).toHaveBeenCalled());
    expect(add.mock.calls[0][1][0]).toEqual(expect.objectContaining({ product_id: 3, choices: {} }));
  });

  it('while another configuration is asked, shows none of the old one’s numbers (R02)', async () => {
    let releaseB: (a: Answer) => void = () => {};
    suggest.mockImplementation(async (items: StockSuggestItem[]) =>
      items[0].options?.includes(101)
        ? new Promise<Answer>((resolve) => {
            releaseB = resolve;
          })
        : { items: [suggestion({ finished_free: 2, kits_free: 3, from_finished: 1, from_kits: 0, to_print: 0 })] },
    );
    open();
    const row = await tick(1);
    expect(await within(row).findByText('of 2')).toBeInTheDocument();
    expect(within(row).getByText('picked automatically')).toBeInTheDocument();

    fireEvent.change(within(row).getByLabelText('Tail'), { target: { value: '101' } });
    expect(within(row).getByText('reading the stock…')).toBeInTheDocument();
    expect(within(row).queryByText('of 2')).not.toBeInTheDocument();
    expect(within(row).queryByText('picked automatically')).not.toBeInTheDocument();
    expect(within(row).queryByText(/to print/)).not.toBeInTheDocument();

    await waitFor(() => expect(suggest).toHaveBeenCalledTimes(2));
    expect(within(row).getByText('reading the stock…')).toBeInTheDocument();
    releaseB({ items: [suggestion({ finished_free: 7, kits_free: 0, from_finished: 1, from_kits: 0, to_print: 0 })] });
    expect(await within(row).findByText('of 7')).toBeInTheDocument();
  });

  it('a failed proposal for the new configuration says so and asks again (R02)', async () => {
    suggest.mockImplementation(async (items: StockSuggestItem[]) => {
      if (items[0].options?.includes(101)) throw new ApiError('boom', 500);
      return { items: [suggestion({})] };
    });
    open();
    const row = await tick(1);
    await within(row).findByText('of 2');
    fireEvent.change(within(row).getByLabelText('Tail'), { target: { value: '101' } });
    expect(await within(row).findByText('Could not read the stock')).toBeInTheDocument();
    expect(within(row).queryByText('of 2')).not.toBeInTheDocument();
    const calls = suggest.mock.calls.length;
    fireEvent.click(within(row).getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(suggest.mock.calls.length).toBeGreaterThan(calls));
  });

  it('keeps its answer, with a note, when only re-reading the same question failed (R02)', async () => {
    let failNext = false;
    suggest.mockImplementation(async (items: StockSuggestItem[]) => {
      if (failNext) throw new ApiError('boom', 500);
      return { items: items.map((item) => suggestion({ product_id: item.product_id })) };
    });
    open();
    const row = await tick(1);
    await within(row).findByText('of 2');
    // The same question asked again — the window came back into focus — and refused.
    failNext = true;
    act(() => {
      focusManager.setFocused(false);
      focusManager.setFocused(true);
    });
    expect(await within(row).findByText('The stock may have changed — it could not be re-read')).toBeInTheDocument();
    expect(within(row).getByText('of 2')).toBeInTheDocument();
    focusManager.setFocused(undefined);
  });

  it('an answer to its own question that leaves the row out is a failure, not an endless wait (R02)', async () => {
    suggest.mockImplementation(async () => ({ items: [] }));
    open();
    const row = await tick(1);
    expect(await within(row).findByText('Could not read the stock')).toBeInTheDocument();
    expect(within(row).queryByText('reading the stock…')).not.toBeInTheDocument();
  });

  it('a row the operator set keeps its numbers while the proposal is re-read (R02)', async () => {
    suggest.mockImplementation(async (items: StockSuggestItem[]) =>
      items[0].options?.includes(101) ? new Promise(() => {}) : { items: [suggestion({})] },
    );
    open();
    const row = await tick(1);
    fireEvent.change(within(row).getByLabelText('Quantity'), { target: { value: '3' } });
    await within(row).findByText('of 2');
    fireEvent.change(within(row).getByLabelText('Ready units'), { target: { value: '1' } });
    fireEvent.change(within(row).getByLabelText('Tail'), { target: { value: '101' } });
    expect(within(row).getByText('reading the stock…')).toBeInTheDocument();
    expect(within(row).getByLabelText('Ready units')).toHaveValue(1);
    expect(within(row).getByText('to print: 1')).toBeInTheDocument();
  });

  it('adding while the proposal is re-read sends «auto» and warns about nothing it did not show (R02)', async () => {
    suggest.mockImplementation(async (items: StockSuggestItem[]) =>
      items[0].quantity === 6 ? new Promise(() => {}) : { items: [suggestion({ from_finished: 1, from_kits: 0, to_print: 0 })] },
    );
    add.mockResolvedValue({
      order: { id: 5, lines: [{ id: 40, product_name: 'Pipe' }] } as never,
      results: [{ line_id: 40, asked_finished: 0, got_finished: 0, asked_kits: 0, got_kits: 0 }],
    });
    open();
    const row = await tick(1);
    await within(row).findByText('of 2');
    fireEvent.change(within(row).getByLabelText('Quantity'), { target: { value: '6' } });
    fireEvent.click(await enabledButton('Add lines (1)'));
    await waitFor(() => expect(add).toHaveBeenCalled());
    expect(add.mock.calls[0][1][0]).toEqual(expect.objectContaining({ quantity: 6, stock: 'auto' }));
    await waitFor(() => expect(screen.queryByText(/less was taken/)).not.toBeInTheDocument());
  });

  it('says the list is loading, a failed list with retry, and an empty search', async () => {
    const get = vi.spyOn(api, 'getProductsPaged').mockImplementation(() => new Promise(() => {}));
    const { unmount } = open();
    expect(await screen.findByText(/Loading/)).toBeInTheDocument();
    unmount();

    get.mockRejectedValue(new ApiError('boom', 500));
    const second = open();
    expect(await screen.findByText('Could not load the products')).toBeInTheDocument();
    second.unmount();

    get.mockResolvedValue(pageOf([], 0));
    open();
    expect(await screen.findByText('Nothing found')).toBeInTheDocument();
    expect(screen.getByText('Change the search or a filter.')).toBeInTheDocument();
  });
});
