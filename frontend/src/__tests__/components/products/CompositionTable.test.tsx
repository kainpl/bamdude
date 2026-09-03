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
import { screen, fireEvent, waitFor, within } from '@testing-library/react';
import { render } from '../../utils';
import { api } from '../../../api/client';
import type { Product, ProductPart } from '../../../api/client';
import { CompositionTable } from '../../../components/products/CompositionTable';

const product = {
  // Product 7, parts 1-3: the ids must DIFFER, or an argument swap in
  // `addProductPartAlias(productId, partId, key)` passes both assertions below
  // while sending the pair the wrong way round.
  id: 7,
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
    await waitFor(() => expect(add).toHaveBeenCalledWith(7, 1, 'Body v2.stl'));

    fireEvent.click(screen.getByTestId('part-1-alias-remove-body.stl'));
    await waitFor(() => expect(remove).toHaveBeenCalledWith(7, 1, 'body.stl'));
  });

  it('a 409 on alias add is reported as a toast', async () => {
    vi.spyOn(api, 'addProductPartAlias').mockRejectedValue(new Error('Alias already belongs to another part'));
    render(<CompositionTable product={product} canEdit />);

    fireEvent.click(screen.getByTestId('part-1-alias-add'));
    fireEvent.change(screen.getByTestId('part-1-alias-input'), { target: { value: 'cube' } });
    fireEvent.keyDown(screen.getByTestId('part-1-alias-input'), { key: 'Enter' });

    expect(await screen.findByText(/already belongs/i)).toBeInTheDocument();
  });

  it('puts the server value back when an inline edit is not sendable', () => {
    // The inputs are uncontrolled and keyed on the server's value, so nothing
    // re-renders when the patch is skipped — a cleared name or a fractional
    // quantity would sit there looking saved until some later refetch happened
    // to remount the row.
    const patch = vi.spyOn(api, 'updateProductPart');
    render(<CompositionTable product={product} canEdit />);

    const row = within(screen.getByTestId('part-1-row'));
    const name = row.getByLabelText('Part') as HTMLInputElement;
    fireEvent.change(name, { target: { value: '   ' } });
    fireEvent.blur(name);
    expect(name.value).toBe('flask body');

    const qty = row.getByLabelText('Per unit') as HTMLInputElement;
    fireEvent.change(qty, { target: { value: '2.5' } });
    fireEvent.blur(qty);
    expect(qty.value).toBe('1');

    expect(patch).not.toHaveBeenCalled();
  });

  it('purchased parts never offer an alias input', () => {
    render(<CompositionTable product={product} canEdit />);
    expect(screen.queryByTestId('part-3-alias-add')).not.toBeInTheDocument();
  });

  it('keeps the alias input open and empty after one lands', async () => {
    // Parts routinely carry several aliases; closing after each one made the
    // second cost a fresh click on "+ alias".
    vi.spyOn(api, 'addProductPartAlias').mockResolvedValue({} as ProductPart);
    render(<CompositionTable product={product} canEdit />);

    fireEvent.click(screen.getByTestId('part-1-alias-add'));
    const input = screen.getByTestId('part-1-alias-input') as HTMLInputElement;
    fireEvent.change(input, { target: { value: 'Body v2.stl' } });
    fireEvent.keyDown(input, { key: 'Enter' });

    await waitFor(() => expect((screen.getByTestId('part-1-alias-input') as HTMLInputElement).value).toBe(''));
  });

  it('merging sends the picked part as the target and this row as the source', async () => {
    // `mergeProductPart(productId, target, source)` deletes the SOURCE, so an
    // argument swap here quietly deletes the wrong part.
    const merge = vi.spyOn(api, 'mergeProductPart').mockResolvedValue({} as never);
    render(<CompositionTable product={product} canEdit />);

    const row = within(screen.getByTestId('part-1-row'));
    fireEvent.change(row.getByLabelText('Merge into…'), { target: { value: '2' } });

    fireEvent.click(await screen.findByRole('button', { name: /^confirm$/i }));
    await waitFor(() => expect(merge).toHaveBeenCalledWith(7, 2, 1));
  });

  it('edits a purchased part price, and a cleared box takes the price off', async () => {
    // The only way to correct a price used to be delete + re-add, and deleting
    // a purchased part deletes every order's acquisitions against it.
    const patch = vi.spyOn(api, 'updateProductPart').mockResolvedValue({} as ProductPart);
    render(<CompositionTable product={product} canEdit />);

    const row = within(screen.getByTestId('part-3-row'));
    const price = row.getByLabelText('Unit price') as HTMLInputElement;
    fireEvent.change(price, { target: { value: '1.25' } });
    fireEvent.blur(price);
    await waitFor(() => expect(patch).toHaveBeenCalledWith(7, 3, { unit_price: 1.25 }));

    patch.mockClear();
    fireEvent.change(price, { target: { value: '' } });
    fireEvent.blur(price);
    // A blank price is a real null — "no price", not "leave it alone".
    await waitFor(() => expect(patch).toHaveBeenCalledWith(7, 3, { unit_price: null }));
  });

  it('edits a purchased part sourcing url and remarks one field at a time', async () => {
    const patch = vi.spyOn(api, 'updateProductPart').mockResolvedValue({} as ProductPart);
    render(<CompositionTable product={product} canEdit />);

    const row = within(screen.getByTestId('part-3-row'));
    const url = row.getByLabelText('Where to buy') as HTMLInputElement;
    fireEvent.change(url, { target: { value: 'https://elsewhere' } });
    fireEvent.blur(url);
    await waitFor(() => expect(patch).toHaveBeenCalledWith(7, 3, { sourcing_url: 'https://elsewhere' }));

    patch.mockClear();
    const remarks = row.getByLabelText('Remarks') as HTMLInputElement;
    fireEvent.change(remarks, { target: { value: 'stainless only' } });
    fireEvent.blur(remarks);
    await waitFor(() => expect(patch).toHaveBeenCalledWith(7, 3, { remarks: 'stainless only' }));
  });

  it('restores a rejected purchased-part price instead of leaving it on screen', () => {
    const patch = vi.spyOn(api, 'updateProductPart');
    render(<CompositionTable product={product} canEdit />);

    const price = within(screen.getByTestId('part-3-row')).getByLabelText('Unit price') as HTMLInputElement;
    fireEvent.change(price, { target: { value: '-3' } });
    fireEvent.blur(price);

    expect(price.value).toBe('0.05');
    expect(patch).not.toHaveBeenCalled();
  });

  it('warns that deleting a purchased part takes the acquisitions with it', async () => {
    render(<CompositionTable product={product} canEdit />);

    fireEvent.click(within(screen.getByTestId('part-3-row')).getByLabelText('Delete part'));
    expect(await screen.findByText(/recorded as acquired against it/i)).toBeInTheDocument();
  });

  it('the printed-part delete confirm says nothing about acquisitions', async () => {
    render(<CompositionTable product={product} canEdit />);

    fireEvent.click(within(screen.getByTestId('part-1-row')).getByLabelText('Delete part'));
    expect(await screen.findByText(/remove .*flask body.* from the composition/i)).toBeInTheDocument();
    expect(screen.queryByText(/recorded as acquired against it/i)).not.toBeInTheDocument();
  });
});
