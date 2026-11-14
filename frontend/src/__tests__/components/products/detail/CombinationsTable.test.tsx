/**
 * The combinations table of the product page's variants section (spec
 * product-part-variant-combinations п. 24): a column per group, then «State» — available, or
 * what is missing, one sentence per reason. The server pages, counts and decides; the table
 * only composes the sentences.
 */
import type { ReactElement } from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, fireEvent, render as renderBare, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render } from '../../../utils';
import { api } from '../../../../api/client';
import type { CombinationRow, Product, ProductCombinationsPage } from '../../../../api/client';
import { COMBINATIONS_PER_PAGE, CombinationsTable } from '../../../../components/products/detail/CombinationsTable';

const product = {
  id: 7,
  variant_groups: [
    { id: 1, name: 'Kind', position: 0, default_option_id: 11, options: [] },
    { id: 2, name: 'Weight', position: 1, default_option_id: 21, options: [] },
  ],
} as unknown as Pick<Product, 'id' | 'variant_groups'>;

function row(kind: [number, string], weight: [number, string], over: Partial<CombinationRow> = {}): CombinationRow {
  return {
    choices: [
      { group_id: 1, group: 'Kind', option_id: kind[0], option: kind[1] },
      { group_id: 2, group: 'Weight', option_id: weight[0], option: weight[1] },
    ],
    available: true,
    reasons: [],
    ...over,
  };
}

const closed = row([12, 'mesh'], [23, '1200'], {
  available: false,
  reasons: [
    { code: 'option_without_part', group: 'Kind', option: 'mesh', part: null },
    { code: 'part_not_sliced', group: null, option: null, part: 'Body 1200' },
  ],
});

function page(items: CombinationRow[], total = items.length, current = 1): ProductCombinationsPage {
  return { total, page: current, items };
}

const rows = () => screen.getAllByTestId('combination-row');

describe('CombinationsTable', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('asks the server for the first page; a column per group, then «State»', async () => {
    const get = vi.spyOn(api, 'getProductCombinations').mockResolvedValue(page([row([11, 'solid'], [21, '400'])]));
    render(<CombinationsTable product={product} />);
    await screen.findByTestId('combination-row');
    expect(get).toHaveBeenCalledWith(7, 1, COMBINATIONS_PER_PAGE);
    expect(screen.getAllByRole('columnheader').map((h) => h.textContent)).toEqual(['Kind', 'Weight', 'State']);
    expect(within(rows()[0]).getAllByRole('cell').map((c) => c.textContent)).toEqual(['solid', '400', 'available']);
  });

  it('a closed row says every reason, each its own sentence', async () => {
    vi.spyOn(api, 'getProductCombinations').mockResolvedValue(page([row([11, 'solid'], [21, '400']), closed]));
    render(<CombinationsTable product={product} />);
    await screen.findAllByTestId('combination-row');
    const state = within(rows()[1]).getAllByRole('cell')[2];
    expect(rows()[1]).toHaveAttribute('data-available', 'false');
    expect(within(state).getByText('no part for «mesh» in «Kind»')).toBeInTheDocument();
    expect(within(state).getByText('«Body 1200» is not sliced')).toBeInTheDocument();
    expect(state).not.toHaveTextContent('available');
  });

  it('a product without groups renders nothing and asks nothing', () => {
    const get = vi.spyOn(api, 'getProductCombinations');
    const { container } = render(<CombinationsTable product={{ id: 7, variant_groups: [] }} />);
    expect(container).toBeEmptyDOMElement();
    expect(get).not.toHaveBeenCalled();
  });

  it('pages on the server: the next page is asked by its number, the count is the whole table’s', async () => {
    const get = vi
      .spyOn(api, 'getProductCombinations')
      .mockImplementation(async (_id, p) =>
        p === 1 ? page([row([11, 'solid'], [21, '400'])], 120, 1) : page([closed], 120, p),
      );
    render(<CombinationsTable product={product} />);
    await screen.findByTestId('combination-row');
    expect(screen.getByText(`Showing 1-${COMBINATIONS_PER_PAGE} of 120 combinations`)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Next page' }));
    await waitFor(() => expect(get).toHaveBeenLastCalledWith(7, 2, COMBINATIONS_PER_PAGE));
    await waitFor(() => expect(rows()[0]).toHaveAttribute('data-available', 'false'));
  });

  it('another product opens on its own first page and never shows the previous product’s rows', async () => {
    let releaseOther: (p: ProductCombinationsPage) => void = () => {};
    const get = vi.spyOn(api, 'getProductCombinations').mockImplementation(async (id, p) => {
      if (id === 8) return new Promise<ProductCombinationsPage>((resolve) => (releaseOther = resolve));
      return p === 1 ? page([row([11, 'solid'], [21, '400'])], 120, 1) : page([closed], 120, p);
    });
    // One client across the rerender (the shared wrapper makes a new one on every render, which
    // would forget the previous answer this test is about).
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
    const inClient = (ui: ReactElement) => <QueryClientProvider client={client}>{ui}</QueryClientProvider>;
    const view = renderBare(inClient(<CombinationsTable product={product} />));
    await screen.findByTestId('combination-row');
    fireEvent.click(screen.getByRole('button', { name: 'Next page' }));
    await waitFor(() => expect(rows()[0]).toHaveAttribute('data-available', 'false'));

    view.rerender(inClient(<CombinationsTable product={{ ...product, id: 8 }} />));
    await waitFor(() => expect(get).toHaveBeenLastCalledWith(8, 1, COMBINATIONS_PER_PAGE));
    // While its own page is read, nothing of product 7 stays on screen.
    expect(screen.queryByTestId('combination-row')).not.toBeInTheDocument();
    await act(async () => releaseOther(page([row([31, 'glass'], [41, '200'])], 1, 1)));
    const [first] = await screen.findAllByTestId('combination-row');
    expect(within(first).getAllByRole('cell').map((c) => c.textContent)).toEqual(['glass', '200', 'available']);
  });

  it('a failed read says so with a retry, never an empty table', async () => {
    let fail: (e: Error) => void = () => {};
    vi.spyOn(api, 'getProductCombinations')
      .mockReturnValueOnce(new Promise((_, reject) => (fail = reject)))
      .mockResolvedValueOnce(page([row([11, 'solid'], [21, '400'])]));
    render(<CombinationsTable product={product} />);
    await act(async () => fail(new Error('boom')));
    expect(await screen.findByText('Could not load the combinations')).toBeInTheDocument();
    expect(screen.queryByTestId('combination-row')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByTestId('combination-row')).toBeInTheDocument();
  });
});
