import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Plus } from 'lucide-react';
import { api } from '../../api/client';
import type { Order, ProjectLine } from '../../api/client';
import { useToast } from '../../contexts/ToastContext';
import { useProductStock } from '../../hooks/useProductStock';
import { ProductPicker } from '../pickers/ProductPicker';
import { Button } from '../Button';
import { invalidateOrderViews } from '../../utils/queryInvalidation';

const FIELD_CLASS =
  'w-full px-2 py-1.5 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm focus:border-bambu-green focus:outline-none';

/**
 * The last row of the lines table: pick a product, say how many.
 *
 * ⚠️ **The material is upper-cased on blur, not on submit.** It is matched
 * against the plate's own filament tokens, which the server upper-cases
 * (`plate_materials` in `product_composition.py`) — so `petg` typed here and
 * `PETG` on the plate have to end up the same string. Doing it on blur means
 * the operator SEES the value that will be sent, instead of discovering after
 * the fact that the field they filled in was rewritten.
 *
 * A material no plate of the product carries is deliberately NOT rejected: the
 * server has no such rule either, and the plan block (pass 3) is where "no
 * plate for this part in this material" is surfaced, with the plates in hand
 * to say it properly.
 *
 * ⚠️ **«From stock» is offered only when there IS stock**, and it defaults to
 * `min(kits_available, quantity)` — the operator's usual answer is "take what
 * is on the shelf" (pass 8, Decision 4). The box is editable down to 0 because
 * the other answer, "keep the shelf for something else", is theirs to give.
 */
