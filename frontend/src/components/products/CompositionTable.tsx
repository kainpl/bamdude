import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { ExternalLink, Trash2, X } from 'lucide-react';
import { api } from '../../api/client';
import type { Product, ProductPart, ProductPartUpdate } from '../../api/client';
import { useToast } from '../../contexts/ToastContext';
import { formatMoney } from '../../utils/currency';
import { ConfirmModal } from '../ConfirmModal';
import { AddPartRow } from './AddPartRow';

const FIELD_CLASS =
  'px-2 py-1 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm focus:border-bambu-green focus:outline-none disabled:opacity-60';
const CHIP_CLASS = 'inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-bambu-dark-tertiary text-bambu-gray';
const HEAD_CLASS = 'px-3 py-2 font-normal text-left';

/** `sort_order` is the authority; `id` only breaks its ties, so two parts that
 *  share a position keep a stable order instead of swapping on every render. */
function byOrder(a: ProductPart, b: ProductPart): number {
  return a.sort_order - b.sort_order || a.id - b.id;
}

interface CompositionTableProps {
  product: Product;
  canEdit: boolean;
}

/**
 * What one unit of the product is made of.
 *
 * ⚠️ **`qty_per_unit = 0` is a value, not a blank.** It means "this object
 * turns up on the plates but is not part of the product" — a spare, a test
 * cube, a jig — and the migration produced plenty of them from old targets. It
 * is therefore rendered WITH a hint saying so, never as an empty box that
 * invites an operator to "fix" a row that was already correct.
 *
 * ⚠️ **Aliases exist only on printed parts.** The server answers 400 to an
 * alias POST on a purchased one, because a purchased part is matched by nothing
 * on a plate. The input is not offered rather than offered-and-refused.
 *
 * ⚠️ **Merge sends THIS part as the source.** `mergeProductPart(productId,
 * target, source)` folds the source's aliases into the target and deletes the
 * source, so the row you press it on is the one that disappears — which is why
 * it asks first, naming both.
 *
 * Every mutation invalidates `['product', id]` AND `['product-plates', id]`:
 * a part's name, aliases or existence changes what the plate walk matches, so
 * the plates below are stale the moment a row here is touched.
 */
