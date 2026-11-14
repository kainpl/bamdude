/**
 * Merging one printed part into another (WS-13 E10 C08, F15): a target chosen
 * from the product's other printed parts, the one that cannot take the source's
 * stock shut with its reason, what the merge does said before it is done — the
 * source's free stock by its number — and every refusal in the dialog.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { render } from '../../utils';
import { api, ApiError } from '../../../api/client';
import type { Product, ProductPart } from '../../../api/client';
import { MergePartDialog } from '../../../components/products/MergePartDialog';

function part(over: Partial<ProductPart> & Pick<ProductPart, 'id' | 'name'>): ProductPart {
  return {
    kind: 'printed',
    name_key: `${over.name.toLowerCase()}.stl`,
    qty_per_unit: 1,
    aliases: [],
    auto: false,
    unit_price: null,
    sourcing_url: null,
    remarks: null,
    sort_order: over.id,
    ignored: false,
    stock_balance: 0,
    variant_option_ids: [],
    ...over,
  };
}

const body = part({ id: 1, name: 'Body', stock_balance: 3 });
const lid = part({ id: 2, name: 'Lid' });
const cube = part({ id: 3, name: 'Cube', qty_per_unit: 0, ignored: true });
const magnet = part({ id: 4, name: 'Magnet', kind: 'purchased' });

const product = { id: 7, code: 'PR-0007', name: 'Flask', parts: [body, lid, cube, magnet] } as unknown as Product;
const noop = () => {};

describe('MergePartDialog', () => {
  beforeEach(() => vi.restoreAllMocks());

  it('names the source and offers only the other printed parts', () => {
    render(<MergePartDialog product={product} source={body} onClose={noop} />);
    const dialog = screen.getByRole('dialog', { name: 'Merge part “Body” into…' });
    const target = within(dialog).getByLabelText('Target part');
    expect(within(target).getAllByRole('option').map((o) => o.textContent)).toEqual([
      'Choose a part',
      'Lid',
      'Cube — this part holds no stock',
    ]);
  });

  it('a target marked «Not counted» is shut while the source has free stock', () => {
    render(<MergePartDialog product={product} source={body} onClose={noop} />);
    expect(screen.getByRole('option', { name: 'Cube — this part holds no stock' })).toBeDisabled();
    expect(screen.getByRole('option', { name: 'Lid' })).toBeEnabled();
  });

  it('a source without stock may go into a marked part — the server says the rest', () => {
    render(<MergePartDialog product={product} source={lid} onClose={noop} />);
    expect(screen.getByRole('option', { name: 'Cube' })).toBeEnabled();
  });

  it('says what the merge does, the free stock by its number', () => {
    render(<MergePartDialog product={product} source={{ ...body, variant_option_ids: [11] }} onClose={noop} />);
    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByText('The names and aliases of “Body” become the target’s aliases.')).toBeInTheDocument();
    expect(within(dialog).getByText('Its free stock — 3 pcs — moves to the target.')).toBeInTheDocument();
    expect(within(dialog).getByText('Saved order configurations count it into the target.')).toBeInTheDocument();
    expect(within(dialog).getByText('Its purchase records are deleted.')).toBeInTheDocument();
    expect(within(dialog).getByText('Its variant binding goes.')).toBeInTheDocument();
  });

  it('a source with no free stock and no binding is not told those move or go', () => {
    render(<MergePartDialog product={product} source={lid} onClose={noop} />);
    const dialog = screen.getByRole('dialog');
    expect(within(dialog).queryByText(/free stock/)).not.toBeInTheDocument();
    expect(within(dialog).queryByText('Its variant binding goes.')).not.toBeInTheDocument();
    expect(within(dialog).getByText('The names and aliases of “Lid” become the target’s aliases.')).toBeInTheDocument();
  });

  it('a bound source is told its binding goes — bound in several groups too', () => {
    render(<MergePartDialog product={product} source={{ ...lid, variant_option_ids: [11, 21] }} onClose={noop} />);
    expect(within(screen.getByRole('dialog')).getByText('Its variant binding goes.')).toBeInTheDocument();
  });

  it('«Merge» waits for a target, then sends this part as the source', async () => {
    const merge = vi.spyOn(api, 'mergeProductPart').mockResolvedValue(lid as never);
    const onClose = vi.fn();
    render(<MergePartDialog product={product} source={body} onClose={onClose} />);
    const button = screen.getByRole('button', { name: 'Merge' });
    expect(button).toBeDisabled();
    fireEvent.change(screen.getByLabelText('Target part'), { target: { value: '2' } });
    fireEvent.click(button);
    await waitFor(() => expect(merge).toHaveBeenCalledWith(7, 2, 1));
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
    expect(await screen.findByText('Parts merged')).toBeInTheDocument();
  });

  it('a refusal stays in the dialog with the focus on «Merge»', async () => {
    vi.spyOn(api, 'mergeProductPart').mockRejectedValue(
      new ApiError('A part that is ordered cannot be merged into one marked as not counted', 409),
    );
    render(<MergePartDialog product={product} source={lid} onClose={noop} />);
    fireEvent.change(screen.getByLabelText('Target part'), { target: { value: '3' } });
    fireEvent.click(screen.getByRole('button', { name: 'Merge' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('cannot be merged into one marked as not counted');
    await waitFor(() => expect(screen.getByRole('button', { name: 'Merge' })).toHaveFocus());
  });

  it('under a request nothing closes it and nothing sends twice — decided in the same frame', async () => {
    const merge = vi.spyOn(api, 'mergeProductPart').mockReturnValue(new Promise(() => {}) as never);
    const onClose = vi.fn();
    render(<MergePartDialog product={product} source={body} onClose={onClose} />);
    fireEvent.change(screen.getByLabelText('Target part'), { target: { value: '2' } });
    const submit = screen.getByRole('button', { name: 'Merge' });
    const cancel = screen.getByRole('button', { name: 'Cancel' });
    act(() => {
      submit.click();
      window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
      cancel.click();
      submit.click();
    });
    await waitFor(() => expect(merge).toHaveBeenCalledTimes(1));
    expect(onClose).not.toHaveBeenCalled();
  });
});

describe('the pictures of the two parts (plan E5, D13)', () => {
  it('shows the source and, once chosen, the target', () => {
    const source = part({ id: 1, name: 'Body', image: { kind: 'render', status: 'ready', v: 'r1' } });
    const target = part({ id: 2, name: 'Lid', image: { kind: 'render', status: 'ready', v: 'r2' } });
    render(<MergePartDialog product={{ ...product, parts: [source, target] } as Product} source={source} onClose={noop} />);
    expect(screen.getByRole('button', { name: 'Show Body larger' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Show Lid larger' })).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Target part'), { target: { value: '2' } });
    expect(screen.getByRole('button', { name: 'Show Lid larger' })).toBeInTheDocument();
  });

  it('says the source’s photo goes only when it has one', () => {
    const withPhoto = part({
      id: 1,
      name: 'Body',
      image_choice: { source: 'photo', instance: null, pin: null, photo: { present: true } },
    });
    const target = part({ id: 2, name: 'Lid' });
    const { rerender } = render(
      <MergePartDialog product={{ ...product, parts: [withPhoto, target] } as Product} source={withPhoto} onClose={noop} />,
    );
    expect(screen.getByText('Its photo is deleted; the target keeps its own picture.')).toBeInTheDocument();
    const plain = part({ id: 1, name: 'Body' });
    rerender(<MergePartDialog product={{ ...product, parts: [plain, target] } as Product} source={plain} onClose={noop} />);
    expect(screen.queryByText('Its photo is deleted; the target keeps its own picture.')).not.toBeInTheDocument();
  });
});
