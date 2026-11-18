/**
 * «Edit line» (WS-13 E4 D, F03): the draft, the stock ceilings and the locks that
 * lived in the table's cells, moved into a dialog WITHOUT changing a rule. The tests
 * that pinned those rules in the cells are here, addressed to the dialog, with the
 * same assertion; the rest are the dialog's own — its sources' states, the error
 * above the footer, and the step into the configuration dialog.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, fireEvent, waitFor, within } from '@testing-library/react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import userEvent from '@testing-library/user-event';
import { render } from '../../utils';
import { api } from '../../../api/client';
import type { Order, ProjectLine } from '../../../api/client';
import { LineEditDialog } from '../../../components/projects/LineEditDialog';
import { makeLine, makeOrder } from '../../fixtures/orderDetail';

function ProductProbe({ id, onFetch }: { id: number; onFetch: () => void }) {
  useQuery({
    queryKey: ['product', id],
    queryFn: async () => {
      onFetch();
      return null;
    },
  });
  return null;
}

/** Re-reads a product in the background, as a focus refetch would. */
function Refetch({ id }: { id: number }) {
  const client = useQueryClient();
  return (
    <button type="button" onClick={() => void client.invalidateQueries({ queryKey: ['product', id] })}>
      refetch product
    </button>
  );
}

const flask = makeLine({
  id: 10,
  product_id: 1,
  product_name: 'Flask',
  quantity: 2,
  material: 'PETG',
  color: null,
  note: null,
  from_stock_units: 2,
  from_finished: 0,
  from_kit_units: 2,
});
const lid = makeLine({
  id: 11,
  product_id: 2,
  product_name: 'Lid',
  quantity: 4,
  material: null,
  from_stock_units: 0,
  from_finished: 0,
  from_kit_units: 0,
});
const order: Order = makeOrder({ id: 1, status: 'active', lines: [flask, lid] });

// Line 10 holds two kits; line 11 none. `stock` is what is still FREE.
const stock = { kits_available: 1, balances: [], movements: [] };
const suggestion = {
  product_id: 1,
  finished_free: 3,
  kits_free: 1,
  from_finished: 2,
  from_kits: 0,
  to_print: 0,
  position_id: 4,
  position_code: 'SK-0004',
};
const product = { id: 1, name: 'Flask', materials: ['PETG', 'PLA'], colors: ['#FFFFFF', '#000000'] };

function open(line: ProjectLine, over: Partial<Order> = {}, handlers: { onClose?: () => void; onConfigure?: () => void } = {}) {
  const current = { ...order, ...over, lines: order.lines.map((l) => (l.id === line.id ? line : l)) };
  return render(
    <LineEditDialog
      order={current}
      line={line}
      onClose={handlers.onClose ?? vi.fn()}
      onConfigure={handlers.onConfigure ?? vi.fn()}
    />,
  );
}

function save() {
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));
}