export function CompositionTable({ product, canEdit }: CompositionTableProps) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();
  // The app-wide currency, fetched the way every other money-showing screen
  // fetches it; `formatMoney` covers the unresolved first paint.
  const { data: settings } = useQuery({ queryKey: ['settings'], queryFn: api.getSettings, staleTime: 60_000 });

  const [aliasOpen, setAliasOpen] = useState<number | null>(null);
  const [deleting, setDeleting] = useState<ProductPart | null>(null);
  const [merging, setMerging] = useState<{ source: ProductPart; target: ProductPart } | null>(null);

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['product', product.id] });
    queryClient.invalidateQueries({ queryKey: ['product-plates', product.id] });
  };
  const fail = (e: Error) => showToast(e.message, 'error');

  const save = useMutation({
    mutationFn: ({ partId, data }: { partId: number; data: ProductPartUpdate }) =>
      api.updateProductPart(product.id, partId, data),
    onSuccess: invalidate,
    onError: fail,
  });

  const addAlias = useMutation({
    mutationFn: ({ partId, nameKey }: { partId: number; nameKey: string }) =>
      api.addProductPartAlias(product.id, partId, nameKey),
    onSuccess: () => {
      invalidate();
      setAliasOpen(null);
    },
    // A 409 ("that name already belongs to another part") is the server's own
    // sentence in a toast, with the input left open and still holding what was
    // typed — the operator's next move is to edit it, not to type it again.
    onError: fail,
  });

  const removeAlias = useMutation({
    mutationFn: ({ partId, nameKey }: { partId: number; nameKey: string }) =>
      api.removeProductPartAlias(product.id, partId, nameKey),
    onSuccess: invalidate,
    onError: fail,
  });

  const merge = useMutation({
    mutationFn: ({ source, target }: { source: ProductPart; target: ProductPart }) =>
      api.mergeProductPart(product.id, target.id, source.id),
    onSuccess: () => {
      invalidate();
      setMerging(null);
    },
    onError: fail,
  });

  const remove = useMutation({
    mutationFn: (partId: number) => api.deleteProductPart(product.id, partId),
    onSuccess: () => {
      invalidate();
      setDeleting(null);
    },
    onError: fail,
  });

  const printed = product.parts.filter((p) => p.kind === 'printed').sort(byOrder);
  const purchased = product.parts.filter((p) => p.kind === 'purchased').sort(byOrder);

  /** Both inline fields commit the same way: an abandoned edit patches nothing. */
  const commitName = (part: ProductPart, raw: string) => {
    const name = raw.trim();
    if (name === '' || name === part.name) return;
    save.mutate({ partId: part.id, data: { name } });
  };
  const commitQty = (part: ProductPart, raw: string) => {
    const next = Number(raw.trim());
    if (raw.trim() === '' || !Number.isInteger(next) || next < 0 || next === part.qty_per_unit) return;
    save.mutate({ partId: part.id, data: { qty_per_unit: next } });
  };

  const nameCell = (part: ProductPart) => (
    <td className="px-3 py-2">
      <div className="flex items-center gap-2 flex-wrap">
        <input
          key={part.name}
          type="text"
          defaultValue={part.name}
          disabled={!canEdit}
          aria-label={t('products.composition.name')}
          onBlur={(e) => commitName(part, e.currentTarget.value)}
          className={`${FIELD_CLASS} min-w-[10rem]`}
        />
        {part.auto && <span className={CHIP_CLASS}>{t('products.composition.fromFile')}</span>}
      </div>
    </td>
  );

  const qtyCell = (part: ProductPart) => (
    <td className="px-3 py-2">
      <div className="flex items-center gap-2 flex-wrap">
        <input
          key={part.qty_per_unit}
          type="number"
          min={0}
          defaultValue={part.qty_per_unit}
          disabled={!canEdit}
          aria-label={t('products.composition.perUnit')}
          onBlur={(e) => commitQty(part, e.currentTarget.value)}
          className={`${FIELD_CLASS} w-20 text-right tabular-nums`}
        />
        {part.qty_per_unit === 0 && (
          <span className="text-xs text-amber-400">{t('products.composition.notCounted')}</span>
        )}
      </div>
    </td>
  );

  const deleteButton = (part: ProductPart) =>
    canEdit && (
      <button
        type="button"
        onClick={() => setDeleting(part)}
        title={t('products.composition.delete')}
        aria-label={t('products.composition.delete')}
        className="p-1.5 rounded-lg text-bambu-gray hover:text-white hover:bg-bambu-dark transition-colors"
      >
        <Trash2 className="w-4 h-4" />
      </button>
    );

  return (
    <section className="space-y-4">
      <h2 className="text-lg font-semibold text-white">{t('products.composition.title')}</h2>

      {printed.length > 0 && (
        <div className="space-y-2">
          <h3 className="text-sm text-bambu-gray">{t('products.composition.printed')}</h3>
          <div className="overflow-x-auto rounded-xl border border-bambu-dark-tertiary">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-bambu-gray border-b border-bambu-dark-tertiary">
                  <th className={HEAD_CLASS}>{t('products.composition.name')}</th>
                  <th className={HEAD_CLASS}>{t('products.composition.perUnit')}</th>
                  <th className={HEAD_CLASS}>{t('products.composition.aliases')}</th>
                  <th className={HEAD_CLASS} />
                </tr>
              </thead>
              <tbody>
                {printed.map((part) => (
                  <tr
                    key={part.id}
                    data-testid={`part-${part.id}-row`}
                    className="border-b border-bambu-dark-tertiary last:border-0 align-top"
                  >
                    {nameCell(part)}
                    {qtyCell(part)}
                    <td className="px-3 py-2">
                      <div className="flex items-center gap-1 flex-wrap">
                        {part.aliases.map((alias) => (
                          <span key={alias} className={CHIP_CLASS}>
                            {alias}
                            {canEdit && (
                              <button
                                type="button"
                                data-testid={`part-${part.id}-alias-remove-${alias}`}
                                onClick={() => removeAlias.mutate({ partId: part.id, nameKey: alias })}
                                aria-label={`${t('common.delete')}: ${alias}`}
                                className="text-bambu-gray hover:text-white"
                              >
                                <X className="w-3 h-3" />
                              </button>
                            )}
                          </span>
                        ))}
                        {canEdit &&
                          (aliasOpen === part.id ? (
                            <input
                              autoFocus
                              type="text"
                              data-testid={`part-${part.id}-alias-input`}
                              placeholder={t('products.composition.aliasPlaceholder')}
                              aria-label={t('products.composition.addAlias')}
                              onKeyDown={(e) => {
                                if (e.key === 'Escape') setAliasOpen(null);
                                if (e.key !== 'Enter') return;
                                const nameKey = e.currentTarget.value.trim();
                                // The alias is sent AS TYPED — the server owns
                                // the normalisation, and folding the case here
                                // would send a key the operator never wrote.
                                if (nameKey) addAlias.mutate({ partId: part.id, nameKey });
                              }}
                              className={`${FIELD_CLASS} w-48`}
                            />
                          ) : (
                            <button
                              type="button"
                              data-testid={`part-${part.id}-alias-add`}
                              onClick={() => setAliasOpen(part.id)}
                              className="px-2 py-0.5 rounded text-xs text-bambu-gray hover:text-white hover:bg-bambu-dark transition-colors"
                            >
                              {t('products.composition.addAlias')}
                            </button>
                          ))}
                      </div>
                    </td>
                    <td className="px-3 py-2">
                      <div className="flex items-center justify-end gap-2">
                        {canEdit && printed.length > 1 && (
                          <select
                            value=""
                            aria-label={t('products.composition.mergeInto')}
                            onChange={(e) => {
                              const target = printed.find((other) => other.id === Number(e.target.value));
                              if (target) setMerging({ source: part, target });
                            }}
                            className={FIELD_CLASS}
                          >
                            <option value="">{t('products.composition.mergeInto')}</option>
                            {printed
                              .filter((other) => other.id !== part.id)
                              .map((other) => (
                                <option key={other.id} value={other.id}>
                                  {other.name}
                                </option>
                              ))}
                          </select>
                        )}
                        {deleteButton(part)}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {purchased.length > 0 && (
        <div className="space-y-2">
          <h3 className="text-sm text-bambu-gray">{t('products.composition.purchased')}</h3>
          <div className="overflow-x-auto rounded-xl border border-bambu-dark-tertiary">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-bambu-gray border-b border-bambu-dark-tertiary">
                  <th className={HEAD_CLASS}>{t('products.composition.name')}</th>
                  <th className={HEAD_CLASS}>{t('products.composition.perUnit')}</th>
                  <th className={HEAD_CLASS}>{t('products.composition.unitPrice')}</th>
                  <th className={HEAD_CLASS}>{t('products.composition.sourcingUrl')}</th>
                  <th className={HEAD_CLASS}>{t('products.composition.remarks')}</th>
                  <th className={HEAD_CLASS} />
                </tr>
              </thead>
              <tbody>
                {purchased.map((part) => (
                  <tr
                    key={part.id}
                    data-testid={`part-${part.id}-row`}
                    className="border-b border-bambu-dark-tertiary last:border-0 align-top"
                  >
                    {nameCell(part)}
                    {qtyCell(part)}
                    <td className="px-3 py-2 text-white tabular-nums">
                      {part.unit_price != null ? formatMoney(part.unit_price, settings?.currency) : '—'}
                    </td>
                    <td className="px-3 py-2">
                      {part.sourcing_url && (
                        <a
                          href={part.sourcing_url}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="inline-flex items-center gap-1 text-bambu-green hover:underline"
                        >
                          <ExternalLink className="w-4 h-4" />
                          {t('products.composition.sourcingUrl')}
                        </a>
                      )}
                    </td>
                    <td className="px-3 py-2 text-bambu-gray">{part.remarks}</td>
                    <td className="px-3 py-2 text-right">{deleteButton(part)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* The row gates itself on `canEdit` — one guard, in the component that
          owns the form, rather than two that can drift apart. */}
      <AddPartRow productId={product.id} canEdit={canEdit} />

      {deleting && (
        <ConfirmModal
          title={t('products.composition.delete')}
          message={t('products.composition.confirmDelete', { name: deleting.name })}
          confirmText={t('common.delete')}
          variant="danger"
          isLoading={remove.isPending}
          onConfirm={() => remove.mutate(deleting.id)}
          onCancel={() => setDeleting(null)}
        />
      )}

      {merging && (
        <ConfirmModal
          title={t('products.composition.mergeInto')}
          message={t('products.composition.mergeConfirm', {
            source: merging.source.name,
            target: merging.target.name,
          })}
          variant="warning"
          isLoading={merge.isPending}
          onConfirm={() => merge.mutate(merging)}
          onCancel={() => setMerging(null)}
        />
      )}
    </section>
  );
}
