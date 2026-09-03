/**
 * The composition table is where a product's parts are actually edited, so the
 * four rules that would silently corrupt a product are covered here.
 *
 * ⚠️ `qty_per_unit: 0` is a LEGITIMATE value — "this object is on the plate but
 * is not part of the product" — not a missing number. It has to read as such on
 * screen, or an operator repairs a row that was already right.
 *
 * ⚠️ A purchased part has no aliases at all: the server answers 400 to an alias
 * POST on one, so the UI must not offer the input in the first place.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, fireEvent, waitFor } from '@testing-library/react';
import { render } from '../../utils';
import { api } from '../../../api/client';
import type { Product, ProductPart } from '../../../api/client';
import { CompositionTable } from '../../../components/products/CompositionTable';

const product = {
  id: 1,
  name: 'Flask',
  parts: [
    {
      id: 1,
      kind: 'printed',
      name: 'flask body',
      name_key: 'flask body',
      qty_per_unit: 1,
      aliases: ['flask body', 'body.stl'],
      auto: true,
      unit_price: null,
      sourcing_url: null,
      remarks: null,
      sort_order: 0,
    },
    {
      id: 2,
      kind: 'printed',
      name: 'cube',
      name_key: 'cube',
      qty_per_unit: 0,
      aliases: ['cube'],
      auto: true,
      unit_price: null,
      sourcing_url: null,
      remarks: null,
      sort_order: 1,
    },
    {
      id: 3,
      kind: 'purchased',
      name: 'M3 screw',
      name_key: 'purchased:m3 screw',
      qty_per_unit: 4,
      aliases: [],
      auto: false,
      unit_price: 0.05,
      sourcing_url: 'https://shop',
      remarks: null,
      sort_order: 2,
    },
  ],
} as unknown as Product;

describe('CompositionTable', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('marks a zero-quantity part as not counted and a file-derived part as from file', () => {
    render(<CompositionTable product={product} canEdit />);
    expect(screen.getByTestId('part-2-row').textContent).toMatch(/not counted/i);
    expect(screen.getAllByText(/from file/i)).toHaveLength(2);
  });

  it('adds and removes aliases through the alias endpoints', async () => {
    const add = vi.spyOn(api, 'addProductPartAlias').mockResolvedValue({} as ProductPart);
    const remove = vi.spyOn(api, 'removeProductPartAlias').mockResolvedValue({} as ProductPart);
    render(<CompositionTable product={product} canEdit />);

    fireEvent.click(screen.getByTestId('part-1-alias-add'));
    fireEvent.change(screen.getByTestId('part-1-alias-input'), { target: { value: 'Body v2.stl' } });
    fireEvent.keyDown(screen.getByTestId('part-1-alias-input'), { key: 'Enter' });
    await waitFor(() => expect(add).toHaveBeenCalledWith(1, 1, 'Body v2.stl'));

    fireEvent.click(screen.getByTestId('part-1-alias-remove-body.stl'));
    await waitFor(() => expect(remove).toHaveBeenCalledWith(1, 1, 'body.stl'));
  });

  it('a 409 on alias add is reported as a toast', async () => {
    vi.spyOn(api, 'addProductPartAlias').mockRejectedValue(new Error('Alias already belongs to another part'));
    render(<CompositionTable product={product} canEdit />);

    fireEvent.click(screen.getByTestId('part-1-alias-add'));
    fireEvent.change(screen.getByTestId('part-1-alias-input'), { target: { value: 'cube' } });
    fireEvent.keyDown(screen.getByTestId('part-1-alias-input'), { key: 'Enter' });

    expect(await screen.findByText(/already belongs/i)).toBeInTheDocument();
  });

  it('purchased parts never offer an alias input', () => {
    render(<CompositionTable product={product} canEdit />);
    expect(screen.queryByTestId('part-3-alias-add')).not.toBeInTheDocument();
  });
});