describe('LineEditDialog · moved from the table cells', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, 'getProduct').mockResolvedValue(product as never);
    vi.spyOn(api, 'getProductStock').mockResolvedValue(stock as never);
    vi.spyOn(api, 'suggestStock').mockResolvedValue({ items: [{ ...suggestion, from_finished: 0, finished_free: 0 }] });
  });

  it('frames the line: title, product and configuration', async () => {
    open(flask);
    const dialog = await screen.findByRole('dialog', { name: 'Edit line' });
    expect(dialog).toHaveTextContent('Flask · standard configuration');
  });

  it('a save with nothing changed sends no PATCH and just closes', async () => {
    const patch = vi.spyOn(api, 'updateOrderLine').mockResolvedValue(order);
    const onClose = vi.fn();
    open(flask, {}, { onClose });
    save();
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(patch).not.toHaveBeenCalled();
  });

  it('repairs a stored lower-case material on save, beside what was changed', async () => {
    // The fold is in `changedFields`: the plates spell their material upper-case and the
    // server stores what it is given, so saving a legacy `petg` row is the repair.
    const patch = vi.spyOn(api, 'updateOrderLine').mockResolvedValue(order);
    open({ ...lid, material: 'petg' });
    fireEvent.change(await screen.findByLabelText('Note'), { target: { value: 'urgent' } });
    save();
    await waitFor(() => expect(patch).toHaveBeenCalledWith(1, 11, { material: 'PETG', note: 'urgent' }));
  });

  it('sends nothing when the chosen material equals the one already stored', async () => {
    const patch = vi.spyOn(api, 'updateOrderLine').mockResolvedValue(order);
    const onClose = vi.fn();
    open(flask, {}, { onClose });
    const material = await screen.findByLabelText('Material');
    await waitFor(() => expect(within(material).getByRole('option', { name: 'PLA' })).toBeInTheDocument());
    fireEvent.change(material, { target: { value: 'PLA' } });
    fireEvent.change(material, { target: { value: 'PETG' } });
    save();
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(patch).not.toHaveBeenCalled();
  });

  it('offers the free kits PLUS the line own, because an edit releases its reservation first', async () => {
    open(flask);
    const box = (await screen.findByLabelText('From stock — part kits')) as HTMLInputElement;
    expect(box.value).toBe('2');
    // One free + two held = three, capped by the quantity of two.
    await waitFor(() => expect(box.max).toBe('2'));
  });

  it('sends the rewritten reservation, and nothing else', async () => {
    const patch = vi.spyOn(api, 'updateOrderLine').mockResolvedValue(order);
    open(flask);
    const box = await screen.findByLabelText('From stock — part kits');
    await waitFor(() => expect(box).toHaveAttribute('max', '2'));
    fireEvent.change(box, { target: { value: '1' } });
    save();
    await waitFor(() => expect(patch).toHaveBeenCalledWith(1, 10, { from_stock_units: 1 }));
  });

  it('leaves the reservation alone on a save that did not touch it', async () => {
    const patch = vi.spyOn(api, 'updateOrderLine').mockResolvedValue(order);
    open(flask);
    await screen.findByLabelText('From stock — part kits');
    fireEvent.change(screen.getByLabelText('Note'), { target: { value: 'urgent' } });
    save();
    await waitFor(() => expect(patch).toHaveBeenCalledWith(1, 10, { note: 'urgent' }));
  });

  it('lowers the reservation with the quantity, in the same edit', async () => {
    const patch = vi.spyOn(api, 'updateOrderLine').mockResolvedValue(order);
    open(flask);
    const box = (await screen.findByLabelText('From stock — part kits')) as HTMLInputElement;
    expect(box.value).toBe('2');
    const quantity = screen.getByLabelText('Quantity, pcs');
    fireEvent.change(quantity, { target: { value: '1' } });
    // The rule is applied when the box is left, not per keystroke (final review I1).
    fireEvent.blur(quantity);
    expect((screen.getByLabelText('From stock — part kits') as HTMLInputElement).value).toBe('1');
    save();
    await waitFor(() => expect(patch).toHaveBeenCalledWith(1, 10, { quantity: 1, from_stock_units: 1 }));
  });

  it('refetches the product views a rewritten reservation moved', async () => {
    const mine = vi.fn();
    const other = vi.fn();
    vi.spyOn(api, 'updateOrderLine').mockResolvedValue(order);
    render(
      <>
        <ProductProbe id={1} onFetch={mine} />
        <ProductProbe id={99} onFetch={other} />
        <LineEditDialog order={order} line={flask} onClose={vi.fn()} onConfigure={vi.fn()} />
      </>,
    );
    await waitFor(() => expect(mine).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(other).toHaveBeenCalledTimes(1));
    const box = await screen.findByLabelText('From stock — part kits');
    await waitFor(() => expect(box).toHaveAttribute('max', '2'));
    fireEvent.change(box, { target: { value: '1' } });
    save();
    await waitFor(() => expect(other).toHaveBeenCalledTimes(2));
  });

  it('sends the number it is showing, clamped in the draft and not only on screen', async () => {
    const patch = vi.spyOn(api, 'updateOrderLine').mockResolvedValue({
      ...order,
      lines: order.lines.map((l) => (l.id === 11 ? { ...l, from_stock_units: 1, from_kit_units: 1 } : l)),
    });
    open(lid);
    const box = (await screen.findByLabelText('From stock — part kits')) as HTMLInputElement;
    await waitFor(() => expect(box).toHaveAttribute('max', '1'));
    // The field settles on its ceiling when it is left — before Save can read the draft.
    fireEvent.change(box, { target: { value: '9' } });
    fireEvent.blur(box);
    expect(box.value).toBe('1');
    save();
    await waitFor(() => expect(patch).toHaveBeenCalledWith(1, 11, { from_stock_units: 1 }));
    await waitFor(() => expect(screen.queryByText(/could be reserved/i)).not.toBeInTheDocument());
  });

  it('sends no reservation at all when the clamp lands back on what is stored', async () => {
    const patch = vi.spyOn(api, 'updateOrderLine').mockResolvedValue(order);
    const onClose = vi.fn();
    open(flask, {}, { onClose });
    const box = (await screen.findByLabelText('From stock — part kits')) as HTMLInputElement;
    await waitFor(() => expect(box).toHaveAttribute('max', '2'));
    fireEvent.change(box, { target: { value: '7' } });
    fireEvent.blur(box);
    expect(box.value).toBe('2');
    save();
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(patch).not.toHaveBeenCalled();
  });

  it('says so when the shelf emptied and less was reserved than asked', async () => {
    vi.spyOn(api, 'getProductStock').mockResolvedValue({ ...stock, kits_available: 3 } as never);
    vi.spyOn(api, 'updateOrderLine').mockResolvedValue({
      ...order,
      lines: order.lines.map((l) => (l.id === 11 ? { ...l, from_stock_units: 1, from_kit_units: 1 } : l)),
    });
    open(lid);
    const box = await screen.findByLabelText('From stock — part kits');
    await waitFor(() => expect(box).toHaveAttribute('max', '3'));
    fireEvent.change(box, { target: { value: '2' } });
    save();
    expect(await screen.findByText(/only 1 could be reserved/i)).toBeInTheDocument();
  });
});

