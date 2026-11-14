/**
 * The variants manager (WS-13 E10 D01–D06, F16): the whole set of groups as a local
 * draft, saved by ONE `PUT …/variants` against the revision it was opened on. Renames
 * keep ids, new rows carry temp ids, a delete the server would refuse is shut with its
 * reason, an inherited group without a standard or without options stays as stored
 * (A06). A refusal points at its row — or brings back a row the server would not let go
 * (R03); a changed revision is re-read only after a confirmation.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { render } from '../../utils';
import { api, ApiError } from '../../../api/client';
import type { Product } from '../../../api/client';
import { ProductVariantsDialog } from '../../../components/products/ProductVariantsDialog';
import { invalidateProductVariants } from '../../../utils/queryInvalidation';

vi.mock('../../../utils/queryInvalidation', async (original) => ({
  ...(await original<typeof import('../../../utils/queryInvalidation')>()),
  invalidateProductVariants: vi.fn(),
}));

const zero = { lines_count: 0, stock_count: 0, parts_count: 0 };
const product = {
  id: 7,
  code: 'PR-0007',
  name: 'Flask',
  parts: [],
  variants_revision: 'rev-1',
  variant_groups: [
    {
      id: 1,
      name: 'Lid type',
      position: 0,
      default_option_id: 10,
      lines_count: 0,
      stock_count: 0,
      parts_count: 1,
      options: [
        { id: 10, name: 'Glass', position: 0, ...zero },
        { id: 11, name: 'Cork', position: 1, lines_count: 0, stock_count: 0, parts_count: 1 },
        { id: 12, name: 'Wood', position: 2, ...zero },
      ],
    },
    {
      id: 2,
      name: 'Size',
      position: 1,
      default_option_id: null,
      requires_part: true,
      lines_count: 3,
      stock_count: 0,
      parts_count: 0,
      options: [
        { id: 20, name: 'S', position: 0, lines_count: 3, stock_count: 0, parts_count: 0 },
        { id: 21, name: 'M', position: 1, ...zero },
      ],
    },
    { id: 3, name: 'Extra', position: 2, default_option_id: null, ...zero, options: [] },
  ],
} as unknown as Product;

const noop = () => {};
const group = (key: string) => screen.getByTestId(`variant-group-${key}`);
const save = () => screen.getByRole('button', { name: /^(save|saving…)$/i });

function refused(message: string, status: number, code: string, refs?: ApiError['refs']) {
  const e = new ApiError(message, status, code);
  if (refs) e.refs = refs;
  return e;
}

describe('ProductVariantsDialog', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.mocked(invalidateProductVariants).mockClear();
  });

  describe('the frame and the draft (D01, D02)', () => {
    it('«Product variants», «code · name», the hint; the cursor in the first group’s name', async () => {
      render(<ProductVariantsDialog product={product} onClose={noop} />);
      const dialog = screen.getByRole('dialog', { name: 'Product variants' });
      expect(dialog).toHaveAccessibleDescription('PR-0007 · Flask');
      expect(within(dialog).getByText(/a group of parts of which an order takes one option/)).toBeInTheDocument();
      await waitFor(() => expect(within(group('g1')).getByLabelText('Group')).toHaveFocus());
    });

    it('a row per option, the standard chosen from the group’s own; «No standard» only where it was stored so', () => {
      render(<ProductVariantsDialog product={product} onClose={noop} />);
      const lid = group('g1');
      expect(within(lid).getAllByRole('textbox', { name: /^Option \d of “Lid type”$/ }).map((i) => (i as HTMLInputElement).value)).toEqual(['Glass', 'Cork', 'Wood']);
      const standard = within(lid).getByLabelText('Standard');
      expect(standard).toHaveValue('o10');
      expect(within(standard).queryByRole('option', { name: 'No standard' })).not.toBeInTheDocument();
      const size = within(group('g2')).getByLabelText('Standard');
      expect(size).toHaveValue('');
      expect(within(size).getByRole('option', { name: 'No standard' })).toBeInTheDocument();
    });

    it('an inherited empty group says so and may take its first option', () => {
      render(<ProductVariantsDialog product={product} onClose={noop} />);
      const extra = group('g3');
      expect(within(extra).getByText('No options')).toBeInTheDocument();
      expect(within(extra).getByText('Add an option or delete the group')).toBeInTheDocument();
      fireEvent.click(within(extra).getByRole('button', { name: 'Option' }));
      expect(within(extra).getByRole('textbox', { name: 'Option 1 of “Extra”' })).toHaveValue('option 1');
    });

    it('a background refresh of the product reseeds nothing; the save goes against the opening revision', async () => {
      const apply = vi.spyOn(api, 'applyProductVariants').mockResolvedValue(product as never);
      const { rerender } = render(<ProductVariantsDialog product={product} onClose={noop} />);
      fireEvent.change(within(group('g1')).getByLabelText('Group'), { target: { value: 'Lid' } });
      rerender(
        <ProductVariantsDialog
          product={{ ...product, variants_revision: 'rev-9', variant_groups: [] } as Product}
          onClose={noop}
        />,
      );
      expect(within(group('g1')).getByLabelText('Group')).toHaveValue('Lid');
      expect(group('g2')).toBeInTheDocument();
      fireEvent.click(save());
      await waitFor(() => expect(apply).toHaveBeenCalledTimes(1));
      expect(apply.mock.calls[0][1].revision).toBe('rev-1');
    });

    it('«Add group» adds the mockup’s «New group» with two options, the first one standard', () => {
      render(<ProductVariantsDialog product={product} onClose={noop} />);
      fireEvent.click(screen.getByRole('button', { name: 'Add group' }));
      const added = screen.getAllByTestId(/^variant-group-/).at(-1)!;
      expect(within(added).getByLabelText('Group')).toHaveValue('New group');
      expect(within(added).getAllByRole('textbox', { name: /^Option \d of “New group”$/ }).map((i) => (i as HTMLInputElement).value)).toEqual(['option 1', 'option 2']);
      expect((within(added).getByLabelText('Standard') as HTMLSelectElement).selectedOptions[0].textContent).toBe('option 1');
    });
  });

  describe('what may go (D03)', () => {
    it('an option in use, or the standard, cannot be removed; another can', () => {
      render(<ProductVariantsDialog product={product} onClose={noop} />);
      const lid = group('g1');
      expect(within(lid).getByRole('button', { name: 'Delete option “Glass”' })).toBeDisabled();
      const cork = within(lid).getByRole('button', { name: 'Delete option “Cork”' });
      expect(cork).toBeDisabled();
      // The reason is on screen beside the option, not only a mouse-over title (final review M8).
      const corkRow = within(lid).getByRole('textbox', { name: 'Option 2 of “Lid type”' }).closest('li') as HTMLElement;
      expect(within(corkRow).getByText('1 part is bound to it')).toBeInTheDocument();
      expect(cork).toHaveAccessibleDescription('1 part is bound to it');
      expect(within(lid).getByRole('button', { name: 'Delete option “Wood”' })).toBeEnabled();
    });

    it('the standard can go once another is chosen in the same draft', () => {
      render(<ProductVariantsDialog product={product} onClose={noop} />);
      const lid = group('g1');
      fireEvent.change(within(lid).getByLabelText('Standard'), { target: { value: 'o12' } });
      expect(within(lid).getByRole('button', { name: 'Delete option “Glass”' })).toBeEnabled();
    });

    it('a group in use cannot be deleted; an unused one can', () => {
      render(<ProductVariantsDialog product={product} onClose={noop} />);
      expect(screen.getByRole('button', { name: 'Delete group “Size”' })).toBeDisabled();
      expect(screen.getByRole('button', { name: 'Delete group “Extra”' })).toBeEnabled();
    });
  });

  describe('checks before the request (D04)', () => {
    it('an empty name or a duplicate is said by its field, and «Save» waits', () => {
      render(<ProductVariantsDialog product={product} onClose={noop} />);
      const lid = group('g1');
      fireEvent.change(within(lid).getByRole('textbox', { name: 'Option 3 of “Lid type”' }), { target: { value: 'cork' } });
      expect(within(lid).getByText('This group already has an option with this name.')).toBeInTheDocument();
      expect(within(lid).getByRole('textbox', { name: 'Option 3 of “Lid type”' })).toHaveAttribute('aria-invalid', 'true');
      fireEvent.change(within(group('g2')).getByLabelText('Group'), { target: { value: ' ' } });
      expect(within(group('g2')).getByText('Enter the group’s name.')).toBeInTheDocument();
      expect(save()).toBeDisabled();
    });

    it('an inherited group without a standard may be renamed and gain an option — no standard appears', async () => {
      const apply = vi.spyOn(api, 'applyProductVariants').mockResolvedValue(product as never);
      render(<ProductVariantsDialog product={product} onClose={noop} />);
      const size = group('g2');
      fireEvent.change(within(size).getByLabelText('Group'), { target: { value: 'Stand' } });
      fireEvent.click(within(size).getByRole('button', { name: 'Option' }));
      fireEvent.click(save());
      await waitFor(() => expect(apply).toHaveBeenCalledTimes(1));
      const body = apply.mock.calls[0][1];
      expect(body.revision).toBe('rev-1');
      expect(body.groups[1]).toEqual({
        id: 2,
        name: 'Stand',
        default: null,
        requires_part: true,
        options: [
          { id: 20, name: 'S' },
          { id: 21, name: 'M' },
          { temp_id: expect.any(String), name: 'option 3' },
        ],
      });
    });
  });

  describe('the save (D05)', () => {
    it('one PUT of the whole draft; renames keep their ids', async () => {
      const apply = vi.spyOn(api, 'applyProductVariants').mockResolvedValue(product as never);
      const onClose = vi.fn();
      render(<ProductVariantsDialog product={product} onClose={onClose} />);
      fireEvent.change(within(group('g1')).getByRole('textbox', { name: 'Option 2 of “Lid type”' }), { target: { value: 'Cork lid' } });
      fireEvent.click(within(group('g1')).getByRole('button', { name: 'Delete option “Wood”' }));
      fireEvent.click(save());
      await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
      expect(apply).toHaveBeenCalledTimes(1);
      expect(apply.mock.calls[0][1].groups[0]).toEqual({
        id: 1,
        name: 'Lid type',
        default: 10,
        requires_part: false,
        options: [
          { id: 10, name: 'Glass' },
          { id: 11, name: 'Cork lid' },
        ],
      });
      expect(invalidateProductVariants).toHaveBeenCalledWith(expect.anything(), 7);
      expect(await screen.findByText('Variants saved')).toBeInTheDocument();
    });

    it('«Every value needs its own part» shows the server’s mark with its hint; the mark alone is a save', async () => {
      const apply = vi.spyOn(api, 'applyProductVariants').mockResolvedValue(product as never);
      const onClose = vi.fn();
      render(<ProductVariantsDialog product={product} onClose={onClose} />);
      const lidMark = within(group('g1')).getByRole('checkbox', { name: 'Every value needs its own part' });
      const sizeMark = within(group('g2')).getByRole('checkbox', { name: 'Every value needs its own part' });
      expect(lidMark).not.toBeChecked();
      expect(sizeMark).toBeChecked();
      expect(lidMark).toHaveAccessibleDescription(/can’t be added to an order\. Leave it off for optional add-ons\./);
      fireEvent.click(lidMark);
      fireEvent.click(save());
      await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
      expect(apply).toHaveBeenCalledTimes(1);
      const body = apply.mock.calls[0][1];
      expect(body.groups.map((g) => [g.id, g.requires_part])).toEqual([
        [1, true],
        [2, true],
        [3, false],
      ]);
      // Nothing else of the group moved.
      expect(body.groups[0]).toEqual(expect.objectContaining({ id: 1, name: 'Lid type', default: 10 }));
    });

    it('a new group starts without the mark', async () => {
      const apply = vi.spyOn(api, 'applyProductVariants').mockResolvedValue(product as never);
      render(<ProductVariantsDialog product={product} onClose={noop} />);
      fireEvent.click(screen.getByRole('button', { name: 'Add group' }));
      fireEvent.click(save());
      await waitFor(() => expect(apply).toHaveBeenCalledTimes(1));
      const added = apply.mock.calls[0][1].groups[3];
      expect(added.temp_id).toEqual(expect.any(String));
      expect(added.requires_part).toBe(false);
    });

    it('nothing changed: closes without a request; «Cancel» sends nothing', () => {
      const apply = vi.spyOn(api, 'applyProductVariants');
      const onClose = vi.fn();
      render(<ProductVariantsDialog product={product} onClose={onClose} />);
      fireEvent.click(save());
      fireEvent.change(within(group('g1')).getByLabelText('Group'), { target: { value: 'Lid' } });
      fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
      expect(onClose).toHaveBeenCalledTimes(2);
      expect(apply).not.toHaveBeenCalled();
    });

    it('a refusal about a row in the draft: said in the slot, that field marked and focused', async () => {
      vi.spyOn(api, 'applyProductVariants').mockRejectedValue(
        refused('An option with this name already exists', 409, 'option_name_taken', { group: 1, option: 12 }),
      );
      render(<ProductVariantsDialog product={product} onClose={noop} />);
      fireEvent.change(within(group('g1')).getByRole('textbox', { name: 'Option 3 of “Lid type”' }), { target: { value: 'Oak' } });
      fireEvent.click(save());
      expect(await screen.findByRole('alert')).toHaveTextContent('An option with this name already exists');
      const field = within(group('g1')).getByRole('textbox', { name: 'Option 3 of “Lid type”' });
      await waitFor(() => expect(field).toHaveFocus());
      expect(field).toHaveAttribute('aria-invalid', 'true');
    });

    it('a refused delete brings its row back on request — named from the opening, the focus on the action', async () => {
      const apply = vi
        .spyOn(api, 'applyProductVariants')
        .mockRejectedValueOnce(refused('Option chosen in 2 order lines', 409, 'option_in_use', { group: 1, option: 12 }))
        .mockResolvedValueOnce(product as never);
      render(<ProductVariantsDialog product={product} onClose={noop} />);
      fireEvent.click(within(group('g1')).getByRole('button', { name: 'Delete option “Wood”' }));
      fireEvent.click(save());
      const alert = await screen.findByRole('alert');
      expect(alert).toHaveTextContent('Option “Wood” cannot be deleted: Option chosen in 2 order lines');
      const back = within(alert).getByRole('button', { name: 'Bring “Wood” back' });
      await waitFor(() => expect(back).toHaveFocus());
      fireEvent.click(back);
      expect(within(group('g1')).getByRole('textbox', { name: 'Option 3 of “Lid type”' })).toHaveValue('Wood');
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
      expect(apply).toHaveBeenCalledTimes(1);
    });

    it('a refused group delete brings the whole group back', async () => {
      vi.spyOn(api, 'applyProductVariants').mockRejectedValue(
        refused('Group chosen in 3 order lines', 409, 'group_in_use', { group: 3 }),
      );
      render(<ProductVariantsDialog product={product} onClose={noop} />);
      fireEvent.click(screen.getByRole('button', { name: 'Delete group “Extra”' }));
      fireEvent.click(save());
      const alert = await screen.findByRole('alert');
      expect(alert).toHaveTextContent('Group “Extra” cannot be deleted: Group chosen in 3 order lines');
      fireEvent.click(within(alert).getByRole('button', { name: 'Bring “Extra” back' }));
      expect(group('g3')).toBeInTheDocument();
    });

    it('a refusal without references: the slot and the primary button', async () => {
      vi.spyOn(api, 'applyProductVariants').mockRejectedValue(
        refused('Orders or stock of this product are being changed right now — try again', 409, 'product_busy'),
      );
      render(<ProductVariantsDialog product={product} onClose={noop} />);
      fireEvent.change(within(group('g1')).getByLabelText('Group'), { target: { value: 'Lid' } });
      fireEvent.click(save());
      expect(await screen.findByRole('alert')).toHaveTextContent('being changed right now');
      await waitFor(() => expect(save()).toHaveFocus());
      expect(save()).toBeEnabled();
    });

    it('a changed revision: «Reload» asks, reads, and only then replaces the draft', async () => {
      vi.spyOn(api, 'applyProductVariants').mockRejectedValue(
        refused('The variants changed since they were opened — reload and try again', 409, 'variants_changed'),
      );
      const fresh = {
        ...product,
        variants_revision: 'rev-2',
        variant_groups: [{ ...product.variant_groups![0], name: 'Cap type' }],
      } as Product;
      const read = vi.spyOn(api, 'getProduct').mockRejectedValueOnce(new Error('HTTP 500')).mockResolvedValueOnce(fresh);
      render(<ProductVariantsDialog product={product} onClose={noop} />);
      fireEvent.change(within(group('g1')).getByLabelText('Group'), { target: { value: 'Lid' } });
      fireEvent.click(save());
      const alert = await screen.findByRole('alert');
      fireEvent.click(within(alert).getByRole('button', { name: 'Reload' }));
      const confirm = await screen.findByRole('dialog', { name: 'Reload the variants?' });
      expect(confirm).toHaveTextContent('Your unsaved changes will be lost.');
      fireEvent.click(within(confirm).getByRole('button', { name: 'Reload' }));
      // A failed read keeps the draft.
      expect(await within(confirm).findByRole('alert')).toHaveTextContent('HTTP 500');
      expect(within(group('g1')).getByLabelText('Group')).toHaveValue('Lid');
      fireEvent.click(within(confirm).getByRole('button', { name: 'Reload' }));
      await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Reload the variants?' })).not.toBeInTheDocument());
      expect(read).toHaveBeenCalledTimes(2);
      expect(within(group('g1')).getByLabelText('Group')).toHaveValue('Cap type');
      expect(screen.queryByTestId('variant-group-g2')).not.toBeInTheDocument();
      // The next save goes against the new revision.
      const apply = vi.spyOn(api, 'applyProductVariants').mockResolvedValue(fresh as never);
      fireEvent.change(within(group('g1')).getByLabelText('Group'), { target: { value: 'Cap' } });
      fireEvent.click(save());
      await waitFor(() => expect(apply).toHaveBeenCalledTimes(2));
      expect(apply.mock.calls[1][1].revision).toBe('rev-2');
    });

    it('under a request nothing closes it and nothing sends twice — decided in the same frame', async () => {
      const apply = vi.spyOn(api, 'applyProductVariants').mockReturnValue(new Promise(() => {}) as never);
      const onClose = vi.fn();
      render(<ProductVariantsDialog product={product} onClose={onClose} />);
      fireEvent.change(within(group('g1')).getByLabelText('Group'), { target: { value: 'Lid' } });
      const submit = save();
      const cancel = screen.getByRole('button', { name: 'Cancel' });
      act(() => {
        submit.click();
        window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
        cancel.click();
        submit.click();
      });
      await waitFor(() => expect(apply).toHaveBeenCalledTimes(1));
      expect(onClose).not.toHaveBeenCalled();
    });
  });
});