export function AddLineRow({ orderId }: { orderId: number }) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();

  const [productId, setProductId] = useState<number | null>(null);
  const [quantity, setQuantity] = useState(1);
  const [material, setMaterial] = useState('');
  const [color, setColor] = useState('');
  const [note, setNote] = useState('');
  /** `null` is "the operator has not touched the box", which is what makes the
   *  default follow the quantity as it is typed. A typed 0 is not null. */
  const [fromStock, setFromStock] = useState<number | null>(null);

  // Only once a product is picked — a disabled query in TanStack v5 is pending
  // and NOT fetching, so nothing is asked for until there is something to ask
  // about. The hook owns the key; see `useProductStock`.
  const { data: stock } = useProductStock(productId);
  const kits = stock?.kits_available ?? 0;
  // Clamped against BOTH the shelf and the line: reserving more kits than the
  // line will ship is not a reservation, it is stock taken out of circulation.
  // The server clamps too — this is what the operator SEES it will send.
  const reserve = Math.min(fromStock ?? quantity, kits, quantity);

  const add = useMutation({
    mutationFn: (units: number) =>
      api.addOrderLine(orderId, {
        product_id: productId!,
        quantity,
        // Folded here as well as on blur: blur is what the operator SEES, this
        // is what actually goes on the wire, and the two must not be able to
        // disagree — a submit that never blurred the field (Enter, or a click
        // straight from the picker) would otherwise send the raw casing. The
        // inline-edit path folds it in `changedFields` for the same reason.
        //
        // An empty box is "not said", which on the wire is null — an empty
        // string would be a colour named "" that no plate can ever match.
        material: material.trim().toUpperCase() || null,
        color: color.trim() || null,
        note: note.trim() || null,
        // ⚠️ Sent only when there is something to reserve. The server defaults
        // it to 0, so a `from_stock_units: 0` on every line would be a field
        // nobody typed riding on every request — and the reservation path would
        // run for products that hold no stock at all.
        ...(units > 0 ? { from_stock_units: units } : {}),
      }),
    onSuccess: (saved: Order, units: number) => {
      // ⚠️ The whole set, not the order alone: a new line is new work, so the
      // plan block has a part to plan that it does not know about yet. The
      // product's shelf moved too when kits were reserved.
      invalidateOrderViews(queryClient, { orderId });
      if (units > 0) {
        queryClient.invalidateQueries({ queryKey: ['product-stock', productId] });
        queryClient.invalidateQueries({ queryKey: ['product', productId] });
        queryClient.invalidateQueries({ queryKey: ['products'] });
        // What was ACTUALLY reserved can be less than what was asked: the shelf
        // may have emptied between this row rendering and Save. The server
        // answers with the whole order, and the new line is its highest id —
        // the row itself is about to reset, so the number is said in a toast
        // rather than left in a box nobody will look at again.
        const created = saved.lines.reduce<ProjectLine | null>(
          (best, line) => (best && best.id > line.id ? best : line),
          null,
        );
        if (created && created.from_stock_units < units) {
          showToast(t('stock.line.clamped', { n: created.from_stock_units }), 'warning');
        }
      }
      setProductId(null);
      setQuantity(1);
      setMaterial('');
      setColor('');
      setNote('');
      setFromStock(null);
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  return (
    <tr className="border-t border-bambu-dark-tertiary align-top">
      <td className="p-2 min-w-[14rem]">
        <p className="text-xs text-bambu-gray mb-1">{t('orders.lines.add')}</p>
        <ProductPicker value={productId} onChange={setProductId} disabled={add.isPending} allowCreate />
      </td>
      <td className="p-2">
        <label className="sr-only" htmlFor="add-line-quantity">
          {t('orders.lines.quantity')}
        </label>
        <input
          id="add-line-quantity"
          type="number"
          min={1}
          value={quantity}
          onChange={(e) => setQuantity(Math.max(1, Number(e.target.value) || 1))}
          disabled={add.isPending}
          className={`${FIELD_CLASS} w-20`}
        />
        {/* Only when the shelf has something. `> 0`, never a bare `&&` on the
            number itself — `{0 && …}` renders the 0. */}
        {kits > 0 && (
          <div className="mt-1">
            <label className="block text-xs text-bambu-gray" htmlFor="add-line-from-stock">
              {t('stock.line.label')}
            </label>
            <input
              id="add-line-from-stock"
              data-testid="add-line-from-stock"
              type="number"
              min={0}
              max={Math.min(kits, quantity)}
              value={reserve}
              onChange={(e) => setFromStock(Math.max(0, Number(e.target.value) || 0))}
              disabled={add.isPending}
              className={`${FIELD_CLASS} w-20`}
            />
            <p className="text-xs text-bambu-gray mt-0.5">{t('stock.line.available', { n: kits })}</p>
          </div>
        )}
      </td>
      <td className="p-2">
        <label className="sr-only" htmlFor="add-line-material">
          {t('orders.lines.material')}
        </label>
        <input
          id="add-line-material"
          type="text"
          value={material}
          onChange={(e) => setMaterial(e.target.value)}
          onBlur={() => setMaterial((m) => m.trim().toUpperCase())}
          disabled={add.isPending}
          className={`${FIELD_CLASS} w-24`}
        />
      </td>
      <td className="p-2">
        <label className="sr-only" htmlFor="add-line-color">
          {t('orders.lines.color')}
        </label>
        <input
          id="add-line-color"
          type="text"
          value={color}
          onChange={(e) => setColor(e.target.value)}
          disabled={add.isPending}
          className={`${FIELD_CLASS} w-24`}
        />
      </td>
      <td className="p-2">
        <label className="sr-only" htmlFor="add-line-note">
          {t('orders.lines.note')}
        </label>
        <input
          id="add-line-note"
          type="text"
          value={note}
          onChange={(e) => setNote(e.target.value)}
          disabled={add.isPending}
          className={FIELD_CLASS}
        />
      </td>
      <td className="p-2" />
      <td className="p-2 text-right">
        <Button size="sm" onClick={() => add.mutate(reserve)} disabled={productId == null || add.isPending}>
          <Plus className="w-4 h-4" />
          {t('orders.lines.addLine')}
        </Button>
      </td>
    </tr>
  );
}