describe('LineEditDialog · ready units', () => {
  const ready = { ...flask, from_finished: 2, from_kit_units: 0, from_stock_units: 2 };

  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, 'getProduct').mockResolvedValue(product as never);
    vi.spyOn(api, 'getProductStock').mockResolvedValue(stock as never);
  });

  it('edits ready units beside kits, within what is free plus what the line holds', async () => {
    vi.spyOn(api, 'suggestStock').mockResolvedValue({ items: [suggestion] });
    open(ready);
    const box = (await screen.findByLabelText('From stock — ready')) as HTMLInputElement;
    expect(box.value).toBe('2');
    await waitFor(() => expect(box.max).toBe('2'));
    expect(await screen.findByText('available 3 in the position «standard»')).toBeInTheDocument();
  });

  it('«Pick from stock» asks the server for this line and fills both boxes', async () => {
    const suggest = vi.spyOn(api, 'suggestStock').mockResolvedValue({ items: [{ ...suggestion, from_finished: 1, from_kits: 1 }] });
    const patch = vi.spyOn(api, 'updateOrderLine').mockResolvedValue(order);
    open(ready);
    fireEvent.click(await screen.findByRole('button', { name: 'Pick from stock' }));
    await waitFor(() =>
      expect(suggest).toHaveBeenLastCalledWith([{ product_id: 1, options: [], quantity: 2, line_id: 10 }]),
    );
    await waitFor(() => expect(screen.getByLabelText('From stock — ready')).toHaveValue(1));
    expect(screen.getByLabelText('From stock — part kits')).toHaveValue(1);
    save();
    await waitFor(() => expect(patch).toHaveBeenCalledWith(1, 10, { from_stock_units: 1, from_finished: 1 }));
  });

  it('sends the ready units only when they moved', async () => {
    vi.spyOn(api, 'suggestStock').mockResolvedValue({ items: [suggestion] });
    const patch = vi.spyOn(api, 'updateOrderLine').mockResolvedValue(order);
    open(ready);
    await screen.findByLabelText('From stock — ready');
    fireEvent.change(screen.getByLabelText('Note'), { target: { value: 'urgent' } });
    save();
    await waitFor(() => expect(patch).toHaveBeenCalledWith(1, 10, { note: 'urgent' }));
  });

  it('an order that is not active keeps its ready units and says why', async () => {
    open(ready, { status: 'completed' });
    expect(await screen.findByLabelText('From stock — ready')).toBeDisabled();
    // A hint under a field, lower-case like every other hint of the dialog (final review M8).
    expect(screen.getByText('only an active order takes ready units from stock')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Pick from stock' })).not.toBeInTheDocument();
  });

  it('lowers the quantity of a completed order without touching its ready units', async () => {
    const patch = vi.spyOn(api, 'updateOrderLine').mockResolvedValue(order);
    open(ready, { status: 'completed' });
    fireEvent.change(await screen.findByLabelText('Quantity, pcs'), { target: { value: '1' } });
    save();
    await waitFor(() => expect(patch).toHaveBeenCalledWith(1, 10, { quantity: 1 }));
  });

  it('says one ready unit, not «1 ready units», when the shelf gave less', async () => {
    vi.spyOn(api, 'suggestStock').mockResolvedValue({ items: [suggestion] });
    vi.spyOn(api, 'updateOrderLine').mockResolvedValue({
      ...order,
      lines: order.lines.map((l) => (l.id === 10 ? { ...l, from_finished: 1, from_stock_units: 1 } : l)),
    });
    open({ ...ready, from_finished: 0, from_stock_units: 0 });
    const box = await screen.findByLabelText('From stock — ready');
    await waitFor(() => expect(box).toHaveAttribute('max', '2'));
    fireEvent.change(box, { target: { value: '2' } });
    save();
    expect(await screen.findByText('Only 1 ready unit could be reserved — the shelf had no more.')).toBeInTheDocument();
  });
});

