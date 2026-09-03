import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Plus } from 'lucide-react';
import { api } from '../../api/client';
import { useToast } from '../../contexts/ToastContext';
import { ProductPicker } from '../pickers/ProductPicker';
import { Button } from '../Button';

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

  const add = useMutation({
    mutationFn: () =>
      api.addOrderLine(orderId, {
        product_id: productId!,
        quantity,
        // An empty box is "not said", which on the wire is null — an empty
        // string would be a colour named "" that no plate can ever match.
        material: material.trim() || null,
        color: color.trim() || null,
        note: note.trim() || null,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['project', orderId] });
      queryClient.invalidateQueries({ queryKey: ['projects'] });
      setProductId(null);
      setQuantity(1);
      setMaterial('');
      setColor('');
      setNote('');
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
        <Button size="sm" onClick={() => add.mutate()} disabled={productId == null || add.isPending}>
          <Plus className="w-4 h-4" />
          {t('orders.lines.addLine')}
        </Button>
      </td>
    </tr>
  );
}
