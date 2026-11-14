/**
 * The part dialog (WS-13 E10 C01–C07, F14): one form for a new part and an edit,
 * written in ONE request — its aliases with it and, on creation, its variant
 * binding (A05). Nothing is written before «Save part»; a refusal stays in the
 * dialog and nothing of it is half-saved.
 *
 * It carries what the inline editors did (Review Focus 1): aliases, «Not
 * counted» only at zero (a count takes the mark off in the same request), the
 * variant binding, the purchased part's price / address / remarks.
 *
 * Its picture (spec part-thumbnails §12.4, §22 p. 2; plan E5, D7): the part first, then the
 * picture; a picture refused after the part was saved keeps the dialog open on the saved part —
 * a retry sends the picture alone, never a second create.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render } from '../../utils';
import { api, ApiError } from '../../../api/client';
import type { Product, ProductPart } from '../../../api/client';
import { ProductPartDialog } from '../../../components/products/ProductPartDialog';

function part(over: Partial<ProductPart> & Pick<ProductPart, 'id' | 'name'>): ProductPart {
  return {
    kind: 'printed',
    name_key: `${over.name.toLowerCase()}.stl`,
    qty_per_unit: 1,
    aliases: [`${over.name.toLowerCase()}.stl`],
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

const body = part({ id: 1, name: 'Body', aliases: ['body.stl', 'body_v2'] });
const cube = part({ id: 3, name: 'Cube', qty_per_unit: 0, ignored: true });
const magnet = part({
  id: 4,
  name: 'Magnet',
  kind: 'purchased',
  name_key: 'purchased:magnet',
  aliases: [],
  qty_per_unit: 4,
  unit_price: 0.5,
  sourcing_url: 'https://shop.example/m',
  remarks: 'N52',
});

const product = {
  id: 7,
  code: 'PR-0007',
  name: 'Flask',
  parts: [body, cube, magnet],
  variant_groups: [
    {
      id: 1,
      name: 'Lid type',
      position: 0,
      default_option_id: 10,
      lines_count: 0,
      stock_count: 0,
      parts_count: 0,
      options: [
        { id: 10, name: 'Glass', position: 0, lines_count: 0, parts_count: 0, stock_count: 0 },
        { id: 11, name: 'Cork', position: 1, lines_count: 0, parts_count: 0, stock_count: 0 },
      ],
    },
  ],
} as unknown as Product;

const noop = () => {};
const save = () => screen.getByRole('button', { name: /^(save part|saving…)$/i });

describe('ProductPartDialog', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, 'getUiPreferences').mockResolvedValue({ currency: 'EUR' } as never);
  });

  describe('the frame (C01)', () => {
    it('a new part: its title, «code · product», «Save part», the cursor in the name', async () => {
      render(<ProductPartDialog product={product} onClose={noop} />);
      const dialog = screen.getByRole('dialog', { name: 'New part' });
      expect(dialog).toHaveAccessibleDescription('PR-0007 · Flask');
      expect(within(dialog).getByRole('button', { name: 'Cancel' })).toBeInTheDocument();
      expect(save()).toBeInTheDocument();
      await waitFor(() => expect(screen.getByLabelText('Name')).toHaveFocus());
    });

    it('an edit: «Edit part» and the part’s values', () => {
      render(<ProductPartDialog product={product} part={body} onClose={noop} />);
      expect(screen.getByRole('dialog', { name: 'Edit part' })).toBeInTheDocument();
      expect(screen.getByLabelText('Name')).toHaveValue('Body');
      expect(screen.getByLabelText('Per unit')).toHaveValue(1);
    });
  });

  describe('the fields and the kind (C02, C03)', () => {
    it('the kind switches the blocks: a printed part’s aliases and mark, a purchased one’s price, address, remarks', async () => {
      render(<ProductPartDialog product={product} onClose={noop} />);
      expect(screen.getByLabelText('Also known as')).toBeInTheDocument();
      expect(screen.getByLabelText('Not counted')).toBeInTheDocument();
      expect(screen.queryByLabelText(/^Unit price/)).not.toBeInTheDocument();
      fireEvent.change(screen.getByLabelText('Kind'), { target: { value: 'purchased' } });
      expect(screen.queryByLabelText('Also known as')).not.toBeInTheDocument();
      expect(screen.queryByLabelText('Not counted')).not.toBeInTheDocument();
      expect(await screen.findByLabelText('Unit price, €')).toBeInTheDocument();
      expect(screen.getByLabelText('Where to buy')).toBeInTheDocument();
      expect(screen.getByLabelText('Remarks')).toBeInTheDocument();
    });

    it('an edit shows the kind as text: it does not change after creation', () => {
      render(<ProductPartDialog product={product} part={magnet} onClose={noop} />);
      const kind = screen.getByLabelText('Kind');
      expect(kind).toHaveValue('Purchased');
      expect(kind).toHaveAttribute('readonly');
      expect(kind).toHaveAccessibleDescription('The kind does not change — create a new part');
    });

    it('a purchased name is bounded by its prefixed key, a printed one by its column', () => {
      render(<ProductPartDialog product={product} onClose={noop} />);
      expect(screen.getByLabelText('Name')).toHaveAttribute('maxLength', '512');
      fireEvent.change(screen.getByLabelText('Kind'), { target: { value: 'purchased' } });
      expect(screen.getByLabelText('Name')).toHaveAttribute('maxLength', '502');
    });
  });

  describe('per unit and «Not counted» (C04)', () => {
    it('«Not counted» only at zero, and says why when shut', () => {
      render(<ProductPartDialog product={product} part={body} onClose={noop} />);
      const mark = screen.getByLabelText('Not counted');
      expect(mark).toBeDisabled();
      expect(mark).toHaveAccessibleDescription('Only for a part with 0 per unit.');
      expect(screen.getByLabelText('Per unit')).toHaveAccessibleDescription('0 — out of the kit');
      fireEvent.change(screen.getByLabelText('Per unit'), { target: { value: '0' } });
      expect(screen.getByLabelText('Not counted')).toBeEnabled();
    });

    it('a count typed on a marked part takes the mark off in the same request', async () => {
      const update = vi.spyOn(api, 'updateProductPart').mockResolvedValue(cube as never);
      render(<ProductPartDialog product={product} part={cube} onClose={noop} />);
      expect(screen.getByLabelText('Not counted')).toBeChecked();
      fireEvent.change(screen.getByLabelText('Per unit'), { target: { value: '2' } });
      fireEvent.click(save());
      await waitFor(() => expect(update).toHaveBeenCalledWith(7, 3, { qty_per_unit: 2, ignored: false }));
    });

    it('a fraction or a negative count is said in the dialog and nothing is sent', async () => {
      const update = vi.spyOn(api, 'updateProductPart');
      render(<ProductPartDialog product={product} part={body} onClose={noop} />);
      fireEvent.change(screen.getByLabelText('Per unit'), { target: { value: '1.5' } });
      fireEvent.click(save());
      expect(await screen.findByRole('alert')).toHaveTextContent('Per unit is a whole number from 0.');
      expect(update).not.toHaveBeenCalled();
    });

    it('the server refusing the mark is said in the slot, the focus on «Save part»', async () => {
      vi.spyOn(api, 'updateProductPart').mockRejectedValue(new ApiError('This part holds stock', 409));
      render(<ProductPartDialog product={product} part={part({ id: 9, name: 'Spare', qty_per_unit: 0 })} onClose={noop} />);
      fireEvent.click(screen.getByLabelText('Not counted'));
      fireEvent.click(save());
      expect(await screen.findByRole('alert')).toHaveTextContent('This part holds stock');
      await waitFor(() => expect(save()).toHaveFocus());
    });
  });

  describe('the variant (C05; spec product-part-variant-combinations п. 22)', () => {
    const groupBox = (name: string) => screen.getByRole('group', { name });
    const tick = (groupName: string, option: string) =>
      fireEvent.click(within(groupBox(groupName)).getByRole('checkbox', { name: option }));

    /** A jar: Weight 400 / 600 (ids 21, 22) and Kind solid / mesh (31, 32). */
    const jar = {
      ...product,
      variant_groups: [
        {
          id: 2,
          name: 'Weight',
          position: 0,
          default_option_id: 21,
          lines_count: 0,
          stock_count: 0,
          parts_count: 0,
          options: [
            { id: 21, name: '400', position: 0, lines_count: 0, parts_count: 0, stock_count: 0 },
            { id: 22, name: '600', position: 1, lines_count: 0, parts_count: 0, stock_count: 0 },
          ],
        },
        {
          id: 3,
          name: 'Kind',
          position: 1,
          default_option_id: 31,
          lines_count: 0,
          stock_count: 0,
          parts_count: 0,
          options: [
            { id: 31, name: 'solid', position: 0, lines_count: 0, parts_count: 0, stock_count: 0 },
            { id: 32, name: 'mesh', position: 1, lines_count: 0, parts_count: 0, stock_count: 0 },
          ],
        },
      ],
    } as unknown as Product;

    it('a row per group: «any» or its ticked options; a tick goes in the same PATCH', async () => {
      const update = vi.spyOn(api, 'updateProductPart').mockResolvedValue(body as never);
      render(<ProductPartDialog product={product} part={body} onClose={noop} />);
      expect(groupBox('Variant')).toHaveAccessibleDescription(
        'Taken when the order picks a ticked option in every group with ticks',
      );
      const lid = groupBox('Lid type');
      expect(within(lid).getAllByRole('checkbox').map((c) => c.parentElement?.textContent)).toEqual(['any', 'Glass', 'Cork']);
      expect(within(lid).getByRole('checkbox', { name: 'any' })).toBeChecked();
      tick('Lid type', 'Cork');
      expect(within(lid).getByRole('checkbox', { name: 'any' })).not.toBeChecked();
      fireEvent.click(save());
      await waitFor(() => expect(update).toHaveBeenCalledWith(7, 1, { variant_option_ids: [11] }));
    });

    it('ticking 400 and «solid» sends both, in the groups’ order', async () => {
      const update = vi.spyOn(api, 'updateProductPart').mockResolvedValue(body as never);
      render(<ProductPartDialog product={jar} part={body} onClose={noop} />);
      tick('Kind', 'solid');
      tick('Weight', '400');
      fireEvent.click(save());
      await waitFor(() => expect(update).toHaveBeenCalledWith(7, 1, { variant_option_ids: [21, 31] }));
    });

    it('two options of one group are «either»; «any» clears its group alone', async () => {
      const update = vi.spyOn(api, 'updateProductPart').mockResolvedValue(body as never);
      render(<ProductPartDialog product={jar} part={{ ...body, variant_option_ids: [21, 31] }} onClose={noop} />);
      tick('Weight', '600');
      tick('Kind', 'any');
      expect(within(groupBox('Weight')).getByRole('checkbox', { name: '400' })).toBeChecked();
      expect(within(groupBox('Kind')).getByRole('checkbox', { name: 'solid' })).not.toBeChecked();
      fireEvent.click(save());
      await waitFor(() => expect(update).toHaveBeenCalledWith(7, 1, { variant_option_ids: [21, 22] }));
    });

    it('clearing every tick sends an empty binding — always in the kit', async () => {
      const update = vi.spyOn(api, 'updateProductPart').mockResolvedValue(body as never);
      render(<ProductPartDialog product={jar} part={{ ...body, variant_option_ids: [21, 31] }} onClose={noop} />);
      tick('Weight', 'any');
      tick('Kind', 'solid');
      fireEvent.click(save());
      await waitFor(() => expect(update).toHaveBeenCalledWith(7, 1, { variant_option_ids: [] }));
    });

    it('the same set, in whatever order, is no change: nothing is sent', () => {
      const update = vi.spyOn(api, 'updateProductPart');
      const onClose = vi.fn();
      // The base lists the ids in another order than the groups': the comparison is of sets.
      render(<ProductPartDialog product={jar} part={{ ...body, variant_option_ids: [31, 21] }} onClose={onClose} />);
      tick('Kind', 'solid');
      tick('Weight', '600');
      tick('Weight', '600');
      tick('Kind', 'solid');
      fireEvent.click(save());
      expect(onClose).toHaveBeenCalledTimes(1);
      expect(update).not.toHaveBeenCalled();
    });

    it('a busy product is said in the slot, and the same press may be sent again', async () => {
      const update = vi
        .spyOn(api, 'updateProductPart')
        .mockRejectedValueOnce(new ApiError('The product is being changed by another request', 409, 'product_busy'))
        .mockResolvedValueOnce(body as never);
      const onClose = vi.fn();
      render(<ProductPartDialog product={product} part={body} onClose={onClose} />);
      tick('Lid type', 'Glass');
      fireEvent.click(save());
      expect(await screen.findByRole('alert')).toHaveTextContent('The product is being changed by another request');
      fireEvent.click(save());
      await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
      expect(update).toHaveBeenCalledTimes(2);
      expect(update).toHaveBeenLastCalledWith(7, 1, { variant_option_ids: [10] });
    });

    it('a bound option the product lost is named so and blocks the save until it is unticked', async () => {
      const update = vi.spyOn(api, 'updateProductPart').mockResolvedValue(body as never);
      render(<ProductPartDialog product={product} part={{ ...body, variant_option_ids: [10, 99] }} onClose={noop} />);
      const gone = screen.getByRole('checkbox', { name: '#99 (no longer exists)' });
      expect(gone).toBeChecked();
      expect(groupBox('Variant')).toHaveAccessibleDescription('A ticked option no longer exists — untick it.');
      expect(save()).toBeDisabled();
      fireEvent.click(gone);
      expect(screen.queryByRole('checkbox', { name: '#99 (no longer exists)' })).not.toBeInTheDocument();
      expect(save()).toBeEnabled();
      fireEvent.click(save());
      await waitFor(() => expect(update).toHaveBeenCalledWith(7, 1, { variant_option_ids: [10] }));
    });

    it('an option lost after the opening keeps the name it had then', () => {
      const corked = { ...body, variant_option_ids: [11] };
      const { rerender } = render(<ProductPartDialog product={product} part={corked} onClose={noop} />);
      const groups = product.variant_groups.map((g) => ({ ...g, options: g.options.filter((o) => o.id !== 11) }));
      rerender(<ProductPartDialog product={{ ...product, variant_groups: groups } as Product} part={corked} onClose={noop} />);
      expect(screen.getByRole('checkbox', { name: 'Lid type: Cork (no longer exists)' })).toBeChecked();
      expect(save()).toBeDisabled();
    });

    it('a product without variants shows no variant block', () => {
      render(<ProductPartDialog product={{ ...product, variant_groups: [] } as Product} part={body} onClose={noop} />);
      expect(screen.queryByRole('group', { name: 'Variant' })).not.toBeInTheDocument();
    });
  });

  describe('aliases (C06)', () => {
    it('its own key is a chip without «×»; the others can be removed', () => {
      render(<ProductPartDialog product={product} part={body} onClose={noop} />);
      const tokens = screen.getByTestId('part-alias-tokens');
      expect(within(tokens).getByText('body.stl')).toBeInTheDocument();
      expect(within(tokens).queryByRole('button', { name: 'Remove body.stl' })).not.toBeInTheDocument();
      expect(within(tokens).getByRole('button', { name: 'Remove body_v2' })).toBeInTheDocument();
      expect(screen.getByLabelText('Also known as')).toHaveAccessibleDescription(
        'Object names on a plate by which a file finds this part',
      );
    });

    it('Enter and a comma add a token as the server stores it — and send nothing', () => {
      const update = vi.spyOn(api, 'updateProductPart');
      render(<ProductPartDialog product={product} part={body} onClose={noop} />);
      const input = screen.getByLabelText('Also known as');
      fireEvent.change(input, { target: { value: '  Body_V3 ' } });
      fireEvent.keyDown(input, { key: 'Enter' });
      fireEvent.change(input, { target: { value: 'lid.STL' } });
      fireEvent.keyDown(input, { key: ',' });
      const tokens = screen.getByTestId('part-alias-tokens');
      expect(within(tokens).getByText('body_v3')).toBeInTheDocument();
      expect(within(tokens).getByText('lid.stl')).toBeInTheDocument();
      expect(input).toHaveValue('');
      expect(update).not.toHaveBeenCalled();
      expect(screen.getByRole('dialog')).toBeInTheDocument();
    });

    it('a pasted comma list becomes a token per name', () => {
      render(<ProductPartDialog product={product} part={body} onClose={noop} />);
      const input = screen.getByLabelText('Also known as');
      fireEvent.change(input, { target: { value: 'Lid.STL, cap_v2 ,, lid.stl' } });
      fireEvent.keyDown(input, { key: 'Enter' });
      const items = within(screen.getByTestId('part-alias-tokens')).getAllByRole('listitem').map((li) => li.textContent);
      expect(items.some((t) => t?.includes(','))).toBe(false);
      expect(within(screen.getByTestId('part-alias-tokens')).getByText('lid.stl')).toBeInTheDocument();
      expect(within(screen.getByTestId('part-alias-tokens')).getByText('cap_v2')).toBeInTheDocument();
      expect(input).toHaveValue('');
    });

    it('a blank or a name already in the list is not added, and says so', () => {
      render(<ProductPartDialog product={product} part={body} onClose={noop} />);
      const input = screen.getByLabelText('Also known as');
      fireEvent.change(input, { target: { value: 'BODY.STL' } });
      fireEvent.keyDown(input, { key: 'Enter' });
      expect(screen.getByText('This name is already in the list.')).toBeInTheDocument();
      expect(within(screen.getByTestId('part-alias-tokens')).getAllByText('body.stl')).toHaveLength(1);
      fireEvent.change(input, { target: { value: '   ' } });
      fireEvent.keyDown(input, { key: 'Enter' });
      expect(within(screen.getByTestId('part-alias-tokens')).getAllByRole('listitem')).toHaveLength(2);
    });

    it('Escape in the field empties it and leaves the dialog open', () => {
      const onClose = vi.fn();
      render(<ProductPartDialog product={product} part={body} onClose={onClose} />);
      const input = screen.getByLabelText('Also known as');
      fireEvent.change(input, { target: { value: 'half' } });
      fireEvent.keyDown(input, { key: 'Escape' });
      expect(input).toHaveValue('');
      expect(onClose).not.toHaveBeenCalled();
      expect(screen.getByRole('dialog')).toBeInTheDocument();
    });

    it('the list is sent whole with the part: removing one leaves the own key', async () => {
      const update = vi.spyOn(api, 'updateProductPart').mockResolvedValue(body as never);
      render(<ProductPartDialog product={product} part={body} onClose={noop} />);
      fireEvent.click(screen.getByRole('button', { name: 'Remove body_v2' }));
      fireEvent.click(save());
      await waitFor(() => expect(update).toHaveBeenCalledWith(7, 1, { aliases: [] }));
    });

    it('text left in the field becomes a token at «Save part», not lost', async () => {
      const update = vi.spyOn(api, 'updateProductPart').mockResolvedValue(body as never);
      render(<ProductPartDialog product={product} part={body} onClose={noop} />);
      fireEvent.change(screen.getByLabelText('Also known as'), { target: { value: 'Body_V3' } });
      fireEvent.click(save());
      await waitFor(() => expect(update).toHaveBeenCalledWith(7, 1, { aliases: ['body_v2', 'body_v3'] }));
    });

    it('a duplicate left in the field blocks the save with its hint', () => {
      const update = vi.spyOn(api, 'updateProductPart');
      render(<ProductPartDialog product={product} part={body} onClose={noop} />);
      fireEvent.change(screen.getByLabelText('Also known as'), { target: { value: 'body_v2' } });
      fireEvent.click(save());
      expect(screen.getByText('This name is already in the list.')).toBeInTheDocument();
      expect(screen.getByLabelText('Also known as')).toHaveFocus();
      expect(update).not.toHaveBeenCalled();
    });

    it('a key another part owns refuses the whole save, in the slot', async () => {
      vi.spyOn(api, 'updateProductPart').mockRejectedValue(new ApiError("'cube.stl' already belongs to part 'Cube'", 409));
      render(<ProductPartDialog product={product} part={body} onClose={noop} />);
      const input = screen.getByLabelText('Also known as');
      fireEvent.change(input, { target: { value: 'cube.stl' } });
      fireEvent.keyDown(input, { key: 'Enter' });
      fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Body Mk II' } });
      fireEvent.click(save());
      expect(await screen.findByRole('alert')).toHaveTextContent("'cube.stl' already belongs to part 'Cube'");
      // What was typed stays.
      expect(screen.getByLabelText('Name')).toHaveValue('Body Mk II');
      expect(within(screen.getByTestId('part-alias-tokens')).getByText('cube.stl')).toBeInTheDocument();
    });
  });

  describe('one request (C07)', () => {
    it('a new printed part: kind, name, count, mark, aliases and binding in one POST', async () => {
      const create = vi.spyOn(api, 'createProductPart').mockResolvedValue(body as never);
      render(<ProductPartDialog product={product} onClose={noop} />);
      fireEvent.change(screen.getByLabelText('Name'), { target: { value: ' Tail A ' } });
      fireEvent.click(within(screen.getByRole('group', { name: 'Lid type' })).getByRole('checkbox', { name: 'Glass' }));
      const input = screen.getByLabelText('Also known as');
      fireEvent.change(input, { target: { value: 'tail_a_v2' } });
      fireEvent.keyDown(input, { key: 'Enter' });
      fireEvent.click(save());
      await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
      expect(create).toHaveBeenCalledWith(7, {
        kind: 'printed',
        name: 'Tail A',
        qty_per_unit: 1,
        ignored: false,
        aliases: ['tail_a_v2'],
        variant_option_ids: [10],
      });
    });

    it('a new purchased part: its price, address and remarks; a blank price is none', async () => {
      const create = vi.spyOn(api, 'createProductPart').mockResolvedValue(magnet as never);
      render(<ProductPartDialog product={product} onClose={noop} />);
      fireEvent.change(screen.getByLabelText('Kind'), { target: { value: 'purchased' } });
      fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Nut' } });
      fireEvent.change(screen.getByLabelText('Per unit'), { target: { value: '4' } });
      fireEvent.change(screen.getByLabelText('Where to buy'), { target: { value: 'https://shop.example/n' } });
      fireEvent.change(screen.getByLabelText('Remarks'), { target: { value: 'M3' } });
      fireEvent.click(save());
      await waitFor(() =>
        expect(create).toHaveBeenCalledWith(7, {
          kind: 'purchased',
          name: 'Nut',
          qty_per_unit: 4,
          variant_option_ids: [],
          unit_price: null,
          sourcing_url: 'https://shop.example/n',
          remarks: 'M3',
        }),
      );
    });

    it('an edit of a purchased part sends only what moved; a cleared price goes as none', async () => {
      const update = vi.spyOn(api, 'updateProductPart').mockResolvedValue(magnet as never);
      render(<ProductPartDialog product={product} part={magnet} onClose={noop} />);
      fireEvent.change(await screen.findByLabelText('Unit price, €'), { target: { value: '' } });
      fireEvent.change(screen.getByLabelText('Remarks'), { target: { value: 'N42' } });
      fireEvent.click(save());
      await waitFor(() => expect(update).toHaveBeenCalledWith(7, 4, { unit_price: null, remarks: 'N42' }));
    });

    it('a negative price is said in the dialog', async () => {
      render(<ProductPartDialog product={product} part={magnet} onClose={noop} />);
      fireEvent.change(await screen.findByLabelText('Unit price, €'), { target: { value: '-1' } });
      fireEvent.click(save());
      expect(await screen.findByRole('alert')).toHaveTextContent('The price cannot be negative.');
    });

    it('an empty name is said in the dialog and nothing is sent', async () => {
      const create = vi.spyOn(api, 'createProductPart');
      render(<ProductPartDialog product={product} onClose={noop} />);
      fireEvent.click(save());
      expect(await screen.findByRole('alert')).toHaveTextContent('Enter the part’s name.');
      expect(screen.getByLabelText('Name')).toHaveFocus();
      expect(create).not.toHaveBeenCalled();
      // Typing what was asked for takes the sentence away (final review M5).
      fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Hinge' } });
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    });

    it('nothing changed: closes without a request; «Cancel» sends nothing', () => {
      const update = vi.spyOn(api, 'updateProductPart');
      const onClose = vi.fn();
      render(<ProductPartDialog product={product} part={body} onClose={onClose} />);
      fireEvent.click(save());
      fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
      expect(onClose).toHaveBeenCalledTimes(2);
      expect(update).not.toHaveBeenCalled();
    });

    it('a success closes, says «Part saved» and makes the product stale', async () => {
      vi.spyOn(api, 'updateProductPart').mockResolvedValue(body as never);
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      client.setQueryData(['product', 7], { seeded: true });
      client.setQueryData(['projects', {}], { seeded: true });
      const onClose = vi.fn();
      render(
        <QueryClientProvider client={client}>
          <ProductPartDialog product={product} part={body} onClose={onClose} />
        </QueryClientProvider>,
      );
      fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Body 2' } });
      fireEvent.click(save());
      await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
      expect(client.getQueryState(['product', 7])?.isInvalidated).toBe(true);
      // A part's count, binding or existence moves the kits of saved order lines (K22).
      expect(client.getQueryState(['projects', {}])?.isInvalidated).toBe(true);
      expect(await screen.findByText('Part saved')).toBeInTheDocument();
    });

    it('under a request nothing closes it and nothing sends twice — decided in the same frame', async () => {
      const create = vi.spyOn(api, 'createProductPart').mockReturnValue(new Promise(() => {}) as never);
      const onClose = vi.fn();
      render(<ProductPartDialog product={product} onClose={onClose} />);
      fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Hinge' } });
      const submit = save();
      const cancel = screen.getByRole('button', { name: 'Cancel' });
      const x = screen.getByRole('button', { name: 'Close' });
      act(() => {
        submit.click();
        window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
        cancel.click();
        x.click();
        submit.click();
      });
      await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
      expect(onClose).not.toHaveBeenCalled();
    });
  });

  describe('the picture — Save after Save (spec part-thumbnails §12.4, §22 p. 2)', () => {
    const photoFile = new File(['png'], 'body.png', { type: 'image/png' });
    const savedNew = part({ id: 9, name: 'Gear', aliases: ['gear.stl'] });
    const withPhoto = part({
      id: 1,
      name: 'Body',
      aliases: ['body.stl', 'body_v2'],
      image: { kind: 'photo', status: 'ready', v: 'old.png' },
      image_choice: { source: 'photo', instance: null, pin: null, photo: { present: true } },
    });

    beforeEach(() => {
      vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: vi.fn(() => 'blob:x'), revokeObjectURL: vi.fn() }));
    });
    afterEach(() => vi.unstubAllGlobals());

    function choosePhoto() {
      fireEvent.change(screen.getByLabelText('Choose a photo'), { target: { files: [photoFile] } });
    }

    it('a new part whose photo is refused: one part, the dialog stays, a retry sends the photo alone (Review Focus 1)', async () => {
      const create = vi.spyOn(api, 'createProductPart').mockResolvedValue(savedNew as never);
      const update = vi.spyOn(api, 'updateProductPart');
      const upload = vi
        .spyOn(api, 'uploadPartPhoto')
        .mockRejectedValueOnce(new ApiError('The photo must be a PNG, JPEG or WebP picture', 422))
        .mockResolvedValueOnce({ ...savedNew, image: { kind: 'photo', status: 'ready', v: 'n.png' } } as never);
      const onClose = vi.fn();
      render(<ProductPartDialog product={product} onClose={onClose} />);
      fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Gear' } });
      choosePhoto();
      fireEvent.click(save());
      expect(await screen.findByRole('alert')).toHaveTextContent(
        'The part is saved; its picture is not: The photo must be a PNG, JPEG or WebP picture',
      );
      // the saved part is the dialog's now: an edit, the kind fixed
      expect(screen.getByRole('dialog', { name: 'Edit part' })).toBeInTheDocument();
      expect(screen.getByLabelText('Kind')).toHaveAttribute('readonly');
      expect(onClose).not.toHaveBeenCalled();
      await waitFor(() => expect(save()).not.toBeDisabled());
      fireEvent.click(save());
      await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
      expect(create).toHaveBeenCalledTimes(1);
      expect(update).not.toHaveBeenCalled();
      expect(upload).toHaveBeenCalledTimes(2);
      expect(upload.mock.calls[1]).toEqual([9, photoFile]);
    });

    it('a picture-only change sends the picture alone — no PATCH', async () => {
      const update = vi.spyOn(api, 'updateProductPart');
      const set = vi.spyOn(api, 'setPartImage').mockResolvedValue(withPhoto as never);
      const onClose = vi.fn();
      render(<ProductPartDialog product={product} part={withPhoto} onClose={onClose} />);
      fireEvent.click(screen.getByRole('radio', { name: /Automatic/ }));
      fireEvent.click(save());
      await waitFor(() => expect(onClose).toHaveBeenCalled());
      expect(set).toHaveBeenCalledWith(1, { source: 'auto' });
      expect(update).not.toHaveBeenCalled();
    });

    it('the fields first, then the picture; one toast, closed', async () => {
      const order: string[] = [];
      vi.spyOn(api, 'updateProductPart').mockImplementation(async () => {
        order.push('patch');
        return { ...body, name: 'Body 2' } as never;
      });
      vi.spyOn(api, 'uploadPartPhoto').mockImplementation(async () => {
        order.push('photo');
        return { ...body, name: 'Body 2' } as never;
      });
      const onClose = vi.fn();
      render(<ProductPartDialog product={product} part={body} onClose={onClose} />);
      fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Body 2' } });
      choosePhoto();
      fireEvent.click(save());
      await waitFor(() => expect(onClose).toHaveBeenCalled());
      expect(order).toEqual(['patch', 'photo']);
      expect(await screen.findByText('Part saved')).toBeInTheDocument();
    });

    it('«Cancel» before «Save part» sends nothing, whatever was chosen', () => {
      const calls = [
        vi.spyOn(api, 'createProductPart'),
        vi.spyOn(api, 'updateProductPart'),
        vi.spyOn(api, 'setPartImage'),
        vi.spyOn(api, 'uploadPartPhoto'),
        vi.spyOn(api, 'deletePartPhoto'),
      ];
      const onClose = vi.fn();
      render(<ProductPartDialog product={product} part={withPhoto} onClose={onClose} />);
      choosePhoto();
      fireEvent.click(screen.getByRole('radio', { name: /Automatic/ }));
      fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
      expect(onClose).toHaveBeenCalledTimes(1);
      for (const call of calls) expect(call).not.toHaveBeenCalled();
    });

    it('a pin refused after the fields saved: the dialog stays on the server’s state, the retry is the pin alone (Review Focus 4)', async () => {
      const key = { library_file_id: 9, plate_index: 1, identify_id: 11 };
      vi.spyOn(api, 'getPartImageCandidates').mockResolvedValue([
        {
          library_file_id: 9,
          filename: 'flask.gcode.3mf',
          hidden: false,
          plate_index: 1,
          identify_id: 11,
          method: 'toolpath',
          reason: null,
          plate_status: 'ready',
          v: 'v11',
          pinned: false,
        },
      ]);
      const savedFields = { ...body, name: 'Body 2', image_choice: { source: 'auto', instance: null, pin: null, photo: null } };
      const update = vi.spyOn(api, 'updateProductPart').mockResolvedValue(savedFields as never);
      const set = vi
        .spyOn(api, 'setPartImage')
        .mockRejectedValueOnce(new ApiError('This object cannot be pinned to this part', 422))
        .mockResolvedValueOnce(savedFields as never);
      const onClose = vi.fn();
      render(<ProductPartDialog product={product} part={body} onClose={onClose} />);
      fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Body 2' } });
      fireEvent.click(await screen.findByRole('radio', { name: /Object #11/ }));
      fireEvent.click(save());
      expect(await screen.findByRole('alert')).toHaveTextContent('This object cannot be pinned to this part');
      expect(screen.getByText('Saved: automatic')).toBeInTheDocument();
      expect(screen.getByLabelText('Name')).toHaveValue('Body 2');
      expect(onClose).not.toHaveBeenCalled();
      await waitFor(() => expect(save()).not.toBeDisabled());
      fireEvent.click(save());
      await waitFor(() => expect(onClose).toHaveBeenCalled());
      expect(update).toHaveBeenCalledTimes(1);
      expect(set).toHaveBeenLastCalledWith(1, { source: 'instance', instance: key });
    });

    it('Escape with the larger picture open closes it alone; the alias being typed survives (E5-R2)', () => {
      const onClose = vi.fn();
      const ready = part({ id: 1, name: 'Body', aliases: ['body.stl'], image: { kind: 'render', status: 'ready', v: 'r1' } });
      render(<ProductPartDialog product={product} part={ready} onClose={onClose} />);
      const input = screen.getByLabelText('Also known as');
      fireEvent.change(input, { target: { value: 'pending_alias' } });
      // the keyboard stays in the alias field; the mouse opens the larger picture
      fireEvent.mouseEnter(within(screen.getByTestId('part-image-section')).getAllByTestId('part-thumb')[0]);
      expect(screen.getByTestId('part-thumb-zoom')).toBeInTheDocument();
      fireEvent.keyDown(input, { key: 'Escape' });
      expect(screen.queryByTestId('part-thumb-zoom')).not.toBeInTheDocument();
      expect(input).toHaveValue('pending_alias');
      expect(onClose).not.toHaveBeenCalled();
      // the next Escape is the alias field's own: it empties it, the dialog stays (C06)
      fireEvent.keyDown(input, { key: 'Escape' });
      expect(input).toHaveValue('');
      expect(onClose).not.toHaveBeenCalled();
      // and the one after is the dialog's
      fireEvent.keyDown(input, { key: 'Escape' });
      expect(onClose).toHaveBeenCalledTimes(1);
    });

    it('the saved picture follows the product; the typed name and the draft stay (E5-R1)', () => {
      const pendingBody = part({
        id: 1,
        name: 'Body',
        aliases: ['body.stl'],
        image: { kind: 'render', status: 'pending', v: null },
        image_choice: { source: 'auto', instance: null, pin: null, photo: null },
      });
      const opened = { ...product, parts: [pendingBody] } as Product;
      const { rerender } = render(<ProductPartDialog product={opened} part={pendingBody} onClose={noop} />);
      fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Unsaved name' } });
      fireEvent.click(screen.getByRole('radio', { name: 'Photo' }));
      const fresh = { ...pendingBody, image: { kind: 'render', status: 'ready', v: 'ready-v2' } } as ProductPart;
      // the caller's stored part stays the opening snapshot — only the product is live
      rerender(<ProductPartDialog product={{ ...opened, parts: [fresh] } as Product} part={pendingBody} onClose={noop} />);
      expect(screen.getByLabelText('Name')).toHaveValue('Unsaved name');
      expect(screen.getByRole('radio', { name: 'Photo' })).toBeChecked();
      expect(within(screen.getByTestId('part-image-section')).getAllByTestId('part-thumb')[0]).toHaveAttribute(
        'data-state',
        'ready',
      );
    });

    it('a refusal leaves the slot once another file is chosen (plan E5, Task 41)', async () => {
      vi.spyOn(api, 'uploadPartPhoto').mockRejectedValueOnce(new ApiError('The photo must be a PNG, JPEG or WebP picture', 422));
      render(<ProductPartDialog product={product} part={body} onClose={noop} />);
      choosePhoto();
      fireEvent.click(save());
      expect(await screen.findByRole('alert')).toHaveTextContent('The picture is not saved');
      choosePhoto();
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    });

    it('the partial-success sentence ends the server’s own and reads on — one full stop (plan E5, Task 41)', async () => {
      vi.spyOn(api, 'createProductPart').mockResolvedValue(savedNew as never);
      vi.spyOn(api, 'uploadPartPhoto')
        .mockRejectedValueOnce(new ApiError('The photo must be a PNG, JPEG or WebP picture', 422))
        .mockRejectedValueOnce(new ApiError('Not a picture.', 422));
      render(<ProductPartDialog product={product} onClose={noop} />);
      fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Gear' } });
      choosePhoto();
      fireEvent.click(save());
      // the server's sentences carry no full stop: the dialog puts one before its own
      expect(await screen.findByRole('alert')).toHaveTextContent(
        'The part is saved; its picture is not: The photo must be a PNG, JPEG or WebP picture. «Save part» tries the picture again',
      );
      await waitFor(() => expect(save()).not.toBeDisabled());
      fireEvent.click(save());
      // and one that does carry it gets no second
      await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('its picture is not: Not a picture. «Save part»'));
    });

    it('a refused pin asks the gallery again: what the part now matches may have moved (review 2)', async () => {
      const candidate = {
        library_file_id: 9,
        filename: 'flask.gcode.3mf',
        hidden: false,
        plate_index: 1,
        identify_id: 11,
        method: 'toolpath',
        reason: null,
        plate_status: 'ready' as const,
        v: 'v11',
        pinned: false,
      };
      const candidates = vi.spyOn(api, 'getPartImageCandidates').mockResolvedValue([candidate]);
      vi.spyOn(api, 'updateProductPart').mockResolvedValue({ ...body, name: 'Body 2' } as never);
      vi.spyOn(api, 'setPartImage').mockRejectedValue(new ApiError('This object cannot be pinned to this part', 422));
      render(<ProductPartDialog product={product} part={body} onClose={noop} />);
      fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Body 2' } });
      fireEvent.click(await screen.findByRole('radio', { name: /Object #11/ }));
      const before = candidates.mock.calls.length;
      fireEvent.click(save());
      expect(await screen.findByRole('alert')).toHaveTextContent('This object cannot be pinned to this part');
      await waitFor(() => expect(candidates.mock.calls.length).toBeGreaterThan(before));
    });

    it('after a partial success, putting the picture back to what is saved closes with «Part saved» (review 4)', async () => {
      vi.spyOn(api, 'createProductPart').mockResolvedValue(savedNew as never);
      vi.spyOn(api, 'uploadPartPhoto').mockRejectedValue(new ApiError('The photo must be a PNG, JPEG or WebP picture', 422));
      const onClose = vi.fn();
      render(<ProductPartDialog product={product} onClose={onClose} />);
      fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Gear' } });
      choosePhoto();
      fireEvent.click(save());
      expect(await screen.findByRole('alert')).toBeInTheDocument();
      fireEvent.click(screen.getByRole('radio', { name: /Automatic/ }));
      await waitFor(() => expect(save()).not.toBeDisabled());
      fireEvent.click(save());
      await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
      expect(await screen.findByText('Part saved')).toBeInTheDocument();
    });

    it('a picture-only refusal ends with its own full stop (review 5)', async () => {
      vi.spyOn(api, 'uploadPartPhoto').mockRejectedValue(new ApiError('The photo must be a PNG, JPEG or WebP picture', 422));
      render(<ProductPartDialog product={product} part={body} onClose={noop} />);
      choosePhoto();
      fireEvent.click(save());
      expect(await screen.findByRole('alert')).toHaveTextContent(
        'The picture is not saved: The photo must be a PNG, JPEG or WebP picture.',
      );
    });

    it('«Photo» with no file on a part without a saved photo: said in the dialog, nothing sent', () => {
      const update = vi.spyOn(api, 'updateProductPart');
      const upload = vi.spyOn(api, 'uploadPartPhoto');
      render(<ProductPartDialog product={product} part={body} onClose={noop} />);
      fireEvent.click(screen.getByRole('radio', { name: 'Photo' }));
      fireEvent.click(save());
      expect(screen.getByRole('alert')).toHaveTextContent('Choose a photo file.');
      expect(screen.getByLabelText('Choose a photo')).toHaveFocus();
      expect(update).not.toHaveBeenCalled();
      expect(upload).not.toHaveBeenCalled();
    });
  });
});


describe('ProductPartDialog additional parts', () => {
  beforeEach(() => { vi.restoreAllMocks(); vi.spyOn(api, 'getUiPreferences').mockResolvedValue({ currency: 'EUR' } as never); });
  it('saves an entered fractional percentage and shows rounding/shipping help', async () => {
    const update = vi.spyOn(api, 'updateProductPart').mockResolvedValue(body as never);
    render(<ProductPartDialog product={product} part={body} onClose={noop} />);
    const field = screen.getByLabelText('Additional parts, %');
    expect(field).toHaveValue(0);
    expect(field).toHaveAccessibleDescription(/Rounded up once over the whole order line/);
    fireEvent.change(field, { target: { value: '12.5' } });
    fireEvent.click(save());
    await waitFor(() => expect(update).toHaveBeenCalledWith(7, 1, { extra_percent: 12.5 }));
  });
  it('can explicitly reset the stored percentage to zero', async () => {
    const update = vi.spyOn(api, 'updateProductPart').mockResolvedValue(body as never);
    render(<ProductPartDialog product={product} part={{ ...body, extra_percent: 15 }} onClose={noop} />);
    fireEvent.change(screen.getByLabelText('Additional parts, %'), { target: { value: '0' } });
    fireEvent.click(save());
    await waitFor(() => expect(update).toHaveBeenCalledWith(7, 1, { extra_percent: 0 }));
  });
  it('does not offer an extra percentage for a purchased part', () => {
    render(<ProductPartDialog product={product} part={magnet} onClose={noop} />);
    expect(screen.queryByLabelText('Additional parts, %')).not.toBeInTheDocument();
  });
  it('rejects invalid percentage without writing', async () => {
    const update = vi.spyOn(api, 'updateProductPart');
    render(<ProductPartDialog product={product} part={body} onClose={noop} />);
    fireEvent.change(screen.getByLabelText('Additional parts, %'), { target: { value: '-1' } });
    fireEvent.click(save());
    expect(await screen.findByText('Enter a percentage from 0 to 1000.')).toBeInTheDocument();
    expect(update).not.toHaveBeenCalled();
  });
});