describe('LineEditDialog · a line whose stock has moved', () => {
  // 6 ordered: 2 issued, 2 held on the shelf for the order.
  const moved = { ...flask, quantity: 6, assembled: 1, received: 1, issued: 2, held: 2, from_finished: 2, from_kit_units: 2 };

  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, 'getProduct').mockResolvedValue(product as never);
    vi.spyOn(api, 'getProductStock').mockResolvedValue(stock as never);
    vi.spyOn(api, 'suggestStock').mockResolvedValue({ items: [suggestion] });
  });

  it('keeps its ready units, and its quantity above what went out', async () => {
    const patch = vi.spyOn(api, 'updateOrderLine').mockResolvedValue(order);
    open(moved);
    const quantity = await screen.findByLabelText('Quantity, pcs');
    expect(screen.queryByLabelText('From stock — ready')).not.toBeInTheDocument();
    // One live kit (2 from kits, 1 of them assembled) — it may only come down.
    expect(screen.getByLabelText('From stock — part kits')).toHaveAttribute('max', '1');
    // A hint under a field, lower-case like every other hint of the dialog.
    expect(screen.getByText('kits can only be lowered once the line’s stock has moved')).toBeInTheDocument();
    expect(quantity).toHaveAttribute('min', '4');
    fireEvent.change(quantity, { target: { value: '8' } });
    save();
    await waitFor(() => expect(patch).toHaveBeenCalledWith(1, 10, { quantity: 8 }));
  });

  it('lowers a moved line’s kits and never raises them', async () => {
    const patch = vi.spyOn(api, 'updateOrderLine').mockResolvedValue(order);
    open(moved);
    const box = await screen.findByLabelText('From stock — part kits');
    fireEvent.change(box, { target: { value: '5' } });
    fireEvent.blur(box);
    expect(box).toHaveValue(1);
    fireEvent.change(box, { target: { value: '0' } });
    save();
    await waitFor(() => expect(patch).toHaveBeenCalledWith(1, 10, { from_stock_units: 0 }));
  });

  it('a moved line with no live kits says why its stock numbers are closed', async () => {
    open({ ...moved, from_kit_units: 1 });
    await screen.findByLabelText('Quantity, pcs');
    expect(screen.queryByLabelText('From stock — part kits')).not.toBeInTheDocument();
    expect(
      screen.getByText('The line’s stock has already moved — to take more, press «Take from stock» on the order card'),
    ).toBeInTheDocument();
  });
});

