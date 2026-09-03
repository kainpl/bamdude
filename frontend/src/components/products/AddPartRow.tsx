import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Plus } from 'lucide-react';
import { api } from '../../api/client';
import type { ProductPartKind } from '../../api/client';
import { useToast } from '../../contexts/ToastContext';
import { Button } from '../Button';

const FIELD_CLASS =
  'px-2 py-1.5 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm focus:border-bambu-green focus:outline-none';

interface AddPartRowProps {
  productId: number;
  canEdit: boolean;
}

/**
 * The last row of the composition: what kind of part, called what, how many.
 *
 * ⚠️ **Not a `<tr>`.** Printed and purchased parts are two tables with
 * different columns, and one add-row that switches its own fields belongs to
 * neither of them — dropping it into one would either grow phantom columns on
 * that table or line the fields up under the wrong headers. It renders as its
 * own strip under both, which is where it reads as "add a part to this
 * product" rather than "add a row to this table".
 *
 * ⚠️ **`qty_per_unit` may legitimately be 0.** The field's floor is 0, not 1:
 * an object that is printed alongside the product but is not part of it is
 * exactly what zero means (see `CompositionTable`), and refusing it here would
 * make the state reachable only by editing a row after creating it wrong.
 *
 * The price / url / remarks fields belong to a purchased part alone and are
 * hidden for a printed one — the server accepts them either way, but a printed
 * part with a sourcing URL is a contradiction the UI should not offer.
 */
export function AddPartRow({ productId, canEdit }: AddPartRowProps) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();

  const [kind, setKind] = useState<ProductPartKind>('printed');
  const [name, setName] = useState('');
  const [qty, setQty] = useState(1);
  const [price, setPrice] = useState('');
  const [url, setUrl] = useState('');
  const [remarks, setRemarks] = useState('');

  const add = useMutation({
    mutationFn: () => {
      const parsedPrice = Number(price.trim());
      return api.createProductPart(productId, {
        kind,
        name: name.trim(),
        qty_per_unit: qty,
        // An empty box is "not said", which on the wire is null — `0` would be
        // a part that is genuinely free, and `""` is not a number at all.
        unit_price: kind === 'purchased' && price.trim() !== '' && Number.isFinite(parsedPrice) ? parsedPrice : null,
        sourcing_url: kind === 'purchased' ? url.trim() || null : null,
        remarks: kind === 'purchased' ? remarks.trim() || null : null,
      });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['product', productId] });
      queryClient.invalidateQueries({ queryKey: ['product-plates', productId] });
      setName('');
      setQty(1);
      setPrice('');
      setUrl('');
      setRemarks('');
    },
    // A name (or alias) another part already owns answers 409 — the server's
    // own sentence, with the form left holding what was typed.
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  if (!canEdit) return null;

  return (
    <div
      data-testid="add-part-row"
      className="space-y-2 rounded-xl border border-dashed border-bambu-dark-tertiary p-3"
    >
      <p className="text-xs text-bambu-gray">{t('products.composition.addPart')}</p>
      <div className="flex items-end gap-2 flex-wrap">
        <div>
          <label className="block text-xs text-bambu-gray mb-1" htmlFor="add-part-kind">
            {t('products.composition.kind')}
          </label>
          <select
            id="add-part-kind"
            value={kind}
            onChange={(e) => setKind(e.target.value as ProductPartKind)}
            disabled={add.isPending}
            className={FIELD_CLASS}
          >
            <option value="printed">{t('products.composition.printed')}</option>
            <option value="purchased">{t('products.composition.purchased')}</option>
          </select>
        </div>

        <div className="min-w-[12rem] flex-1">
          <label className="block text-xs text-bambu-gray mb-1" htmlFor="add-part-name">
            {t('products.composition.name')}
          </label>
          <input
            id="add-part-name"
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            disabled={add.isPending}
            className={`${FIELD_CLASS} w-full`}
          />
        </div>

        <div>
          <label className="block text-xs text-bambu-gray mb-1" htmlFor="add-part-qty">
            {t('products.composition.perUnit')}
          </label>
          <input
            id="add-part-qty"
            type="number"
            min={0}
            value={qty}
            onChange={(e) => setQty(Math.max(0, Number(e.target.value) || 0))}
            disabled={add.isPending}
            className={`${FIELD_CLASS} w-20 text-right tabular-nums`}
          />
        </div>

        {kind === 'purchased' && (
          <>
            <div>
              <label className="block text-xs text-bambu-gray mb-1" htmlFor="add-part-price">
                {t('products.composition.unitPrice')}
              </label>
              <input
                id="add-part-price"
                type="number"
                min={0}
                step="0.01"
                value={price}
                onChange={(e) => setPrice(e.target.value)}
                disabled={add.isPending}
                className={`${FIELD_CLASS} w-24 text-right tabular-nums`}
              />
            </div>
            <div className="min-w-[10rem]">
              <label className="block text-xs text-bambu-gray mb-1" htmlFor="add-part-url">
                {t('products.composition.sourcingUrl')}
              </label>
              <input
                id="add-part-url"
                type="url"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                disabled={add.isPending}
                className={`${FIELD_CLASS} w-full`}
              />
            </div>
            <div className="min-w-[10rem]">
              <label className="block text-xs text-bambu-gray mb-1" htmlFor="add-part-remarks">
                {t('products.composition.remarks')}
              </label>
              <input
                id="add-part-remarks"
                type="text"
                value={remarks}
                onChange={(e) => setRemarks(e.target.value)}
                disabled={add.isPending}
                className={`${FIELD_CLASS} w-full`}
              />
            </div>
          </>
        )}

        <Button size="sm" onClick={() => add.mutate()} disabled={name.trim() === '' || add.isPending}>
          <Plus className="w-4 h-4" />
          {t('products.composition.add')}
        </Button>
      </div>
    </div>
  );
}