describe('LineEditDialog · its own rules (E4 D02, D03, D05)', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, 'getProductStock').mockResolvedValue(stock as never);
    vi.spyOn(api, 'suggestStock').mockResolvedValue({ items: [suggestion] });
  });

  it('offers the product’s materials and colours, the current value and «any», and sends a colour’s code', async () => {
    vi.spyOn(api, 'getProduct').mockResolvedValue(product as never);
    const patch = vi.spyOn(api, 'updateOrderLine').mockResolvedValue(order);
    open({ ...flask, material: 'ABS', color: 'Ocean' });
    const material = await screen.findByLabelText('Material');
    await waitFor(() => expect(within(material).getAllByRole('option')).toHaveLength(4));
    expect(within(material).getAllByRole('option').map((o) => o.textContent)).toEqual(['PETG', 'PLA', 'ABS', 'any']);
    const color = screen.getByLabelText('Colour');
    expect(within(color).getAllByRole('option').map((o) => [o.getAttribute('value'), o.textContent])).toEqual([
      ['#FFFFFF', 'White'],
      ['#000000', 'Black'],
      ['Ocean', 'Ocean'],
      ['', 'any'],
    ]);
    expect(screen.getByText('a hard filter for plates')).toBeInTheDocument();
    expect(screen.getByText('a hint, not a filter')).toBeInTheDocument();
    fireEvent.change(color, { target: { value: '#000000' } });
    save();
    await waitFor(() => expect(patch).toHaveBeenCalledWith(1, 10, { color: '#000000' }));
  });

  it('while the product is read, offers only the current value and «any», and says so', async () => {
    vi.spyOn(api, 'getProduct').mockReturnValue(new Promise(() => {}));
    open(flask);
    const material = await screen.findByLabelText('Material');
    expect(within(material).getAllByRole('option').map((o) => o.textContent)).toEqual(['PETG', 'any']);
    expect(screen.getAllByText('the product’s options are still being read').length).toBeGreaterThan(0);
  });

  it('when the product cannot be read, offers the same two and says it failed — not an empty list', async () => {
    vi.spyOn(api, 'getProduct').mockRejectedValue(new Error('boom'));
    open(flask);
    expect((await screen.findAllByText('could not read the product’s materials')).length).toBeGreaterThan(0);
    expect(within(screen.getByLabelText('Material')).getAllByRole('option').map((o) => o.textContent)).toEqual([
      'PETG',
      'any',
    ]);
  });

  it('tells a stock still being read, a stock that failed and an empty stock apart', async () => {
    vi.spyOn(api, 'getProduct').mockResolvedValue(product as never);
    vi.spyOn(api, 'getProductStock').mockReturnValue(new Promise(() => {}));
    const { unmount } = open(lid);
    expect(await screen.findByText('availability is still being read')).toBeInTheDocument();
    expect(screen.queryByText(/available 0/)).not.toBeInTheDocument();
    unmount();

    vi.spyOn(api, 'getProductStock').mockRejectedValue(new Error('boom'));
    const second = open(lid);
    // A hint under a field, lower-case like every other hint of the dialog.
    expect(await screen.findByText('could not read the stock')).toBeInTheDocument();
    second.unmount();

    vi.spyOn(api, 'getProductStock').mockResolvedValue({ ...stock, kits_available: 0 } as never);
    open(lid);
    expect(await screen.findByText('available 0 for this configuration')).toBeInTheDocument();
  });

  it('keeps the draft when the product is read again in the background', async () => {
    const get = vi.spyOn(api, 'getProduct').mockResolvedValue(product as never);
    render(
      <>
        <Refetch id={1} />
        <LineEditDialog order={order} line={flask} onClose={vi.fn()} onConfigure={vi.fn()} />
      </>,
    );
    const material = await screen.findByLabelText('Material');
    await waitFor(() => expect(within(material).getAllByRole('option')).toHaveLength(3));
    fireEvent.change(material, { target: { value: 'PLA' } });
    fireEvent.change(screen.getByLabelText('Note'), { target: { value: 'typed' } });
    get.mockResolvedValue({ ...product, materials: ['ASA'] } as never);
    fireEvent.click(screen.getByRole('button', { name: 'refetch product' }));
    await waitFor(() => expect(within(material).getByRole('option', { name: 'ASA' })).toBeInTheDocument());
    expect(material).toHaveValue('PLA');
    expect(screen.getByLabelText('Note')).toHaveValue('typed');
  });

  it('shows a refusal above the footer and keeps the draft; Escape writes nothing', async () => {
    vi.spyOn(api, 'getProduct').mockResolvedValue(product as never);
    const patch = vi.spyOn(api, 'updateOrderLine').mockRejectedValue(new Error('Quantity is below what was issued'));
    const onClose = vi.fn();
    open(flask, {}, { onClose });
    fireEvent.change(await screen.findByLabelText('Note'), { target: { value: 'urgent' } });
    save();
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Quantity is below what was issued');
    expect(screen.getByLabelText('Note')).toHaveValue('urgent');
    expect(onClose).not.toHaveBeenCalled();
    expect(patch).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(window, { key: 'Escape' });
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(patch).toHaveBeenCalledTimes(1);
  });

  describe('a parts line', () => {
    const partsLine = makeLine({
      ...lid,
      mode: 'parts',
      parts: [
        {
          part_id: 1,
          name: 'flask',
          qty_per_unit: 2,
          need: 2,
          usable: 0,
          in_progress: 0,
          remaining: 2,
          surplus: 0,
          variant: false,
          queued: 0,
          bankable: 0,
        },
      ],
    });

    beforeEach(() => {
      vi.spyOn(api, 'getProduct').mockResolvedValue(product as never);
    });

    it('lists its parts instead of quantity and stock fields, and keeps material, colour and note', async () => {
      open(partsLine);
      const dialog = await screen.findByRole('dialog', { name: 'Edit line' });
      expect(dialog).toHaveTextContent('Lid · parts only');
      expect(within(dialog).getByText('flask × 2')).toBeInTheDocument();
      expect(screen.queryByLabelText('Quantity, pcs')).not.toBeInTheDocument();
      expect(screen.queryByLabelText('From stock — part kits')).not.toBeInTheDocument();
      expect(screen.getByLabelText('Material')).toBeInTheDocument();
      expect(screen.getByLabelText('Colour')).toBeInTheDocument();
      expect(screen.getByLabelText('Note')).toBeInTheDocument();
    });

    it('goes straight to the configuration from a clean draft', async () => {
      const onConfigure = vi.fn();
      open(partsLine, {}, { onConfigure });
      fireEvent.click(await screen.findByRole('button', { name: 'Change part quantities…' }));
      expect(onConfigure).toHaveBeenCalledTimes(1);
      expect(screen.queryByText('Discard unsaved changes to the line?')).not.toBeInTheDocument();
    });

    it('asks before dropping a dirty draft: «Stay» keeps the fields, «Discard» sends nothing', async () => {
      const onConfigure = vi.fn();
      const patch = vi.spyOn(api, 'updateOrderLine').mockResolvedValue(order);
      open(partsLine, {}, { onConfigure });
      fireEvent.change(await screen.findByLabelText('Note'), { target: { value: 'spares' } });
      fireEvent.click(screen.getByRole('button', { name: 'Change part quantities…' }));
      const ask = await screen.findByRole('dialog', { name: 'Discard unsaved changes to the line?' });
      // The Workshop's one confirmation (E13 E02), not the generic ConfirmModal.
      expect(ask.querySelector('[data-workshop-dialog-footer]')).not.toBeNull();
      fireEvent.click(within(ask).getByRole('button', { name: 'Stay' }));
      await waitFor(() =>
        expect(screen.queryByRole('dialog', { name: 'Discard unsaved changes to the line?' })).not.toBeInTheDocument(),
      );
      expect(screen.getByLabelText('Note')).toHaveValue('spares');
      expect(onConfigure).not.toHaveBeenCalled();

      fireEvent.click(screen.getByRole('button', { name: 'Change part quantities…' }));
      fireEvent.click(
        within(await screen.findByRole('dialog', { name: 'Discard unsaved changes to the line?' })).getByRole('button', {
          name: 'Discard and continue',
        }),
      );
      await waitFor(() => expect(onConfigure).toHaveBeenCalledTimes(1));
      expect(patch).not.toHaveBeenCalled();
    });

    it('closes the step to the configuration where the menu closes it, and says why', async () => {
      const { unmount } = open(partsLine, { status: 'completed' });
      let button = await screen.findByRole('button', { name: 'Change part quantities…' });
      expect(button).toBeDisabled();
      expect(button).toHaveAttribute('title', 'The order is completed — reopen it to change a line’s configuration');
      expect(screen.getByText('The order is completed — reopen it to change a line’s configuration')).toBeInTheDocument();
      unmount();

      open({ ...partsLine, issued: 1 });
      button = await screen.findByRole('button', { name: 'Change part quantities…' });
      expect(button).toBeDisabled();
      expect(button).toHaveAttribute('title', "This line's stock has moved — take more from stock instead");
    });
  });
});

describe('LineEditDialog · final review (I1, M3, M4, M9)', () => {
  // Ten units, eight kits held; one more kit free — a pool of nine.
  const big = { ...flask, id: 12, quantity: 10, from_stock_units: 8, from_kit_units: 8 };

  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, 'getProduct').mockResolvedValue(product as never);
    vi.spyOn(api, 'getProductStock').mockResolvedValue(stock as never);
    vi.spyOn(api, 'suggestStock').mockResolvedValue({ items: [{ ...suggestion, from_finished: 0, finished_free: 0 }] });
  });

  it('retyping a quantity through a smaller number keeps the reservation (I1)', async () => {
    const patch = vi.spyOn(api, 'updateOrderLine').mockResolvedValue(order);
    open(big);
    const kits = (await screen.findByLabelText('From stock — part kits')) as HTMLInputElement;
    await waitFor(() => expect(kits).toHaveAttribute('max', '9'));
    const quantity = screen.getByLabelText('Quantity, pcs');
    // 10 → 12 typed the usual way passes through «1».
    fireEvent.change(quantity, { target: { value: '1' } });
    fireEvent.change(quantity, { target: { value: '12' } });
    expect(kits.value).toBe('8');
    fireEvent.blur(quantity);
    expect(kits.value).toBe('8');
    save();
    await waitFor(() => expect(patch).toHaveBeenCalledWith(1, 12, { quantity: 12 }));
  });

  it('applies the quantity rule on Save even when the box was never left (I1)', async () => {
    const patch = vi.spyOn(api, 'updateOrderLine').mockResolvedValue(order);
    open(big);
    await screen.findByLabelText('From stock — part kits');
    fireEvent.change(screen.getByLabelText('Quantity, pcs'), { target: { value: '5' } });
    save();
    await waitFor(() => expect(patch).toHaveBeenCalledWith(1, 12, { quantity: 5, from_stock_units: 5 }));
  });

  it('a moved line’s quantity is not snapped to its floor while it is typed (I1)', async () => {
    const patch = vi.spyOn(api, 'updateOrderLine').mockResolvedValue(order);
    // 6 ordered, 2 issued, 2 held: the floor is 4.
    open({ ...flask, quantity: 6, assembled: 1, received: 1, issued: 2, held: 2, from_finished: 2, from_kit_units: 2 });
    const quantity = await screen.findByLabelText('Quantity, pcs');
    // Typed key by key: clamped per keystroke, «1» became 4 and «12» became «42».
    await userEvent.clear(quantity);
    await userEvent.type(quantity, '12');
    expect(quantity).toHaveValue(12);
    save();
    await waitFor(() => expect(patch).toHaveBeenCalledWith(1, 10, { quantity: 12 }));
  });

  it('says why a moved line’s quantity cannot go lower (M9)', async () => {
    open({ ...flask, quantity: 6, assembled: 1, received: 1, issued: 2, held: 2, from_finished: 2, from_kit_units: 2 });
    await screen.findByLabelText('Quantity, pcs');
    expect(screen.getByText('at least 4 — issued and held on the shelf for the order')).toBeInTheDocument();
  });

  it('writes back only what the operator changed, not what changed elsewhere meanwhile (M3)', async () => {
    const patch = vi.spyOn(api, 'updateOrderLine').mockResolvedValue(order);
    const view = open(big);
    await screen.findByLabelText('From stock — part kits');
    // Another session: a note, another material, and a kit assembled off the reservation.
    const fresh = { ...big, note: 'from elsewhere', material: 'PLA', from_kit_units: 7 };
    view.rerender(
      <LineEditDialog order={{ ...order, lines: [fresh, lid] }} line={fresh} onClose={vi.fn()} onConfigure={vi.fn()} />,
    );
    fireEvent.change(screen.getByLabelText('Quantity, pcs'), { target: { value: '11' } });
    save();
    await waitFor(() => expect(patch).toHaveBeenCalledWith(1, 12, { quantity: 11 }));
  });

  it('does not «repair» a lower-case material somebody else has replaced meanwhile (M3)', async () => {
    const patch = vi.spyOn(api, 'updateOrderLine').mockResolvedValue(order);
    const legacy = { ...lid, material: 'petg' };
    const view = open(legacy);
    await screen.findByLabelText('Note');
    const fresh = { ...legacy, material: 'PLA' };
    view.rerender(
      <LineEditDialog order={{ ...order, lines: [flask, fresh] }} line={fresh} onClose={vi.fn()} onConfigure={vi.fn()} />,
    );
    fireEvent.change(screen.getByLabelText('Note'), { target: { value: 'urgent' } });
    save();
    await waitFor(() => expect(patch).toHaveBeenCalledWith(1, 11, { note: 'urgent' }));
  });

  it('keeps offering the line’s own off-catalogue material after another is picked (M4)', async () => {
    open({ ...flask, material: 'PLA-CF' });
    const material = await screen.findByLabelText('Material');
    await waitFor(() => expect(within(material).getByRole('option', { name: 'PETG' })).toBeInTheDocument());
    fireEvent.change(material, { target: { value: 'PETG' } });
    expect(within(material).getByRole('option', { name: 'PLA-CF' })).toBeInTheDocument();
    fireEvent.change(material, { target: { value: 'PLA-CF' } });
    expect(material).toHaveValue('PLA-CF');
  });
});
